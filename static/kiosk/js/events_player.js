import { state } from './state.js';
import { BrokerApi } from './broker-api.js';

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
      const isSameActive = Boolean(
        state.isPlaying && state.card && targetCard && (
          (targetCard.id && (state.card.id === targetCard.id || state.card.cardId === targetCard.id)) ||
          (targetCard.title && state.card.title === targetCard.title)
        )
      );
      if (isSameActive) {
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
    if (act === 'vol-down') {
      const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
      const cur = state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50);
      const newVol = Math.max(0, Math.min(maxLimit, cur - 10));
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
    if (act === 'vol-up') {
      const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
      const cur = state.volume != null ? state.volume : (state.activeDevice?.volume != null ? state.activeDevice.volume : 50);
      const newVol = Math.min(maxLimit, cur + 10);
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
    if (act === 'vol-set') {
      const maxLimit = (state.activeDevice && state.activeDevice.volume_max != null) ? state.activeDevice.volume_max : 100;
      const rect = actBtn.getBoundingClientRect();
      const clickX = e.clientX - rect.left;
      const ratio = Math.max(0, Math.min(1, clickX / rect.width));
      const rawVol = Math.round(ratio * 100);
      const newVol = Math.min(maxLimit, rawVol);
      state.volume = newVol;
      if (state.activeDevice) state.activeDevice.volume = newVol;
      app.render();
      BrokerApi.sendPlayerControl('volume', { volume: newVol });
      return;
    }
    if (act === 'gear-button') {
  }
  return false;
}
