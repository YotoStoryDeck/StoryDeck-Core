import { state } from './state.js';
import { Utils } from './config.js';
import { BrokerApi } from './broker-api.js';
import { PlayerScreen } from './screens/player.js';

function _getPlayerDuration() {
  const ch = (state.card?.chapters || [])[state.chapterIndex] || {};
  return state.activeDevice?.duration || state.card?.duration || ch.duration || 1800;
}

function _computeBarTargetSec(barWrapEl, clientX) {
  const barEl = barWrapEl.querySelector('.bar') || barWrapEl;
  const rect = barEl.getBoundingClientRect();
  const ratio = rect.width > 0 ? Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) : 0;
  const dur = _getPlayerDuration();
  return { ratio, dur, targetSec: Math.round(ratio * dur) };
}

function _computeVolValue(volBarEl, clientX) {
  const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
  const rect = volBarEl.getBoundingClientRect();
  const ratio = rect.width > 0 ? Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) : 0;
  const vol = Math.min(maxLimit, Math.round(ratio * 100));
  return { vol, maxLimit };
}

export function bindPlayerScrubbing(app) {
  if (!app?.container || app._scrubBound) return;
  app._scrubBound = true;

  let activeDrag = null;
  let rafId = null;
  let pendingClientX = 0;

  const paintDrag = () => {
    rafId = null;
    if (!activeDrag) return;
    const { type, target, npEl } = activeDrag;
    if (type === 'seek') {
      const { ratio, dur, targetSec } = _computeBarTargetSec(target, pendingClientX);
      activeDrag.lastValue = targetSec;
      const pct = ratio * 100;
      const fill = npEl.querySelector('.bar i');
      const thumb = npEl.querySelector('.bar b');
      if (fill) fill.style.width = `${pct}%`;
      if (thumb) thumb.style.left = `${pct}%`;
      const spans = npEl.querySelectorAll('.times span');
      if (spans[0]) spans[0].textContent = Utils.mmss(targetSec);
      if (spans[1]) spans[1].textContent = `-${Utils.mmss(Math.max(0, dur - targetSec))}`;
    } else if (type === 'vol') {
      const { vol, maxLimit } = _computeVolValue(target, pendingClientX);
      activeDrag.lastValue = vol;
      const fill = npEl.querySelector('.volbar i');
      const thumb = npEl.querySelector('.volbar-thumb');
      const volText = npEl.querySelector('.vol-text');
      if (fill) fill.style.width = `${vol}%`;
      if (thumb) thumb.style.left = `${vol}%`;
      if (volText) volText.textContent = `${vol}%${maxLimit < 100 ? ` (Max ${maxLimit}%)` : ''}`;
    }
  };

  app.container.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0 && e.pointerType === 'mouse') return;
    const seekEl = e.target.closest('.bar-wrap[data-act="seek-bar"]');
    const volEl = e.target.closest('.volbar[data-act="vol-set"]');
    const target = seekEl || volEl;
    if (!target) return;
    const npEl = target.closest('.np');
    if (!npEl) return;

    const type = seekEl ? 'seek' : 'vol';
    try { target.setPointerCapture?.(e.pointerId); } catch (_) {}
    if (type === 'seek') npEl.dataset.scrubbing = '1';
    else npEl.dataset.volDragging = '1';

    activeDrag = { type, target, npEl, pointerId: e.pointerId, lastValue: null };
    pendingClientX = e.clientX;
    paintDrag();
  });

  app.container.addEventListener('pointermove', (e) => {
    if (!activeDrag || e.pointerId !== activeDrag.pointerId) return;
    pendingClientX = e.clientX;
    if (!rafId) rafId = requestAnimationFrame(paintDrag);
  });

  const finishDrag = (e) => {
    if (!activeDrag || (e.pointerId !== undefined && e.pointerId !== activeDrag.pointerId)) return;
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    if (e.clientX !== undefined) pendingClientX = e.clientX;
    paintDrag();

    const { type, target, npEl, lastValue } = activeDrag;
    activeDrag = null;
    if (type === 'seek') delete npEl.dataset.scrubbing;
    else delete npEl.dataset.volDragging;

    target.dataset.justDragged = '1';
    setTimeout(() => { if (target) delete target.dataset.justDragged; }, 150);

    if (e.type === 'pointercancel' || lastValue == null) {
      PlayerScreen.tick();
      return;
    }

    if (type === 'seek') {
      if (state.activeDevice) state.activeDevice.position = lastValue;
      PlayerScreen.tick();
      BrokerApi.sendPlayerControl('seek', { position: lastValue });
    } else if (type === 'vol') {
      state.volume = lastValue;
      if (state.activeDevice) state.activeDevice.volume = lastValue;
      PlayerScreen.tick();
      BrokerApi.sendPlayerControl('volume', { volume: lastValue });
    }
  };

  app.container.addEventListener('pointerup', finishDrag);
  app.container.addEventListener('pointercancel', finishDrag);
}

