import { state } from './state.js';
import { BrokerApi } from './broker-api.js';
import { handleGrownupsChange, handleGrownupsClick, bindGrownupsInputs } from './events_grownups.js';
import { handlePlayerAction, bindPlayerScrubbing } from './events_player.js';

export const eventsMixin = {
  bindEvents() {
    bindPlayerScrubbing(this);
    this.container.addEventListener('change', (e) => {
      handleGrownupsChange(this, e);
    });
    this.container.addEventListener('click', (e) => {
      // 0. Dismiss screensaver on any tap/click
      if (state.screen === 'screensaver') {
        this.exitScreensaver();
        return;
      }
      // 1. Profile Card Selection
      const card = e.target.closest('[data-profile]');
      if (card) {
        this.selectProfile(card.dataset.profile);
        return;
      }
      // 2. Profile Switcher Pill
      const switcher = e.target.closest('[data-act="switch-profile"]');
      if (switcher) {
        state.setScreen('profile');
        this.render();
        return;
      }
      // 2b. Device Picker Toggle
      const devToggle = e.target.closest('[data-act="toggle-device-picker"]');
      if (devToggle) {
        state.toggleDevicePicker();
        this.render();
        return;
      }
      // 2c. Target Device Selection
      const devOpt = e.target.closest('[data-act="select-device"]');
      if (devOpt) {
        const did = devOpt.dataset.deviceId;
        const pool = state.mode === 'standalone'
          ? state.getAllAvailablePlayers()
          : (state.activeProfile?.players || state.getAllAvailablePlayers());
        const target = pool.find(p => p.id === did);
        if (target) {
          state.setActiveDevice(target);
          this.syncActiveDevicePlayback();
          this.render();
          if (state.mode === 'standalone') {
            BrokerApi.fetchLiveCatalog(null, target.id).then(() => {
              this.syncActiveDevicePlayback();
              this.render();
            });
          }
        }
        return;
      }
      // Close device picker if clicking elsewhere
      if (state.isDevicePickerOpen && !e.target.closest('.device-pill-wrap')) {
        state.isDevicePickerOpen = false;
        this.render();
      }
      // 3. Catalog Tab Navigation (All / Series / Recent)
      const tab = e.target.closest('.tab-btn[data-tab]');
      if (tab && state.screen === 'catalog') {
        state.setActiveTab(tab.dataset.tab);
        this.render();
        return;
      }
      // 3b. Series Badge Jump from Detail Screen
      const seriesJumpBtn = e.target.closest('.series-badge-btn[data-series]');
      if (seriesJumpBtn) {
        const targetSeries = (seriesJumpBtn.dataset.series || '').trim().toLowerCase();
        state.setActiveTab('series');
        state.setScreen('catalog');
        this.render();
        if (targetSeries) {
          const sec = Array.from(this.container.querySelectorAll('.series-section[data-series-name]'))
            .find(el => (el.dataset.seriesName || '').trim().toLowerCase() === targetSeries);
          if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        return;
      }
      // 4. Media Card Selection (Catalog Grid)
      const tile = e.target.closest('[data-card]');
      if (tile) {
        const id = tile.dataset.card;
        const found = state.catalog.find(c => (c.id || c.cardId) === id);
        if (found) {
          state.setPreviewCard(found);
          // If no active card is playing, also set state.card
          if (!state.card || !state.isPlaying) {
            state.setCard(found);
          }
          state.setScreen('detail');
          this.render();
          // Fetch real tracklist / chapters from Broker if not yet cached on the card
          if (!found.chapters || found.chapters.length === 0) {
            BrokerApi.fetchMediaTracks(id).then(tracks => {
              if (tracks && tracks.length > 0) {
                found.chapters = tracks.map(t => ({
                  key: t.key || '',
                  track_key: t.track_key || t.key || '',
                  title: t.title,
                  duration: t.duration_seconds || 0,
                  icon_url: t.icon_url
                }));
                found.duration = tracks.reduce((sum, t) => sum + (t.duration_seconds || 0), 0);
                if (state.card && (state.card.id || state.card.cardId) === id) {
                  state.card = { ...found };
                  this.render();
                }
              }
            });
          }
        }
        return;
      }
      // Change PIN button inside Grown-ups screen
      const guChangePin = e.target.closest('[data-act="gu-change-pin"]');
      if (guChangePin) {
        state.previousScreen = 'grownups';
        state.currentEnteredPin = '';
        state.isPinError = false;
        state.pinMode = 'change';
        state.setScreen('pin');
        this.render();
        return;
      }
      // PIN Cancel Button
      const pinCancel = e.target.closest('[data-act="pin-cancel"]');
      if (pinCancel) {
        const returnScreen = state.previousScreen || (state.mode === 'standalone' ? 'catalog' : 'profile');
        state.previousScreen = null;
        state.currentEnteredPin = '';
        state.isPinError = false;
        state.setScreen(returnScreen);
        this.render();
        return;
      }
      // PIN Keypad Tap
      const pinKeyBtn = e.target.closest('.pin-key');
      if (pinKeyBtn && state.screen === 'pin') {
        const key = pinKeyBtn.dataset.key;
        if (key === 'clear') {
          state.currentEnteredPin = '';
          state.isPinError = false;
          this.render();
          return;
        }
        if (key === 'back') {
          state.currentEnteredPin = (state.currentEnteredPin || '').slice(0, -1);
          state.isPinError = false;
          this.render();
          return;
        }
        if (key != null && (state.currentEnteredPin || '').length < 4) {
          state.currentEnteredPin = (state.currentEnteredPin || '') + key;
          state.isPinError = false;
          this.render();
          // Check if 4 digits reached
          if (state.currentEnteredPin.length === 4) {
            const entered = state.currentEnteredPin;
            setTimeout(() => {
              if (state.pinMode === 'unlock') {
                if (entered === state.parentPin) {
                  state.currentEnteredPin = '';
                  state.failedPinAttempts = 0;
                  state.setScreen('grownups');
                  this.render();
                } else {
                  state.isPinError = true;
                  state.currentEnteredPin = '';
                  state.failedPinAttempts = (state.failedPinAttempts || 0) + 1;
                  
                  if (state.failedPinAttempts >= 3) {
                    state.pinLockoutUntil = Date.now() + 10000; // 10 second penalty delay
                    this.render();
                    const lockoutInterval = setInterval(() => {
                      if (Date.now() >= state.pinLockoutUntil) {
                        clearInterval(lockoutInterval);
                        state.pinLockoutUntil = 0;
                        state.failedPinAttempts = 0;
                        state.isPinError = false;
                        this.render();
                      } else {
                        this.render();
                      }
                    }, 1000);
                  } else {
                    this.render();
                    setTimeout(() => {
                      state.isPinError = false;
                      this.render();
                    }, 400);
                  }
                }
              } else if (state.pinMode === 'setup') {
                state.tempSetupPin = entered;
                state.currentEnteredPin = '';
                state.pinMode = 'confirm';
                this.render();
              } else if (state.pinMode === 'confirm') {
                if (entered === state.tempSetupPin) {
                  state.parentPin = entered;
                  state.persistParentSettings();
                  state.tempSetupPin = '';
                  state.currentEnteredPin = '';
                  state.setScreen('grownups');
                  this.render();
                } else {
                  state.isPinError = true;
                  state.currentEnteredPin = '';
                  this.render();
                  setTimeout(() => {
                    state.pinMode = 'setup';
                    state.isPinError = false;
                    this.render();
                  }, 500);
                }
              } else if (state.pinMode === 'change') {
                if (entered === state.parentPin) {
                  state.currentEnteredPin = '';
                  state.pinMode = 'setup';
                  this.render();
                } else {
                  state.isPinError = true;
                  state.currentEnteredPin = '';
                  this.render();
                  setTimeout(() => {
                    state.isPinError = false;
                    this.render();
                  }, 400);
                }
              }
            }, 100);
          }
        }
        return;
      }
      // Grown-ups Tab Navigation
      if (handleGrownupsClick(this, e) !== false) {
        return;
      }
      const inspectCard = e.target.closest('[data-act="inspect-card"]');
      if (inspectCard) {
        if (state.card) {
          state.setPreviewCard(state.card);
          state.setScreen('detail');
          this.render();
          // Fetch tracks if needed
          const id = state.card.id || state.card.cardId;
          if (id && (!state.card.chapters || state.card.chapters.length === 0)) {
            BrokerApi.fetchMediaTracks(id).then(tracks => {
              if (tracks && tracks.length > 0) {
                state.card.chapters = tracks.map(t => ({
                  key: t.key || '',
                  track_key: t.track_key || t.key || '',
                  title: t.title,
                  duration: t.duration_seconds || 0,
                  icon_url: t.icon_url
                }));
                state.card.duration = tracks.reduce((sum, t) => sum + (t.duration_seconds || 0), 0);
                state.previewCard = { ...state.card };
                this.render();
              }
            });
          }
        }
        return;
      }
      // 5. Quick Action Button inside Nowbar (play/pause toggle should NOT navigate)
      const nbPlayBtn = e.target.closest('.nb-play');
      if (nbPlayBtn) {
        const nextState = !state.isPlaying;
        state.setPlaying(nextState);
        this.render();
        const targetAction = nextState ? 'resume' : 'pause';
        BrokerApi.sendPlayerControl(targetAction).then(res => {
          if (!res || !res.ok) {
            console.warn(`Failed to execute ${targetAction}, reverting UI state:`, res);
            state.setPlaying(!nextState);
            this.render();
          }
        });
        return;
      }
      // 5b. Back / Screen Navigation Links
      const go = e.target.closest('[data-go]');
      if (go) {
        state.setScreen(go.dataset.go);
        this.render();
        return;
      }
      // 6. Track / Chapter Row Tap -> Play
      const chrow = e.target.closest('[data-ch]');
      if (chrow) {
        const chIdx = +chrow.dataset.ch;
        const targetCard = state.previewCard || state.card;
        this.initiatePlayback(targetCard, chIdx, chrow);
        return;
      }
      // 7. Player Action Controls
      if (handlePlayerAction(this, e) !== false) {
        return;
      }
      const gearBtn = e.target.closest('[data-act="gear-button"]');
      if (gearBtn) {
        e.preventDefault();
        e.stopPropagation();
        state.previousScreen = state.screen;
        state.currentEnteredPin = '';
        state.isPinError = false;
        state.pinMode = !state.parentPin ? 'setup' : 'unlock';
        state.setScreen('pin');
        this.render();
        return;
      }
    });
  },

  bindGrownUpsEvents() {
    bindGrownupsInputs(this);
  }
};
