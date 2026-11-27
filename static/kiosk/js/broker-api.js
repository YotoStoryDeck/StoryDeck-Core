import { CONFIG } from './config.js';
import { state } from './state.js';
/**
 * Broker REST API Service
 * Handles device announcement, token rotation, profile discovery, and catalog fetching.
 */
export const BrokerApi = {
  _catalogCache: new Map(),
  _chaptersById: new Map(),

  _catalogCacheKey(profileId = null, deviceId = null) {
    const isStandalone = state.mode === 'standalone' || !profileId;
    const targetKey = isStandalone ? '__all__' : String(profileId).toLowerCase();
    const devParam = String(deviceId || state.activeDevice?.id || '').trim().toLowerCase();
    return `${targetKey}|${devParam}`;
  },

  getCachedCatalog(profileId = null, deviceId = null) {
    return this._catalogCache.get(this._catalogCacheKey(profileId, deviceId)) || null;
  },
    /**
   * Fast health probe ping to check if broker is online
   */
  async ping() {
    try {
      const controller = new AbortController();
      const id = setTimeout(() => controller.abort(), 3000);
      const res = await fetch(`${CONFIG.BROKER_BASE}/health`, { signal: controller.signal });
      clearTimeout(id);
      return res.ok;
    } catch {
      return false;
    }
  },
/**
   * Registers the virtual device and acquires rotating JWT tokens
   */
  async registerDevice() {
    const res = await fetch(`${CONFIG.BROKER_BASE}/api/devices/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mac: CONFIG.DEVICE_ID,
        friendly_name: CONFIG.FRIENDLY_NAME,
        firmware_version: CONFIG.FIRMWARE_VERSION
      })
    });
    if (res.status === 202) {
      return { status: 'pending_approval' };
    }
    if (res.ok) {
      const data = await res.json();
      if (data.tokens?.access_token) {
        state.deviceToken = data.tokens.access_token;
      }
      if (data.tokens?.refresh_token) {
        state.refreshToken = data.tokens.refresh_token;
      }
      return { status: 'approved', data };
    }
    throw new Error(`Device registration failed with status ${res.status}`);
  },
  /**
   * Refreshes the short-lived device JWT using refreshToken.
   */
  async refreshAuthToken() {
    if (!state.refreshToken) return false;
    try {
      const res = await fetch(`${CONFIG.BROKER_BASE}/api/device/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: state.refreshToken })
      });
      if (!res.ok) return false;
      const data = await res.json().catch(() => ({}));
      const newTok = data.access_token || data.tokens?.access_token;
      if (newTok) {
        state.deviceToken = newTok;
        if (data.refresh_token || data.tokens?.refresh_token) {
          state.refreshToken = data.refresh_token || data.tokens?.refresh_token;
        }
        return true;
      }
    } catch (e) {
      console.warn('Device JWT refresh failed:', e);
    }
    return false;
  },
  async _authedFetch(url, opts = {}) {
    const buildOpts = () => {
      const headers = { ...(opts.headers || {}) };
      if (state.deviceToken) {
        headers['Authorization'] = `Bearer ${state.deviceToken}`;
      }
      return { ...opts, headers };
    };
    let res = await fetch(url, buildOpts());
    if (res.status === 401 && await this.refreshAuthToken()) {
      res = await fetch(url, buildOpts());
    }
    return res;
  },
  /**
   * Queries broker configuration & profile summaries
   */
  async getDeviceConfig() {
    const res = await fetch(`${CONFIG.BROKER_BASE}/api/device/config?device_id=${encodeURIComponent(CONFIG.DEVICE_ID)}`);
    if (res.status === 401 || res.status === 403) {
      state.isApproved = false;
      throw new Error(`Device access revoked (HTTP ${res.status})`);
    }
    if (!res.ok) throw new Error(`Config fetch failed: ${res.status}`);
    const conf = await res.json();
    state.deviceConfig = conf;
    if (!conf.abs_configured && state.sourceFilter === 'abs') {
      state.sourceFilter = 'all';
    }
    if (conf.is_approved !== undefined) {
      state.isApproved = Boolean(conf.is_approved);
    }
    let changed = false;
    if (conf.parent_pin && state.parentPin !== conf.parent_pin) {
      console.log(`[Config] Received OTA Parent PIN update from Core: ${conf.parent_pin}`);
      state.parentPin = conf.parent_pin;
      changed = true;
    }
    if (conf.lock_player !== undefined && state.lockPlayer !== Boolean(conf.lock_player)) {
      state.lockPlayer = Boolean(conf.lock_player);
      changed = true;
    }
    if (conf.locked_player_id !== undefined && conf.locked_player_id && state.lockedPlayerId !== conf.locked_player_id) {
      state.lockedPlayerId = conf.locked_player_id;
      changed = true;
    }
    if (conf.settings && typeof conf.settings === 'object') {
      if (conf.settings.sleepTimerMin !== undefined) state.parentSettings.sleepTimerMin = conf.settings.sleepTimerMin;
      if (conf.settings.sleepTimerMode !== undefined) state.parentSettings.sleepTimerMode = conf.settings.sleepTimerMode;
      if (conf.settings.screensaverMin !== undefined) state.parentSettings.screensaverMin = conf.settings.screensaverMin;
      if (conf.settings.bedtime !== undefined) state.parentSettings.bedtime = conf.settings.bedtime;
      if (conf.settings.wakeTime !== undefined) state.parentSettings.wakeTime = conf.settings.wakeTime;
      if (conf.settings.clockSize !== undefined) state.parentSettings.clockSize = conf.settings.clockSize;
      if (conf.settings.keepAwake !== undefined) state.parentSettings.keepAwake = Boolean(conf.settings.keepAwake);
      if (conf.settings.blockRemotePlayback !== undefined) state.blockRemotePlayback = Boolean(conf.settings.blockRemotePlayback);
      changed = true;
    }
    if (changed) {
      state.persistParentSettings(false);
    }
        if (conf.profiles && conf.profiles.length > 0) {
      state.profiles = conf.profiles.map(p => {
        const players = p.players || [];
        return {
          id: p.id,
          display_name: p.display_name,
          avatar_url: p.avatar_url && p.avatar_url.startsWith('/')
            ? `${CONFIG.BROKER_BASE}${p.avatar_url}`
            : (p.avatar_url || ''),
          avatar_color: p.avatar_color || '#e5352b',
          avatar_initial: p.avatar_initial || '',
          default_player_id: p.default_player_id || '',
          default_player_name: p.default_player_name || 'Yoto Player',
          players: players
        };
      });
      // Keep activeProfile and activeDevice objects updated with live telemetry
      if (state.mode === 'standalone') {
        const allPlayers = state.getAllAvailablePlayers();
        if (state.activeDevice) {
          const updatedDev = allPlayers.find(d => d.id === state.activeDevice.id);
          if (updatedDev) {
            state.activeDevice = updatedDev;
          } else {
            state.activeDevice = state.resolveActiveDevice(null);
          }
        } else if (allPlayers.length > 0) {
          state.activeDevice = state.resolveActiveDevice(null);
        }
      } else if (state.activeProfile) {
        const updatedProf = state.profiles.find(p => p.id === state.activeProfile.id);
        if (updatedProf) {
          state.activeProfile = updatedProf;
          if (state.activeDevice) {
            const updatedDev = (updatedProf.players || []).find(d => d.id === state.activeDevice.id);
            if (updatedDev) {
              state.activeDevice = updatedDev;
            } else {
              state.activeDevice = state.resolveActiveDevice(updatedProf);
            }
          } else {
            state.activeDevice = state.resolveActiveDevice(updatedProf);
          }
        }
      }
    }
    return conf;
  },
  /**
   * Fetches personalized media catalog (ABS + Yoto) for selected profile
   */
  /**
   * Fetches chapters / tracks breakdown for a specific card on demand
   */
  async fetchMediaTracks(mediaId) {
    try {
      const res = await this._authedFetch(`${CONFIG.BROKER_BASE}/api/media/${encodeURIComponent(mediaId)}/tracks`);
      if (!res.ok) return null;
      const data = await res.json();
      const tracks = (data && data.tracks) ? data.tracks : [];
      if (tracks.length > 0) {
        const chapters = tracks.map(t => ({
          key: t.key || '',
          track_key: t.track_key || t.key || '',
          title: t.title,
          duration: t.duration_seconds || 0,
          icon_url: t.icon_url
        }));
        const duration = tracks.reduce((sum, t) => sum + (t.duration_seconds || 0), 0);
        this._chaptersById.set(String(mediaId), { chapters, duration });
      }
      return tracks;
    } catch (e) {
      console.warn(`Failed to fetch tracks for ${mediaId}:`, e);
      return [];
    }
  },
  async fetchLiveCatalog(profileId = null, deviceId = null) {
    const isStandalone = state.mode === 'standalone' || !profileId;
    const targetKey = isStandalone ? '__all__' : encodeURIComponent(profileId);
    const devParam = (deviceId || state.activeDevice?.id || '').trim();
    const qs = devParam
      ? `client=kiosk&device_id=${encodeURIComponent(devParam)}`
      : 'client=kiosk';

    let absItems = [];
    let yotoItems = [];
    const combinedRes = await this._authedFetch(`${CONFIG.BROKER_BASE}/api/kids/${targetKey}/catalog?${qs}`)
      .catch(() => null);
    if (combinedRes && combinedRes.ok) {
      const payload = await combinedRes.json().catch(() => ({}));
      absItems = payload.abs || [];
      yotoItems = payload.yoto || [];
    } else {
      const skipAbs = state.deviceConfig && state.deviceConfig.abs_configured === false;
      const absPromise = skipAbs
        ? Promise.resolve([])
        : this._authedFetch(`${CONFIG.BROKER_BASE}/api/kids/${targetKey}/abs?${qs}`)
          .then(r => r.ok ? r.json() : [])
          .catch(() => []);
      const yotoPromise = this._authedFetch(`${CONFIG.BROKER_BASE}/api/kids/${targetKey}/yoto?${qs}`)
        .then(r => r.ok ? r.json() : [])
        .catch(() => []);
      [absItems, yotoItems] = await Promise.all([absPromise, yotoPromise]);
    }
    const combined = [];
    (absItems || []).forEach(b => {
      const thumb = b.thumb || b.cover_url || b.cover;
      const coverUrl = thumb ? (thumb.startsWith('http') ? thumb : `${CONFIG.BROKER_BASE}${thumb}`) : null;
      const cachedCh = this._chaptersById.get(String(b.id));
      const mappedCh = (b.chapters || []).map(ch => ({ title: ch.title, duration: (ch.end && ch.start) ? (ch.end - ch.start) : (ch.duration || 0) }));
      combined.push({
        id: b.id,
        title: b.title,
        author: b.author,
        cover: coverUrl,
        is_highlight: !!b.is_highlight,
        has_physical: !!b.has_physical,
        last_played_at: b.last_played_at || '',
        daily: !!b.daily,
        duration: b.duration || (cachedCh ? cachedCh.duration : 0),
        chapters: mappedCh.length ? mappedCh : (cachedCh ? cachedCh.chapters : []),
        source: 'abs',
        series: b.series || '',
        sequence: b.sequence != null ? b.sequence : null,
        sequence_display: b.sequence_display || (b.sequence != null ? String(b.sequence) : null),
        series_list: b.series_list || (b.series ? [{ name: b.series, sequence: b.sequence, sequence_display: b.sequence_display }] : [])
      });
    });
    (yotoItems || []).forEach(y => {
      const thumb = y.thumb || y.cover_url || y.cover;
      const coverUrl = thumb ? (thumb.startsWith('http') ? thumb : `${CONFIG.BROKER_BASE}${thumb}`) : null;
      const yid = y.card_id || y.id;
      const cachedCh = this._chaptersById.get(String(yid));
      const mappedCh = (y.chapters || []).map(ch => ({ title: ch.title, duration: ch.duration || 0 }));
      combined.push({
        id: yid,
        title: y.title,
        author: y.author || 'Yoto',
        cover: coverUrl,
        is_highlight: !!y.is_highlight,
        is_myo: !!y.is_myo,
        has_physical: !!y.has_physical,
        last_played_at: y.last_played_at || '',
        daily: !!y.daily,
        duration: y.duration || (cachedCh ? cachedCh.duration : 0),
        chapters: mappedCh.length ? mappedCh : (cachedCh ? cachedCh.chapters : []),
        source: 'yoto',
        series: y.series || '',
        sequence: y.sequence != null ? y.sequence : null,
        sequence_display: y.sequence_display || (y.sequence != null ? String(y.sequence) : null),
        groups: y.groups || [],
        series_list: y.series_list || (y.series ? [{ name: y.series, sequence: y.sequence, sequence_display: y.sequence_display }] : [])
      });
    });
    const sorted = (typeof window !== 'undefined' && window.StoryDeckSeries)
      ? window.StoryDeckSeries.sortItems(combined, { sortBy: 'name', highlightsFirst: true })
      : combined.sort((a, b) => {
          if (!!b.is_highlight !== !!a.is_highlight) return b.is_highlight ? 1 : -1;
          const cleanA = (a.title || '').replace(/^(the|a|an)\s+/i, '').trim().toLowerCase();
          const cleanB = (b.title || '').replace(/^(the|a|an)\s+/i, '').trim().toLowerCase();
          return cleanA.localeCompare(cleanB);
        });
    state.catalog = sorted;
    this._catalogCache.set(this._catalogCacheKey(profileId, deviceId), sorted);
    return sorted;
  },
  /**
   * Dispatches playback command to the broker /api/play endpoint.
   * @param {Object} card - The media card object (ABS book or Yoto card).
   * @param {number} chapterIndex - 0-based chapter index from UI (converted to 1-based for broker).
   */
  async playMedia(card, chapterIndex = null) {
    if (!card) return { ok: false, error: 'No media selected' };
    const headers = {
      'Content-Type': 'application/json'
    };
    if (state.deviceToken) {
      headers['Authorization'] = `Bearer ${state.deviceToken}`;
    }
    const payload = {
      child: state.activeProfile?.id || '',
      type: card.source || card.type || 'abs',
      id: card.id || card.cardId,
      device_id: state.activeDevice?.id || state.activeProfile?.default_player_id || ''
    };
    if (chapterIndex != null && chapterIndex >= 0) {
      const chObj = (card.chapters || [])[chapterIndex] || {};
      payload.chapter = chapterIndex + 1;
      if (chObj.key) payload.chapter_key = chObj.key;
      if (chObj.track_key) payload.track_key = chObj.track_key;
    }
    try {
      const res = await this._authedFetch(`${CONFIG.BROKER_BASE}/api/play`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
      if (!res.ok) {
        console.error('Playback request returned non-OK status:', res.status, data);
        return { ok: false, status: res.status, ...data };
      }
      return data;
    } catch (err) {
      console.error('Playback dispatch failed:', err);
      return { ok: false, error: err.message };
    }
  },
  /**
   * Dispatches transport / player commands (pause, resume, stop, next, prev, volume)
   * to the broker /api/player/control endpoint.
   */
  /**
   * Pushes Grown-ups settings changes to StoryDeck Core (/api/device/settings)
   */
  async syncDeviceSettings() {
    try {
      await fetch(`${CONFIG.BROKER_BASE}/api/device/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: CONFIG.DEVICE_ID,
          parent_pin: state.parentPin,
          lock_player: state.lockPlayer,
          locked_player_id: state.lockedPlayerId || '',
          settings: {
            ...state.parentSettings,
            blockRemotePlayback: state.blockRemotePlayback
          }
        })
      });
    } catch (e) {
      console.warn('Could not sync settings to Core:', e);
    }
  },

  async sendPlayerControl(action, payload = {}) {
    const deviceId = state.activeDevice?.id || state.activeProfile?.default_player_id;
    if (!deviceId) return { ok: false, error: 'No active device selected' };
    const headers = { 'Content-Type': 'application/json' };
    if (state.deviceToken) {
      headers['Authorization'] = `Bearer ${state.deviceToken}`;
    }
    const body = {
      device_id: deviceId,
      action: action,
      ...payload
    };
    try {
      const res = await this._authedFetch(`${CONFIG.BROKER_BASE}/api/player/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
      if (!res.ok) {
        console.warn('Player control command error:', res.status, data);
        return { ok: false, status: res.status, ...data };
      }
      return data;
    } catch (err) {
      console.error('Player control fetch failed:', err);
      return { ok: false, error: err.message };
    }
  }
};
