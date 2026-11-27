import { state } from './state.js';
import { BrokerApi } from './broker-api.js';

export function handleGrownupsChange(app, e) {
  const guLockSelect = e.target.closest('[data-act="gu-select-lock-player"]');
  if (guLockSelect) {
    const did = guLockSelect.value;
    const allPlayers = state.getAllAvailablePlayers();
    const found = allPlayers.find(p => p.id === did);
    if (found) {
      state.setActiveDevice(found);
      state.lockedPlayerId = found.id;
      state.persistParentSettings();
      app.syncActiveDevicePlayback();
    }
  }
}

export function handleGrownupsClick(app, e) {
  const guTabBtn = e.target.closest('[data-act="gu-tab"]');
  if (guTabBtn) {
    state.guActiveTab = guTabBtn.dataset.tab;
    app.render();
    return;
  }
  // Grown-ups Close Button
  const guClose = e.target.closest('[data-act="gu-close"]');
  if (guClose) {
    const returnScreen = state.previousScreen || (state.mode === 'standalone' ? 'catalog' : 'profile');
    state.previousScreen = null;
    state.setScreen(returnScreen);
    app.render();
    return;
  }
  // Grown-ups Mode Switch
  const guModeBtn = e.target.closest('[data-act="gu-set-mode"]');
  if (guModeBtn) {
    const newMode = guModeBtn.dataset.mode;
    state.mode = newMode;
    state.persistParentSettings();
    if (newMode === 'standalone') {
      // Switch directly to catalog without profile requirement
      const allPlayers = state.getAllAvailablePlayers();
      if (!state.activeDevice && allPlayers.length > 0) {
        state.setActiveDevice(allPlayers[0]);
      }
      if (state.profiles.length > 0) {
        BrokerApi.fetchLiveCatalog(state.profiles[0].id).then(() => {
          app.syncActiveDevicePlayback();
        });
      }
    }
    app.render();
    return;
  }
  // Grown-ups Lock Player Toggle
  const guLockBtn = e.target.closest('[data-act="gu-set-lock"]');
  if (guLockBtn) {
    state.lockPlayer = (guLockBtn.dataset.lock === 'true');
    if (state.lockPlayer && state.activeDevice) {
      state.lockedPlayerId = state.activeDevice.id;
    }
    state.persistParentSettings();
    app.render();
    return;
  }
  // Grown-ups Sleep Timer Duration
  const guSleepBtn = e.target.closest('[data-act="gu-set-sleep"]');
  if (guSleepBtn) {
    state.parentSettings.sleepTimerMin = parseInt(guSleepBtn.dataset.val, 10);
    state.persistParentSettings();
    app.checkAndArmSleepTimer(true);
    app.render();
    return;
  }
  // Grown-ups Sleep Timer Scope (Bedtime vs Always)
  const guSleepScopeBtn = e.target.closest('[data-act="gu-set-sleep-scope"]');
  if (guSleepScopeBtn) {
    state.parentSettings.sleepTimerMode = guSleepScopeBtn.dataset.val;
    state.persistParentSettings();
    app.checkAndArmSleepTimer(true);
    app.render();
    return;
  }
  // Grown-ups Remote/Travel Block Toggle
  const guRemoteBlockBtn = e.target.closest('[data-act="gu-set-remote-block"]');
  if (guRemoteBlockBtn) {
    state.blockRemotePlayback = (guRemoteBlockBtn.dataset.val === 'true');
    state.persistParentSettings();
    app.render();
    return;
  }
  // Grown-ups Screensaver Delay
  const guSaverBtn = e.target.closest('[data-act="gu-set-saver"]');
  if (guSaverBtn) {
    state.parentSettings.screensaverMin = parseInt(guSaverBtn.dataset.val, 10);
    state.persistParentSettings();
    app.render();
    return;
  }
  // Grown-ups Clock Size
  const guClockSizeBtn = e.target.closest('[data-act="gu-set-clock-size"]');
  if (guClockSizeBtn) {
    state.parentSettings.clockSize = parseInt(guClockSizeBtn.dataset.val, 10);
    state.persistParentSettings();
    app.render();
    return;
  }
  // Grown-ups Restart Action
  const guRestartBtn = e.target.closest('[data-act="gu-restart"]');
  if (guRestartBtn) {
    if (confirm("Restart this controller?")) {
      window.location.reload();
    }
    return;
  }
  // Grown-ups Re-pair Action
  const guRepairBtn = e.target.closest('[data-act="gu-re-pair"]');
  if (guRepairBtn) {
    state.deviceToken = null;
    state.setScreen('profile');
    app.bootstrap();
    return;
  }
  // 4b. Inspect Active Card (Tap Cover or Title in Player Screen)
  return false;
}

export function bindGrownupsInputs(app) {
  // Parent volume ceiling slider removed - volume limits are dynamically queried per-device from Yoto
  const bedtimeInput = document.getElementById('guBedtimeInput');
  if (bedtimeInput) {
    bedtimeInput.addEventListener('change', (e) => {
      state.parentSettings.bedtime = e.target.value;
      state.persistParentSettings();
    });
  }
  const wakeInput = document.getElementById('guWakeInput');
  if (wakeInput) {
    wakeInput.addEventListener('change', (e) => {
      state.parentSettings.wakeTime = e.target.value;
      state.persistParentSettings();
    });
  }
  const brightnessSlider = document.getElementById('guBrightnessSlider');
  const brightnessLabel = document.getElementById('guBrightnessLabel');
  if (brightnessSlider) {
    brightnessSlider.addEventListener('input', (e) => {
      const val = parseInt(e.target.value, 10);
      state.parentSettings.brightness = val;
      if (brightnessLabel) brightnessLabel.textContent = `${val}%`;
    });
    brightnessSlider.addEventListener('change', (e) => {
      state.persistParentSettings();
    });
  }
}
