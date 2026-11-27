import { state } from '../state.js';
import { Utils } from '../config.js';
import { PlayerScreen } from './player.js';
/**
 * Screen 2: Catalog Browser & Navigation Component
 */
export const CatalogScreen = {
  renderTopNav() {
    const isStandalone = state.mode === 'standalone';
    const isLocked = state.lockPlayer;
    const p = state.activeProfile || state.profiles[0] || { display_name: 'Profiles', avatar_url: '' };
    const players = isStandalone ? state.getAllAvailablePlayers() : (p.players || []);
    const activeDev = state.activeDevice || state.resolveActiveDevice(isStandalone ? null : p);
    const hasMultiple = players.length > 1;
    const _ui = (typeof window !== 'undefined' && window.StoryDeckUI) ? window.StoryDeckUI : null;
    const devMeta = _ui ? _ui.getDeviceStatusMeta(activeDev) : (() => {
      const isAway = activeDev && activeDev.online && activeDev.is_home === false;
      return {
        isAway,
        icon: isAway ? '✈️' : (activeDev?.is_playing ? '▶' : (activeDev?.online ? '●' : '○')),
        statusClass: isAway ? 'status-away' : (activeDev?.is_playing ? 'status-playing' : (activeDev?.online ? 'status-online' : 'status-offline')),
      };
    })();
    const devStatusIcon = devMeta.icon;
    const devStatusClass = devMeta.statusClass;
    // Dropdown options if picker is open (and player is not locked)
    const pickerDropdown = (state.isDevicePickerOpen && hasMultiple && !isLocked) ? `
      <div class="device-dropdown">
        <div class="dropdown-header">Select Target Player</div>
        ${players.map(d => {
          const isSelected = d.id === activeDev?.id;
          const dMeta = _ui ? _ui.getDeviceStatusMeta(d) : {
            isAway: d.online && d.is_home === false,
            isPlaying: d.is_playing,
            icon: (d.online && d.is_home === false) ? '✈️' : (d.is_playing ? '▶' : '●'),
            dotClass: (d.online && d.is_home === false) ? 'dot-away' : (d.is_playing ? 'dot-play' : (d.online ? 'dot-on' : 'dot-off')),
          };
          return `
            <div class="device-opt ${isSelected ? 'selected' : ''}" data-act="select-device" data-device-id="${Utils.esc(d.id)}">
              <span class="dev-dot ${dMeta.dotClass}">
                ${dMeta.icon === '○' ? '●' : dMeta.icon}
              </span>
              <span class="dev-name">${Utils.esc(d.name)}</span>
              ${dMeta.isAway ? '<span class="dev-tag" style="background:#ea580c; color:#fff;">AWAY</span>' : ''}
              ${d.id === p.default_player_id ? '<span class="dev-tag">DEFAULT</span>' : ''}
              ${dMeta.isPlaying ? '<span class="dev-tag play">PLAYING</span>' : ''}
            </div>
          `;
        }).join('')}
      </div>
    ` : '';
    const profAvatarHtml = _ui
      ? _ui.renderProfileAvatarHtml(p, 24)
      : ((p.avatar_url && p.avatar_url.includes('?t='))
        ? `<img src="${Utils.esc(p.avatar_url)}" onerror="this.style.display='none'">`
        : `<span style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:${Utils.esc(p.avatar_color || '#e5352b')};color:#fff;font-size:12px;font-weight:800;">${Utils.esc(p.avatar_initial || (p.display_name || 'P').charAt(0).toUpperCase())}</span>`);
    return `
      <div class="topnav">
        <div class="topnav-left">
          ${!isStandalone ? `
            <!-- Core Mode: Profile Selector Pill (only clickable if allow_profile_switch is true) -->
            <div class="prof-pill" ${state.deviceConfig?.allow_profile_switch !== false && state.profiles.length > 1 ? 'data-act="switch-profile" title="Switch profile"' : 'style="cursor:default;" title="Assigned profile"'}>
              ${profAvatarHtml}
              <span>${Utils.esc(p.display_name)}</span>
              ${state.deviceConfig?.allow_profile_switch !== false && state.profiles.length > 1 ? '<span style="font-size:10px; color:#8a8276;">&#9660;</span>' : ''}
            </div>
          ` : ''}
          <!-- Device / Player Selector Button (Primary identity in Standalone mode) -->
          <div class="device-pill-wrap">
            <div class="device-pill ${(!isLocked && hasMultiple) ? 'clickable' : ''}" 
                 data-act="${(!isLocked && hasMultiple) ? 'toggle-device-picker' : ''}" 
                 title="${isLocked ? 'Controller is locked to this player' : (hasMultiple ? 'Choose playback device' : 'Active playback device')}">
              ${isLocked ? '<span style="font-size:11px;">🔒</span>' : ''}
              <span class="device-dot ${devStatusClass}">${devStatusIcon}</span>
              <span class="device-label">${Utils.esc(activeDev?.name || p.default_player_name || 'Yoto Player')}</span>
              ${(!isLocked && hasMultiple) ? '<span class="pill-chev">&#9660;</span>' : ''}
            </div>
            ${pickerDropdown}
          </div>
        </div>
        <div class="nav-tabs">
          <button class="tab-btn ${state.activeTab === 'all' ? 'active' : ''}" data-tab="all">Catalog</button>
          <button class="tab-btn ${state.activeTab === 'series' ? 'active' : ''}" data-tab="series">Series</button>
          <button class="tab-btn ${state.activeTab === 'recent' ? 'active' : ''}" data-tab="recent">Recently Played</button>
          
          <!-- Grown-Ups Screen Gear -->
          <button class="nav-gear-btn" data-act="gear-button" title="Grown-ups settings">&#9881;</button>
        </div>
      </div>
    `;
  },
  cleanTitle(t) {
    if (typeof window !== 'undefined' && window.StoryDeckSeries) {
      return window.StoryDeckSeries.cleanTitleForSort(t);
    }
    return (t || '').replace(/^(the|a|an)\s+/i, '').trim().toLowerCase();
  },
  getFilteredCatalog() {
    const q = (state.searchQuery || '').trim().toLowerCase();
    const hasAbs = Boolean(state.deviceConfig?.abs_configured);
    const src = (!hasAbs && state.sourceFilter === 'abs') ? 'all' : (state.sourceFilter || 'all');
    return (state.catalog || []).filter(c => {
      const itemSrc = (c.source === 'abs' || c.type === 'abs') ? 'abs' : 'yoto';
      if (src !== 'all' && itemSrc !== src) return false;
      if (!q) return true;
      if ((c.title || '').toLowerCase().includes(q)) return true;
      if ((c.author || '').toLowerCase().includes(q)) return true;
      if ((c.series || '').toLowerCase().includes(q)) return true;
      if (Array.isArray(c.series_list) && c.series_list.some(s => (s.name || '').toLowerCase().includes(q))) {
        return true;
      }
      return false;
    });
  },
  renderToolbar(matchCount) {
    if ((state.activeTab !== 'all' && state.activeTab !== 'series') || !(state.catalog || []).length) {
      return '';
    }
    const hasAbs = Boolean(state.deviceConfig?.abs_configured);
    const src = (!hasAbs && state.sourceFilter === 'abs') ? 'all' : (state.sourceFilter || 'all');
    const q = state.searchQuery || '';
    const filterChipsHtml = hasAbs ? `
        <div class="catalog-filter-chips">
          <button type="button" class="src-chip ${src === 'all' ? 'active' : ''}" data-act="set-source-filter" data-src="all">All</button>
          <button type="button" class="src-chip ${src === 'abs' ? 'active' : ''}" data-act="set-source-filter" data-src="abs">ABS</button>
          <button type="button" class="src-chip ${src === 'yoto' ? 'active' : ''}" data-act="set-source-filter" data-src="yoto">Yoto</button>
        </div>
    ` : '';
    return `
      <div class="catalog-toolbar">
        <div class="catalog-search-wrap">
          <span class="catalog-search-icon" aria-hidden="true">&#128269;</span>
          <input id="kioskCatalogSearch" class="catalog-search-input" type="search" placeholder="Search books, authors, series..." value="${Utils.esc(q)}" autocomplete="off" spellcheck="false">
          <button type="button" class="catalog-search-clear" data-act="clear-search" title="Clear search" style="${q ? '' : 'display:none;'}">&#10005;</button>
        </div>
        ${filterChipsHtml}
        <span class="catalog-count">${matchCount} ${matchCount === 1 ? 'book' : 'books'}</span>
      </div>
    `;
  },
  renderAllGridItems(items) {
    const list = [...(items || [])];
    const sorted = (typeof window !== 'undefined' && window.StoryDeckSeries)
      ? window.StoryDeckSeries.sortItems(list, { sortBy: 'name', highlightsFirst: true })
      : list.sort((a, b) => {
          if (!!b.is_highlight !== !!a.is_highlight) return b.is_highlight ? 1 : -1;
          return this.cleanTitle(a.title).localeCompare(this.cleanTitle(b.title));
        });
    const tiles = sorted.map(c => `
      <div class="tile" data-card="${Utils.esc(c.id || c.cardId)}" title="${Utils.esc(c.title)}">
        <img loading="lazy" decoding="async" src="${Utils.esc(c.cover || c.img || '/static/img/placeholder.png')}" onerror="this.onerror=null;this.src='/static/img/placeholder.png'">
        ${(c.is_highlight && state.activeTab === 'all') ? '<span class="badge-star" title="Highlighted">★</span>' : ''}
        ${c.source === 'abs' || c.type === 'abs' ? '<span class="badge-abs">ABS</span>' : '<span class="badge-yoto">YOTO</span>'}
      </div>
    `).join('');
    if (tiles.length) return tiles;
    const msg = (state.catalog || []).length
      ? 'No audiobooks match your search or filter.'
      : 'No audiobooks found in this library.';
    return `<div style="padding:40px; color:#888; text-align:center; grid-column:1/-1;">${msg}</div>`;
  },
  updateSearchResultsInPlace() {
    const appEl = typeof document !== 'undefined' ? document.getElementById('app') : null;
    if (!appEl || state.screen !== 'catalog') return;
    const filtered = this.getFilteredCatalog();
    if (state.activeTab === 'series') {
      const seriesEl = appEl.querySelector('.grid-series');
      if (seriesEl) seriesEl.innerHTML = this.renderSeriesView(filtered);
    } else if (state.activeTab === 'all') {
      const gridEl = appEl.querySelector('.grid');
      if (gridEl) gridEl.innerHTML = this.renderAllGridItems(filtered);
    }
    const countEl = appEl.querySelector('.catalog-count');
    if (countEl) {
      countEl.textContent = `${filtered.length} ${filtered.length === 1 ? 'book' : 'books'}`;
    }
    const clearBtn = appEl.querySelector('.catalog-search-clear');
    if (clearBtn) {
      clearBtn.style.display = state.searchQuery ? '' : 'none';
    }
    const activeSrc = state.sourceFilter || 'all';
    appEl.querySelectorAll('.src-chip[data-act="set-source-filter"]').forEach(chip => {
      chip.classList.toggle('active', chip.dataset.src === activeSrc);
    });
  },
  renderSeriesView(items = null) {
    const sharedSeries = (typeof window !== 'undefined' && window.StoryDeckSeries) ? window.StoryDeckSeries : null;
    // Group books by series name, supporting cards belonging to multiple series/groups
    const groups = new Map();
    const sourceItems = items || state.catalog || [];
    sourceItems.forEach(item => {
      const seriesEntries = sharedSeries
        ? sharedSeries.extractItemSeriesList(item)
        : ((item.series_list && item.series_list.length > 0)
            ? item.series_list
            : (item.series ? [{ name: item.series, sequence: item.sequence }] : []));
      if (seriesEntries.length === 0) {
        const groupName = 'Standalone & Singles';
        if (!groups.has(groupName)) {
          groups.set(groupName, []);
        }
        groups.get(groupName).push({ ...item, groupSequence: null });
      } else {
        // Place card into every series/group it belongs to
        const seenGroupNames = new Set();
        seriesEntries.forEach(entry => {
          const groupName = (entry.name || '').trim();
          if (groupName && !seenGroupNames.has(groupName.toLowerCase())) {
            seenGroupNames.add(groupName.toLowerCase());
            if (!groups.has(groupName)) {
              groups.set(groupName, []);
            }
            const rawSeq = entry.order || entry.sequence_display || (entry.sequence != null ? String(entry.sequence) : '') || (item.sequence != null ? String(item.sequence) : '');
            const parsed = sharedSeries ? sharedSeries.parseSequenceOrder(rawSeq) : null;
            let gSeq = (parsed && parsed.hasNum) ? parsed.num : (entry.sequence != null ? entry.sequence : item.sequence);
            let gDisp = rawSeq || item.sequence_display || (gSeq != null ? String(gSeq) : null);
            if (gSeq == null && !gDisp) {
              const m = (item.title || '').match(/#(\d+(?:\.\d+)?)/);
              if (m) {
                gSeq = parseFloat(m[1]);
                gDisp = m[1];
              }
            }
            groups.get(groupName).push({
              ...item,
              groupSequence: gSeq,
              groupSequenceDisplay: gDisp
            });
          }
        });
      }
    });
    const shouldExcludeSingle = state.deviceConfig?.exclude_single_card_series !== false;
    // Filter out groups that have only 1 item if the setting is enabled
    const eligibleGroupNames = Array.from(groups.keys()).filter(name => {
      const count = (groups.get(name) || []).length;
      if (shouldExcludeSingle && count <= 1) {
        return false;
      }
      return true;
    });
    // Sort series groups alphabetically ignoring 'The', 'A', 'An' (with standalone last)
    const sortedGroupNames = eligibleGroupNames.sort((a, b) => {
      if (a === 'Standalone & Singles') return 1;
      if (b === 'Standalone & Singles') return -1;
      return this.cleanTitle(a).localeCompare(this.cleanTitle(b));
    });
    if (!sortedGroupNames.length) {
      return '<div style="padding:40px; color:#888; text-align:center;">No multi-book series found in this library.</div>';
    }
    return sortedGroupNames.map(groupName => {
      const items = groups.get(groupName);
      // Sort books within series by group-specific sequence ascending, then clean title
      items.sort((a, b) => {
        const seqA = a.groupSequence != null ? a.groupSequence : 999999;
        const seqB = b.groupSequence != null ? b.groupSequence : 999999;
        if (seqA !== seqB) {
          return seqA - seqB;
        }
        return this.cleanTitle(a.title).localeCompare(this.cleanTitle(b.title));
      });
      const tiles = items.map((c, index) => {
        let seqLabel = '';
        if (c.groupSequenceDisplay) {
          seqLabel = c.groupSequenceDisplay.startsWith('#') ? c.groupSequenceDisplay : `#${c.groupSequenceDisplay}`;
        } else if (c.groupSequence != null) {
          seqLabel = `#${c.groupSequence % 1 === 0 ? c.groupSequence : c.groupSequence.toFixed(1)}`;
        } else {
          seqLabel = `#${index + 1}`;
        }
        return `
          <div class="tile" data-card="${Utils.esc(c.id || c.cardId)}" title="${Utils.esc(c.title)}">
            <img loading="lazy" decoding="async" src="${Utils.esc(c.cover || c.img || '/static/img/placeholder.png')}" onerror="this.onerror=null;this.src='/static/img/placeholder.png'">
            ${seqLabel ? `<span class="badge-seq">${Utils.esc(seqLabel)}</span>` : ''}
            ${c.source === 'abs' || c.type === 'abs' ? '<span class="badge-abs">ABS</span>' : '<span class="badge-yoto">YOTO</span>'}
          </div>
        `;
      }).join('');
      return `
        <div class="series-section" data-series-name="${Utils.esc(groupName)}">
          <div class="series-header">
            <span class="series-title">${Utils.esc(groupName)}</span>
            <span class="series-count">(${items.length} ${items.length === 1 ? 'card' : 'cards'})</span>
          </div>
          <div class="series-row">
            ${tiles}
          </div>
        </div>
      `;
    }).join('');
  },
  render() {
    if (state.activeTab === 'series') {
      const filteredSeries = this.getFilteredCatalog();
      return `
        ${this.renderTopNav()}
        ${this.renderToolbar(filteredSeries.length)}
        <div class="grid-series">
          ${this.renderSeriesView(filteredSeries)}
        </div>
        ${PlayerScreen.renderNowbar()}
      `;
    }
    if (state.activeTab === 'recent') {
      const recentItems = (state.catalog || []).filter(c => c.daily || Boolean(c.last_played_at && String(c.last_played_at).trim()));
      recentItems.sort((a, b) => {
        if (Boolean(a.daily) !== Boolean(b.daily)) return a.daily ? -1 : 1;
        return String(b.last_played_at || '').localeCompare(String(a.last_played_at || ''));
      });
      const tiles = recentItems.slice(0, 24).map(c => `
        <div class="tile" data-card="${Utils.esc(c.id || c.cardId)}" title="${Utils.esc(c.title)}">
          <img loading="lazy" decoding="async" src="${Utils.esc(c.cover || c.img || '/static/img/placeholder.png')}" onerror="this.onerror=null;this.src='/static/img/placeholder.png'">
          ${c.source === 'abs' || c.type === 'abs' ? '<span class="badge-abs">ABS</span>' : '<span class="badge-yoto">YOTO</span>'}
        </div>
      `).join('');
      return `
        ${this.renderTopNav()}
        <div class="grid">
          ${tiles.length ? tiles : '<div style="padding:40px; color:#888; text-align:center;">No recently played audiobooks yet.</div>'}
        </div>
        ${PlayerScreen.renderNowbar()}
      `;
    }
    const filtered = this.getFilteredCatalog();
    return `
      ${this.renderTopNav()}
      ${this.renderToolbar(filtered.length)}
      <div class="grid">
        ${this.renderAllGridItems(filtered)}
      </div>
      ${PlayerScreen.renderNowbar()}
    `;
  }
};
