// ============================================================================
// StoryDeck Shared UI Utilities
// Loaded before page modules across Admin Console (/admin), Parent Portal (/),
// and Tablet Kiosk (/kiosk) so HTML escaping, toast alerts, time formatting,
// profile avatar fallbacks, and Yoto device status classification never drift.
// ============================================================================

(function (global) {
  "use strict";

  function escapeHtml(str) {
    if (str === null || str === undefined || str === "") return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function showToast(message, type = "info") {
    if (typeof document === "undefined" || !document.body) return;
    let container = document.getElementById("toastContainer");
    if (!container) {
      container = document.createElement("div");
      container.id = "toastContainer";
      container.className = "toast-container";
      container.style.cssText =
        "position: fixed; bottom: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; max-width: 340px;";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = "toast";
    const borderColors = {
      success: "rgba(16, 185, 129, 0.55)",
      warning: "rgba(245, 158, 11, 0.55)",
      danger: "rgba(239, 68, 68, 0.55)",
      error: "rgba(239, 68, 68, 0.55)",
      info: "rgba(59, 130, 246, 0.55)",
    };
    toast.style.borderLeft = `3px solid ${borderColors[type] || borderColors.info}`;
    toast.innerText = String(message || "");
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transition = "opacity 0.3s";
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }

  function formatSeconds(secs) {
    const s = Math.max(0, Math.floor(Number(secs) || 0));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${rem < 10 ? "0" : ""}${rem}`;
  }

  function formatDuration(secs) {
    const totalMin = Math.round(Math.max(0, Number(secs) || 0) / 60);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  function getDeviceStatusMeta(dev) {
    const online = Boolean(dev && dev.online);
    const isPlaying = Boolean(dev && dev.is_playing);
    const isAway = Boolean(online && dev && dev.is_home === false);
    if (isAway) {
      return {
        isAway: true,
        isPlaying,
        online,
        icon: "✈️",
        statusClass: "status-away",
        dotClass: "dot-away",
      };
    }
    if (isPlaying) {
      return {
        isAway: false,
        isPlaying: true,
        online,
        icon: "▶",
        statusClass: "status-playing",
        dotClass: "dot-play",
      };
    }
    if (online) {
      return {
        isAway: false,
        isPlaying: false,
        online: true,
        icon: "●",
        statusClass: "status-online",
        dotClass: "dot-on",
      };
    }
    return {
      isAway: false,
      isPlaying: false,
      online: false,
      icon: "○",
      statusClass: "status-offline",
      dotClass: "dot-off",
    };
  }

  function renderProfileAvatarHtml(profile, sizePx = 24) {
    const p = profile || {};
    const size = Number(sizePx) || 24;
    const label = String(p.display_name || "P");
    const initial = escapeHtml(p.avatar_initial || label.charAt(0).toUpperCase());
    const bg = escapeHtml(p.avatar_color || "#e5352b");
    if (p.avatar_url && String(p.avatar_url).includes("?t=")) {
      return `<img src="${escapeHtml(p.avatar_url)}" alt="${escapeHtml(label)}" style="width:${size}px;height:${size}px;border-radius:50%;object-fit:cover;" onerror="this.style.display='none'">`;
    }
    const fontSize = Math.max(10, Math.round(size * 0.5));
    return `<span style="display:inline-flex;align-items:center;justify-content:center;width:${size}px;height:${size}px;border-radius:50%;background:${bg};color:#fff;font-size:${fontSize}px;font-weight:800;flex-shrink:0;">${initial}</span>`;
  }

  global.StoryDeckUI = {
    escapeHtml,
    showToast,
    formatSeconds,
    formatDuration,
    getDeviceStatusMeta,
    renderProfileAvatarHtml,
  };

  // Expose top-level globals for classic non-module scripts
  global.escapeHtml = escapeHtml;
  global.showToast = showToast;
  global.formatSeconds = formatSeconds;
  global.formatDuration = formatDuration;
})(typeof window !== "undefined" ? window : globalThis);
