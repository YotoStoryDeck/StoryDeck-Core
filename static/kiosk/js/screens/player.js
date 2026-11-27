import { state } from '../state.js';
import { Utils } from '../config.js';

/**
 * Screen 3: Now Playing / Player Screen Component
 */
export const PlayerScreen = {
  renderNowbar() {
    const dev = state.activeDevice;
    const devName = dev?.name || state.activeProfile?.display_name || 'Player';
    const isOnline = Boolean(dev?.online);
    const isPlaying = Boolean(state.isPlaying || dev?.is_playing);

    const _ui = (typeof window !== 'undefined' && window.StoryDeckUI) ? window.StoryDeckUI : null;
    const devForMeta = { ...dev, online: isOnline, is_playing: isPlaying };
    const statusMeta = _ui ? _ui.getDeviceStatusMeta(devForMeta) : {
      isAway: isOnline && dev?.is_home === false,
      isPlaying,
      online: isOnline,
    };
    const isAway = statusMeta.isAway;
    let dotStyle = 'background:#9ca3af; box-shadow:none;';
    if (isAway) {
      dotStyle = 'background:#ea580c; box-shadow:0 0 8px #ea580c;';
    } else if (isPlaying) {
      dotStyle = 'background:#e5352b; box-shadow:0 0 8px #e5352b;';
    } else if (isOnline) {
      dotStyle = 'background:#16a34a; box-shadow:0 0 8px #16a34a;';
    }

    if (state.card && (isPlaying || dev?.has_card)) {
      const ch = (state.card.chapters || [])[state.chapterIndex] || {};
      const trackTitle = state.card.chapter_title || ch.title || 'Playing';
      const author = state.card.author || '';
      const bookTitle = state.card.title || 'Now Playing';

      // Format bottom line as "Author • Chapter Title"
      let bottomLine = Utils.esc(trackTitle);
      if (author && trackTitle) {
        bottomLine = `${Utils.esc(author)} &bull; ${Utils.esc(trackTitle)}`;
      } else if (author) {
        bottomLine = Utils.esc(author);
      }

      return `
        <div class="nowbar" data-go="now">
          <div class="status" title="${isOnline ? (isPlaying ? 'Playing' : 'Online') : 'Offline'}">
            <span class="dot" style="${dotStyle}"></span>
            ${Utils.esc(devName)}${isAway ? ' <span style="font-size:11px;">✈️ AWAY</span>' : ''}
          </div>
          <img class="nb-thumb" decoding="async" src="${Utils.esc(state.card.cover || state.card.img || '/static/img/placeholder.png')}" onerror="this.onerror=null;this.src='/static/img/placeholder.png'">
          <div class="nb-txt">
            <div class="nb-title">${Utils.esc(bookTitle)}</div>
            <div class="nb-sub">${bottomLine}</div>
          </div>
          <div class="nb-play" data-act="toggle">${isPlaying ? '&#10074;&#10074;' : '&#9654;'}</div>
        </div>
      `;
    }
    return `
      <div class="nowbar">
        <div class="status" title="${isOnline ? 'Online' : 'Offline'}">
          <span class="dot" style="${dotStyle}"></span>
          ${Utils.esc(devName)}
        </div>
        <div class="nb-mini">&#9835;</div>
        <div class="nb-txt muted">Nothing playing &mdash; tap a book to listen</div>
      </div>
    `;
  },

  tick() {
    const el = document.querySelector('#app .np');
    const c = state.card;
    if (!el || !c) return false;
    const cardId = String(c.id || c.cardId || '');
    if (el.dataset.cardId !== cardId) return false;

    const ch = (c.chapters || [])[state.chapterIndex] || {};
    const trackTitle = c.chapter_title || ch.title || 'Playing';
    if ((el.dataset.chapterTitle || '') !== trackTitle) return false;

    const dur = state.activeDevice?.duration || c.duration || ch.duration || 1800;
    const pos = Math.min(state.activeDevice?.position || 0, dur);
    const pct = dur > 0 ? Math.min(100, Math.max(0, (pos / dur) * 100)) : 0;
    const devMaxVol = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
    const vol = Math.min(devMaxVol, state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50));

    const barFill = el.querySelector('.bar i');
    const barThumb = el.querySelector('.bar b');
    const timesEl = el.querySelector('.times');
    if (el.dataset.scrubbing !== '1') {
      if (barFill) barFill.style.width = `${pct}%`;
      if (barThumb) barThumb.style.left = `${pct}%`;

      if (timesEl) {
        const spans = timesEl.querySelectorAll('span');
        if (spans[0]) spans[0].textContent = Utils.mmss(pos);
        if (spans[1]) spans[1].textContent = `-${Utils.mmss(dur - pos)}`;
      }
    }

    const toggleBtn = el.querySelector('[data-act="toggle"]');
    if (toggleBtn) {
      toggleBtn.innerHTML = state.isPlaying ? '&#10074;&#10074;' : '&#9654;';
      toggleBtn.title = state.isPlaying ? 'Pause' : 'Play';
    }

    const volFill = el.querySelector('.volbar i');
    const volThumb = el.querySelector('.volbar-thumb');
    const volText = el.querySelector('.vol-text');
    if (el.dataset.volDragging !== '1') {
      if (volFill) volFill.style.width = `${vol}%`;
      if (volThumb) volThumb.style.left = `${vol}%`;
      if (volText) volText.textContent = `${vol}%${devMaxVol < 100 ? ` (Max ${devMaxVol}%)` : ''}`;
    }

    const topEl = el.querySelector('.nptop');
    if (topEl) {
      let chip = topEl.querySelector('.sleep-chip');
      if (state.sleepTimerRemainingSec > 0) {
        const label = `Zz ${Math.ceil(state.sleepTimerRemainingSec / 60)}m`;
        if (!chip) {
          chip = document.createElement('span');
          chip.className = 'sleep-chip';
          chip.title = 'Sleep timer active';
          topEl.appendChild(chip);
        }
        chip.textContent = label;
      } else if (chip) {
        chip.remove();
      }
    }
    return true;
  },

  render() {
    const c = state.card;
    if (!c) {
      return `
        <div class="frame" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px;">
          <div style="font-size:42px;">📻</div>
          <div style="font-size:18px; font-weight:600; color:#4a453e;">Nothing is currently playing.</div>
          <button class="gu-pill-btn active" data-go="catalog" style="padding:10px 20px;">&#8592; Back to Library</button>
        </div>
      `;
    }

    const ch = (c.chapters || [])[state.chapterIndex] || {};
    const dur = state.activeDevice?.duration || c.duration || ch.duration || 1800;
    const pos = Math.min(state.activeDevice?.position || 0, dur);
    const pct = dur > 0 ? Math.min(100, Math.max(0, (pos / dur) * 100)) : 0;
    const devMaxVol = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
    const vol = Math.min(devMaxVol, state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50));

    const trackTitle = c.chapter_title || ch.title || 'Playing';
    const author = c.author || '';

    // Format subtitle as "Author • Chapter Title"
    let subLine = Utils.esc(trackTitle);
    if (author && trackTitle) {
      subLine = `${Utils.esc(author)} &bull; ${Utils.esc(trackTitle)}`;
    } else if (author) {
      subLine = Utils.esc(author);
    }

    return `
      <div class="np" data-card-id="${Utils.esc(c.id || c.cardId || '')}" data-chapter-title="${Utils.esc(trackTitle)}">
        <div class="nptop">
          <div class="np-back-btn" data-go="catalog" title="Return to Library">&#8592; Back</div>
          <span class="label">Playing on ${Utils.esc(state.activeDevice?.name || state.activeProfile?.display_name || 'Yoto')}</span>
          ${state.sleepTimerRemainingSec > 0 ? `
            <span class="sleep-chip" title="Sleep timer active">Zz ${Math.ceil(state.sleepTimerRemainingSec / 60)}m</span>
          ` : ''}
        </div>
        <div class="npmid">
          <img class="npcover clickable-card" decoding="async" data-act="inspect-card" src="${Utils.esc(c.cover || c.img)}" title="View book details and tracklist">
          <div class="npinfo">
            <div class="t clickable-card" data-act="inspect-card" title="View book details and tracklist">${Utils.esc(c.title)}</div>
            <div class="c">${subLine}</div>
            <div class="bar-wrap" data-act="seek-bar" title="Seek playback position">
              <div class="bar">
                <i style="width: ${pct}%;"></i>
                <b style="left: ${pct}%;"></b>
              </div>
            </div>
            <div class="times"><span>${Utils.mmss(pos)}</span><span>-${Utils.mmss(dur - pos)}</span></div>
            <div class="ctrls">
              <span class="ic" data-act="prev" title="Previous Chapter">&#9198;</span>
              <button type="button" class="ic-skip" data-act="back30" title="Back 30 seconds">-30s</button>
              <div class="big" data-act="toggle" title="${state.isPlaying ? 'Pause' : 'Play'}">${state.isPlaying ? '&#10074;&#10074;' : '&#9654;'}</div>
              <button type="button" class="ic-skip" data-act="fwd30" title="Forward 30 seconds">+30s</button>
              <span class="ic" data-act="next" title="Next Chapter">&#9197;</span>
              <span class="vol">
                <span class="vol-btn" data-act="vol-down" title="Volume Down">&#128264;</span>
                <div class="volbar-wrap">
                  <div class="volbar" data-act="vol-set" title="Set Volume (Max: ${devMaxVol}%)">
                    <i style="width: ${vol}%;"></i>
                    <b class="volbar-thumb" style="left: ${vol}%;"></b>
                    ${devMaxVol < 100 ? `<div class="volbar-limit" style="width:${100 - devMaxVol}%;" title="Parent Limit: ${devMaxVol}%"></div>` : ''}
                  </div>
                </div>
                <span class="vol-btn" data-act="vol-up" title="Volume Up">&#128266;</span>
                <span class="vol-text">${vol}%${devMaxVol < 100 ? ` (Max ${devMaxVol}%)` : ''}</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    `;
  }
};