export function handlePlayerAction(app, e) {
  const actBtn = e.target.closest('[data-act]');
  const act = actBtn?.dataset.act;
  if (act) {
    // Prevent clicking nowbar play from opening the now screen
    if (act === 'toggle' && actBtn.classList.contains('nb-play')) {
      e.stopPropagation();
    }
    if (act === 'playstart') {
      const btn = document.getElementById('btn-play-book') || actBtn;
      const targetCard = state.previewCard || state.card;
      if (state.isCardCurrentlyPlaying(targetCard)) {
        state.setScreen('now');
        app.render();
        return;
      }
      // Pass null for chapterIndex so 'Play Book' resumes where the profile/player left off
      app.initiatePlayback(targetCard, null, btn);
      return;
    }
    if (act === 'toggle') {
      const nextState = !state.isPlaying;
      state.setPlaying(nextState);
      app.render();
      const targetAction = nextState ? 'resume' : 'pause';
      BrokerApi.sendPlayerControl(targetAction).then(res => {
        if (!res || !res.ok) {
          console.warn(`Failed to execute ${targetAction}, reverting UI state:`, res);
          state.setPlaying(!nextState);
          app.render();
        }
      });
      return;
    }
    if (act === 'next') {
      BrokerApi.sendPlayerControl('next').then(res => {
        if (res && res.ok) {
          if (state.card && state.chapterIndex < (state.card.chapters || []).length - 1) {
            state.setChapter(state.chapterIndex + 1);
          }
          BrokerApi.getDeviceConfig().then(() => {
            app.syncActiveDevicePlayback();
            app.render();
          });
        } else {
          console.warn('Next track error:', res);
        }
      });
      return;
    }
    if (act === 'prev') {
      BrokerApi.sendPlayerControl('prev').then(res => {
        if (res && res.ok) {
          if (res.data?.track) {
            state.setChapter(Math.max(0, res.data.track - 1));
          } else if (state.chapterIndex > 0) {
            state.setChapter(state.chapterIndex - 1);
          }
          BrokerApi.getDeviceConfig().then(() => {
            app.syncActiveDevicePlayback();
            app.render();
          });
        } else {
          console.warn('Prev track error:', res);
        }
      });
      return;
    }
    if (act === 'back30' || act === 'fwd30') {
      const dur = _getPlayerDuration();
      const curPos = state.activeDevice?.position || 0;
      const delta = act === 'back30' ? -30 : 30;
      const targetPos = Math.max(0, Math.min(dur, Math.round(curPos + delta)));
      if (state.activeDevice) state.activeDevice.position = targetPos;
      if (!PlayerScreen.tick()) app.render();
      BrokerApi.sendPlayerControl(act).then(res => {
        if (res && res.ok) {
          BrokerApi.getDeviceConfig().then(() => {
            app.syncActiveDevicePlayback();
            app.updateLiveTelemetryUI(state.screen);
          }).catch(() => {});
        } else {
          console.warn(`${act} skip error:`, res);
        }
      });
      return;
    }
    if (act === 'seek-bar') {
      if (actBtn.dataset.justDragged === '1') return;
      const { targetSec } = _computeBarTargetSec(actBtn, e.clientX);
      if (state.activeDevice) state.activeDevice.position = targetSec;
      if (!PlayerScreen.tick()) app.render();
      BrokerApi.sendPlayerControl('seek', { position: targetSec });
      return;
    }
    if (act === 'vol-down') {
      const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
      const cur = state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50);
      const newVol = Math.max(0, Math.min(maxLimit, cur - 10));
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      if (state.screen !== 'now' || !PlayerScreen.tick()) app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
    if (act === 'vol-up') {
      const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
      const cur = state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50);
      const newVol = Math.min(maxLimit, cur + 10);
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      if (state.screen !== 'now' || !PlayerScreen.tick()) app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
    if (act === 'vol-set') {
      if (actBtn.dataset.justDragged === '1') return;
      const { vol: newVol } = _computeVolValue(actBtn, e.clientX);
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      if (state.screen !== 'now' || !PlayerScreen.tick()) app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
  }
  return false;
}
