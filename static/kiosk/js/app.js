import { CONFIG, Utils } from './config.js';
import { state } from './state.js';
import { BrokerApi } from './broker-api.js';
import { ProfileScreen } from './screens/profile.js';
import { CatalogScreen } from './screens/catalog.js';
import { DetailScreen } from './screens/detail.js';
import { PlayerScreen } from './screens/player.js';
import { GrownUpsScreen } from './screens/grownups.js';
import { PinScreen } from './screens/pin.js';
import { ScreensaverScreen } from './screens/screensaver.js';

import { eventsMixin } from './events.js';
import { lifecycleMixin } from './lifecycle.js';
import { timersMixin } from './timers.js';

/**
 * Main Controller & Event Router
 */
const App = {
  container: null,
  statusBadge: null,
  _wakeLock: null,
  init() {
    this.container = document.getElementById('app');
    this.statusBadge = document.getElementById('brokerStatus');
    this.applyDisplayBrightness();
    this.syncWakeLock();
    window.addEventListener('pointerdown', () => this.syncWakeLock(), { passive: true, once: true });
    window.__storydeckSyncSettings = () => BrokerApi.syncDeviceSettings();
    // Suppress native browser context / long-press callout menus on touch tablets
    window.addEventListener('contextmenu', (e) => e.preventDefault());
    this.bindEvents();
    this.startSleepTimerTicker();
    this.startInactivityTracker();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.syncWakeLock();
        if (!state.isOffline && state.isApproved !== false) {
          const prevScreen = state.screen;
          BrokerApi.getDeviceConfig()
            .then(() => {
              this.syncActiveDevicePlayback();
              this.updateLiveTelemetryUI(prevScreen);
            })
            .catch(() => {});
        }
      }
    });
    document.addEventListener('fullscreenchange', () => {
      if (state.screen === 'grownups') this.render();
    });
    this.bootstrap();
  },
  async acquireWakeLock() {
    if (state.parentSettings?.keepAwake === false) {
      await this.releaseWakeLock();
      return;
    }
    if (typeof navigator === 'undefined' || !('wakeLock' in navigator) || document.hidden || this._wakeLock) {
      return;
    }
    try {
      const sentinel = await navigator.wakeLock.request('screen');
      this._wakeLock = sentinel;
      sentinel.addEventListener('release', () => {
        if (this._wakeLock === sentinel) this._wakeLock = null;
      });
    } catch (e) {
      console.debug('Screen Wake Lock unavailable or deferred:', e?.message || e);
    }
  },
  async releaseWakeLock() {
    if (!this._wakeLock) return;
    try {
      await this._wakeLock.release();
    } catch (e) {
      // Ignore already-released sentinel errors
    } finally {
      this._wakeLock = null;
    }
  },
  syncWakeLock() {
    if (state.parentSettings?.keepAwake === false) {
      this.releaseWakeLock();
    } else {
      this.acquireWakeLock();
    }
  },
  applyDisplayBrightness() {
    if (!this.container) return;
    const val = Math.max(10, Math.min(100, Number(state.parentSettings?.brightness ?? 100) || 100));
    this.container.style.filter = val < 100 ? `brightness(${val / 100})` : '';
  },
  /**
   * Confirms playback has successfully initiated on the physical player before switching to Now Playing.
   */
  async initiatePlayback(card, chapterIndex = 0, triggerEl = null) {
    if (!card) return;
    // Safety Interlock: Away/Traveling Player Guard
    const activeDev = state.activeDevice;
    if (activeDev && activeDev.online && activeDev.is_home === false) {
      if (state.blockRemotePlayback) {
        const msg = `${activeDev.name} is currently away from home (traveling). Remote playback is blocked.`;
        if (window.StoryDeckUI?.showToast) window.StoryDeckUI.showToast(msg, 'warning');
        else alert(msg);
        return;
      } else {
        const proceed = confirm(`${activeDev.name} is currently away from home (traveling).
Are you sure you want to start audio playback on this remote player?`);
        if (!proceed) return;
      }
    }
    const originalText = triggerEl ? triggerEl.innerHTML : null;
    if (triggerEl) {
      triggerEl.style.opacity = '0.6';
      triggerEl.style.pointerEvents = 'none';
      if (triggerEl.classList.contains('playbtn')) {
        triggerEl.innerHTML = '&#9203; Starting...';
      }
    }
    try {
      const res = await BrokerApi.playMedia(card, chapterIndex);
      if (res && (res.ok || res.status === 'success')) {
        const nowIso = new Date().toISOString();
        card.last_played_at = nowIso;
        const cardKey = card.id || card.cardId;
        const catEntry = (state.catalog || []).find(c => (c.id || c.cardId) === cardKey);
        if (catEntry) catEntry.last_played_at = nowIso;

        state.setCard(card);
        state.setPreviewCard(card);
        const resolvedIdx = (chapterIndex != null && chapterIndex >= 0)
          ? chapterIndex
          : (res.chapter != null && res.chapter > 0 ? (res.chapter - 1) : 0);
        state.setChapter(resolvedIdx);
        state.setPlaying(true);
        state.localSessionActive = true;
        
        // Immediately fetch fresh device status to verify live playback
        await BrokerApi.getDeviceConfig();
        this.syncActiveDevicePlayback();
        // Transition to Now Playing screen now that playback is confirmed
        state.setScreen('now');
        this.render();
      } else {
        const msg = res?.error || res?.message || 'Playback failed to start';
        if (window.StoryDeckUI?.showToast) window.StoryDeckUI.showToast(`Could not start playback: ${msg}`, 'danger');
        else alert(`Could not start playback: ${msg}`);
        if (triggerEl && originalText) {
          triggerEl.innerHTML = originalText;
          triggerEl.style.opacity = '1';
          triggerEl.style.pointerEvents = 'auto';
        }
      }
    } catch (err) {
      console.error('Playback initiation error:', err);
      const errMsg = `Playback error: ${err.message || err}`;
      if (window.StoryDeckUI?.showToast) window.StoryDeckUI.showToast(errMsg, 'danger');
      else alert(errMsg);
      if (triggerEl && originalText) {
        triggerEl.innerHTML = originalText;
        triggerEl.style.opacity = '1';
        triggerEl.style.pointerEvents = 'auto';
      }
    }
  },
  render() {
    if (!this.container) return;
    // Preserve scroll positions for scrollable grids across renders
    const activeScrollEl = this.container.querySelector('.grid, .grid-series, .chapters');
    const savedScrollTop = activeScrollEl ? activeScrollEl.scrollTop : 0;
    const savedScrollLeft = activeScrollEl ? activeScrollEl.scrollLeft : 0;
    switch (state.screen) {
      case 'profile':
        this.container.innerHTML = ProfileScreen.render();
        break;
      case 'catalog':
        this.container.innerHTML = CatalogScreen.render();
        break;
      case 'detail':
        this.container.innerHTML = DetailScreen.render();
        break;
      case 'now':
        this.container.innerHTML = PlayerScreen.render();
        break;
      case 'pin':
        this.container.innerHTML = PinScreen.render();
        break;
      case 'grownups':
        this.container.innerHTML = GrownUpsScreen.render();
        this.bindGrownUpsEvents();
        break;
      case 'screensaver':
        this.container.innerHTML = ScreensaverScreen.render();
        break;
      case 'offline':
        this.container.innerHTML = `
          <div class="profile-screen" style="text-align:center; padding:40px;">
            <div style="font-size:48px; margin-bottom:16px;">🔌</div>
            <div class="profile-title">BROKER OFFLINE</div>
            <div class="profile-subtitle" style="margin-top:12px; max-width:480px; line-height:1.5;">
              Connection lost to <code>${Utils.esc(CONFIG.BROKER_BASE)}</code>.<br>
              Pinging broker for recovery... You will be returned to your screen as soon as connection restores.
            </div>
          </div>
        `;
        break;
      default:
        this.container.innerHTML = ProfileScreen.render();
    }
    // Restore scroll position
    if (savedScrollTop > 0 || savedScrollLeft > 0) {
      const restoredEl = this.container.querySelector('.grid, .grid-series, .chapters');
      if (restoredEl) {
        restoredEl.scrollTop = savedScrollTop;
        restoredEl.scrollLeft = savedScrollLeft;
      }
    }
    const renderedNowbar = this.container.querySelector('.nowbar');
    this._lastNowbarHtml = renderedNowbar ? renderedNowbar.outerHTML : null;
  },
  async selectProfile(profileId) {
    const p = state.profiles.find(x => x.id === profileId);
    if (!p) return;
    state.setActiveProfile(p);
    const cached = BrokerApi.getCachedCatalog(p.id, state.activeDevice?.id);
    const prevFp = cached ? cached.map(c => `${c.id}:${c.last_played_at || ''}`).join(',') : '';
    if (cached) {
      state.catalog = cached;
      this.syncActiveDevicePlayback();
    }
    state.setScreen('catalog');
    this.render();
    // Fetch dynamic live catalog for selected profile (stale-while-revalidate when cached)
    try {
      const fresh = await BrokerApi.fetchLiveCatalog(p.id, state.activeDevice?.id);
      const nextFp = (fresh || []).map(c => `${c.id}:${c.last_played_at || ''}`).join(',');
      this.syncActiveDevicePlayback();
      if (!cached || nextFp !== prevFp) {
        this.render();
      } else {
        this.updateLiveTelemetryUI(state.screen);
      }
    } catch (e) {
      console.error('Failed to load profile catalog:', e);
    }
  },
  syncActiveDevicePlayback() {
    const dev = state.activeDevice;
    if (dev && dev.volume != null) {
      state.volume = Math.min(dev.volume_max != null ? dev.volume_max : 100, dev.volume);
    }
    const hasCard = dev && (dev.has_card || dev.is_playing || dev.now_playing || dev.item_id);
    if (hasCard) {
      state.isPlaying = Boolean(dev.is_playing);
      // Find matching item in catalog by ID or Title
      const matched = state.catalog.find(c => 
        (dev.item_id && (c.id === dev.item_id || c.cardId === dev.item_id)) ||
        (dev.now_playing && c.title && c.title.toLowerCase().includes(dev.now_playing.toLowerCase())) ||
        (dev.now_playing && dev.now_playing.toLowerCase().includes(c.title ? c.title.toLowerCase() : '---'))
      );
      const coverUrl = (matched && (matched.cover || matched.img))
        ? (matched.cover || matched.img)
        : (dev.cover_url || (dev.item_id ? `${CONFIG.BROKER_BASE}/api/thumb?type=abs&id=${encodeURIComponent(dev.item_id)}&size=300` : null));
      const fullTitle = dev.now_playing || matched?.title || 'Audiobook';
      const chapterTitle = dev.chapter_title || (matched?.chapters?.[state.chapterIndex]?.title) || 'Playing Track';
      const author = dev.author || matched?.author || '';
      // Only overwrite state.card if user isn't currently browsing an alternate card detail view, or if state.card matches
      if (!state.card || state.screen !== 'detail' || (matched && (state.card.id === matched.id || state.card.cardId === matched.cardId))) {
        state.card = {
          id: dev.item_id || matched?.id || 'remote-play',
          title: fullTitle,
          chapter_title: chapterTitle,
          author: author,
          cover: coverUrl || '/static/img/placeholder.png',
          duration: dev.duration || matched?.duration || 1800,
          chapters: matched?.chapters || [{ title: chapterTitle, duration: dev.duration || 1800 }]
        };
      }
    } else {
      // No card in player: clear live playing card & playing state;
      // return to catalog if currently on Now Playing (preview in Detail screen remains accessible)
      state.isPlaying = false;
      state.card = null;
      if (state.screen === 'now') {
        state.setScreen('catalog');
      }
    }
  },
  updateLiveTelemetryUI(prevScreen) {
    if (!this.container) return;
    if (state.screen !== prevScreen) {
      this._lastNowbarHtml = null;
      this.render();
      return;
    }
    if (state.screen === 'now') {
      if (!PlayerScreen.tick()) {
        this.render();
      }
      return;
    }
    if (state.screen === 'catalog' || state.screen === 'detail') {
      const nowbarEl = this.container.querySelector('.nowbar');
      const nextNowbarHtml = PlayerScreen.renderNowbar();
      if (nowbarEl && this._lastNowbarHtml !== nextNowbarHtml) {
        nowbarEl.outerHTML = nextNowbarHtml;
        this._lastNowbarHtml = nextNowbarHtml;
      } else if (!nowbarEl) {
        this.render();
      }
      if (state.screen === 'catalog') {
        const dev = state.activeDevice;
        const _ui = (typeof window !== 'undefined' && window.StoryDeckUI) ? window.StoryDeckUI : null;
        const meta = _ui ? _ui.getDeviceStatusMeta(dev) : {
          isAway: Boolean(dev && dev.online && dev.is_home === false),
          icon: (dev && dev.online && dev.is_home === false) ? '✈️' : (dev?.is_playing ? '▶' : (dev?.online ? '●' : '○')),
          statusClass: (dev && dev.online && dev.is_home === false) ? 'status-away' : (dev?.is_playing ? 'status-playing' : (dev?.online ? 'status-online' : 'status-offline')),
        };
        const dotEl = this.container.querySelector('.device-pill .device-dot');
        const labelEl = this.container.querySelector('.device-pill .device-label');
        if (dotEl) {
          dotEl.className = `device-dot ${meta.statusClass}`;
          dotEl.textContent = meta.icon;
        }
        if (labelEl && dev?.name) {
          labelEl.textContent = dev.name;
        }
      }
    }
  },
  /**
   * Continuous background health & telemetry monitor (every 5s, paused when hidden)
   */
  startHealthMonitor() {
    if (this._healthTimer) return;
    this._healthTimer = setInterval(async () => {
      if (state.isOffline || (typeof document !== 'undefined' && document.hidden)) return;
      const prevScreen = state.screen;
      try {
        await BrokerApi.getDeviceConfig();
        if (state.isApproved === false) {
          console.warn('[Core] Device is quarantined in Core! Transitioning to pairing screen.');
          this.enterQuarantineState();
          return;
        }
        this.syncActiveDevicePlayback();
        this.updateLiveTelemetryUI(prevScreen);
      } catch (err) {
        if (state.isApproved === false) {
          this.enterQuarantineState();
          return;
        }
        console.warn('Health monitor detected communication breakdown:', err);
        this.enterOfflineState();
      }
    }, 5000);
  }
};

Object.assign(App, eventsMixin, lifecycleMixin, timersMixin);

// Start application when DOM is ready or immediately if already loaded
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => App.init());
} else {
  App.init();
}
