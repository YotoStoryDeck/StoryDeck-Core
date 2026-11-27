import { state } from '../state.js';
import { Utils } from '../config.js';

/**
 * Screen 1: Profile Selection Screen Component
 * Renders the avatar grid with scrolling support, friendly player names,
 * and a top-right Grown-Ups gear button (Tap -> PIN -> Grown-Ups Settings).
 */
export const ProfileScreen = {
  getTimeGreeting() {
    const hour = new Date().getHours();
    if (hour >= 5 && hour < 12) {
      return 'WHO IS LISTENING THIS MORNING?';
    } else if (hour >= 12 && hour < 17) {
      return 'WHO IS LISTENING THIS AFTERNOON?';
    } else {
      return 'WHO IS LISTENING TONIGHT?';
    }
  },

  render() {
    const greeting = this.getTimeGreeting();
    const _ui = (typeof window !== 'undefined' && window.StoryDeckUI) ? window.StoryDeckUI : null;
    const cards = state.profiles.map(p => {
      const avatarHtml = _ui
        ? _ui.renderProfileAvatarHtml(p, 88)
        : `<img class="profile-avatar" src="${Utils.esc(p.avatar_url || '/static/img/placeholder.png')}" onerror="this.onerror=null;this.src='/static/img/placeholder.png'">`;
      return `
        <div class="profile-card" data-profile="${Utils.esc(p.id)}">
          <div class="profile-avatar" style="display:flex;align-items:center;justify-content:center;overflow:hidden;">
            ${avatarHtml}
          </div>
          <div class="profile-name">${Utils.esc(p.display_name)}</div>
        </div>
      `;
    }).join('');

    return `
      <div class="profile-screen">
        <!-- Grown-Ups Screen Gear -->
        <button class="nav-gear-btn" data-act="gear-button" title="Grown-ups settings" style="position:absolute; top:14px; right:18px; z-index:20;">&#9881;</button>
        <div class="profile-header">
          <div class="profile-title">${greeting}</div>
          <div class="profile-subtitle">Tap your profile avatar to explore your audiobooks</div>
        </div>
        <div class="profiles-scroll">
          <div class="profiles-row">
            ${cards}
          </div>
        </div>
      </div>
    `;
  }
};
