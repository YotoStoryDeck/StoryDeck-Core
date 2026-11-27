/* ==============================================================================
   YOTO & AUDIOBOOKSHELF CONTROLLER ADMIN PORTAL (ES6 SPA CLIENT)
   Handles auth lifecycle, profile CRUD, curation matrix, hardware, diagnostics.
   ============================================================================== */

let currentUser = null;
let currentProfiles = {};
let currentMatrix = { items: [], profiles: [], shared_rules: {} };
let currentDevices = [];
let cachedAbsServerTags = [];
let configuredDefaultChildTags = ["Children"];
let selectedMatrixItemIds = new Set();
let lastCheckedMatrixIndex = -1;

const AVATAR_PALETTE = [
  "#3b82f6",  // Blue
  "#ef4444",  // Red
  "#10b981",  // Emerald
  "#f59e0b",  // Amber
  "#8b5cf6",  // Purple
  "#ec4899",  // Pink
  "#06b6d4",  // Cyan
  "#f97316",  // Orange
  "#14b8a6",  // Teal
  "#6366f1"   // Indigo
];

// Lifecycle Boot
document.addEventListener("DOMContentLoaded", () => {
  checkAuthStatus();
});

window.currentSsoMeta = {};
let initialDataLoaded = false;

window.addEventListener("storydeck:core-status", (e) => {
  if (e && e.detail && e.detail.recovered) {
    showToast("StoryDeck Core server reconnected.", "success");
    checkAuthStatus();
  }
});

// Global helper to populate ABS server tag dropdowns across views and modals
function populateAbsServerTagsDropdowns() {
  const settingsSelect = document.getElementById("absServerTagsDropdown");
  if (settingsSelect) {
    settingsSelect.innerHTML = '<option value="">-- Choose from Audiobookshelf tags --</option>';
    cachedAbsServerTags.forEach(tag => {
      const opt = document.createElement("option");
      opt.value = tag;
      opt.textContent = tag;
      settingsSelect.appendChild(opt);
    });
  }

  const modalSelect = document.getElementById("curationModalTagDropdown");
  if (modalSelect) {
    modalSelect.innerHTML = '<option value="">-- Add another ABS tag --</option>';
    cachedAbsServerTags.forEach(tag => {
      const opt = document.createElement("option");
      opt.value = tag;
      opt.textContent = tag;
      modalSelect.appendChild(opt);
    });
  }
}

// ============================================================================
// Auth & Setup Lifecycle
// ============================================================================

async function checkAuthStatus() {
  try {
    const res = await fetch("/api/admin/auth/status", { cache: "no-store" });
    if (res.status === 502 || res.status === 503 || res.status === 504 || !res.ok) {
      if (window.StoryDeckHealth) {
        window.StoryDeckHealth.reportNetworkError(`Core gateway returned HTTP ${res.status}.`);
      }
      return;
    }
    const data = await res.json();
    window.currentSsoMeta = data.sso || {};

    const ssoNoticeEl = document.getElementById("loginSsoNotice");
    const ssoContainerEl = document.getElementById("loginSsoContainer");
    const ssoButtonTextEl = document.getElementById("ssoButtonText");
    const ssoButtonIconEl = document.getElementById("ssoButtonIcon");
    const loginErrorEl = document.getElementById("loginErrorAlert");

    const urlParams = new URLSearchParams(window.location.search);
    const errParam = urlParams.get("error");
    if (loginErrorEl) {
      if (errParam) {
        loginErrorEl.style.display = "block";
        loginErrorEl.innerText = `⚠️ ${errParam}`;
      } else {
        loginErrorEl.style.display = "none";
      }
    }

    const ssoEnabled = Boolean(
      window.currentSsoMeta.enabled ||
      window.currentSsoMeta.oidc_enabled ||
      window.currentSsoMeta.forward_auth_enabled
    );
    const rawProv = String(window.currentSsoMeta.provider_name || "").trim();
    const prov = (!rawProv || ["reverse proxy", "generic forwardauth"].includes(rawProv.toLowerCase()))
      ? "SSO"
      : rawProv;

    if (ssoContainerEl) {
      if (ssoEnabled) {
        ssoContainerEl.style.display = "block";
        if (ssoButtonTextEl) {
          ssoButtonTextEl.innerText = `Sign in with ${prov}`;
        }
        if (ssoButtonIconEl) {
          ssoButtonIconEl.innerText = window.currentSsoMeta.login_mode === "oidc" ? "🔑" : "🛡️";
        }
      } else {
        ssoContainerEl.style.display = "none";
      }
    }

    if (ssoNoticeEl) {
      if (window.currentSsoMeta.login_mode === "oidc") {
        ssoNoticeEl.style.display = "block";
        ssoNoticeEl.innerText = `🔑 OpenID Connect (${prov}) is configured. Click "Sign in with ${prov}" above to authenticate via your Identity Provider.`;
      } else if (window.currentSsoMeta.forward_auth_enabled) {
        ssoNoticeEl.style.display = "block";
        ssoNoticeEl.innerText = `🛡️ Reverse-proxy ForwardAuth (${prov}) is enabled. Accessing via your SSO gateway signs you in automatically.`;
      } else {
        ssoNoticeEl.style.display = "none";
      }
    }

    const rawNext = urlParams.get("next");
    const safeNext = (rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//") && !rawNext.includes("\\")) ? rawNext : null;
    const curPath = window.location.pathname;
    const isLoginRoute = (curPath === "/login" || curPath === "/admin/login");

    const modalTitleEl = document.getElementById("loginModalTitle");
    const modalSubEl = document.getElementById("loginModalSubtitle");
    if (modalTitleEl && modalSubEl) {
      const wantsAdmin = Boolean((safeNext && safeNext.startsWith("/admin") && safeNext !== "/admin/login") || (curPath.startsWith("/admin") && !isLoginRoute));
      if (wantsAdmin) {
        modalTitleEl.innerText = "🔒 Administrator Sign In";
        modalSubEl.innerText = "Enter credentials to manage profiles and curation.";
      } else {
        modalTitleEl.innerText = "🔒 StoryDeck Sign In";
        modalSubEl.innerText = "Sign in with your household account to continue.";
      }
    }

    if (data.setup_required) {
      document.getElementById("setupModal").style.display = "flex";
      document.getElementById("loginModal").style.display = "none";
      document.getElementById("mainApp").style.display = "none";
    } else if (!data.authenticated) {
      if (data.portal_authenticated && (isLoginRoute || (safeNext && !safeNext.startsWith("/admin")))) {
        const portalTarget = (safeNext && !safeNext.startsWith("/admin") && safeNext !== "/login") ? safeNext : "/";
        window.location.href = portalTarget;
        return;
      }
      document.getElementById("setupModal").style.display = "none";
      document.getElementById("loginModal").style.display = "flex";
      document.getElementById("mainApp").style.display = "none";
    } else {
      if (safeNext && safeNext !== curPath && safeNext !== "/login" && safeNext !== "/admin/login") {
        window.location.href = safeNext;
        return;
      }
      if (curPath === "/login") {
        window.location.href = "/";
        return;
      }
      currentUser = data.user;
      document.getElementById("setupModal").style.display = "none";
      document.getElementById("loginModal").style.display = "none";
      document.getElementById("mainApp").style.display = "flex";

      if (typeof renderSidebarUserIdentity === "function") {
        renderSidebarUserIdentity(currentUser, window.currentSsoMeta);
      }

      if (!initialDataLoaded) {
        initialDataLoaded = true;
        loadInitialData();
      }
    }
  } catch (err) {
    console.error("Auth check failed:", err);
    if (window.StoryDeckHealth) {
      window.StoryDeckHealth.reportNetworkError("Cannot reach StoryDeck Core server to verify authentication.");
    }
  }
}

async function handleInitialSetup(e) {
  e.preventDefault();
  const username = document.getElementById("setupUsername").value.trim();
  const displayName = document.getElementById("setupDisplayName").value.trim() || "Parent";
  const password = document.getElementById("setupPassword").value;

  try {
    const res = await fetch("/api/admin/auth/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, display_name: displayName, password })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast("Primary administrator account created successfully!", "success");
      // Proceed to login
      await handleAutoLogin(username, password);
    } else {
      showToast(data.error || "Setup failed.", "danger");
    }
  } catch (err) {
    showToast("Network error during setup.", "danger");
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  await handleAutoLogin(username, password);
}

async function handleAutoLogin(username, password) {
  try {
    const res = await fetch("/api/admin/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast("Signed in successfully.", "success");
      const urlParams = new URLSearchParams(window.location.search);
      const rawNext = urlParams.get("next");
      const safeNext = (rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//") && !rawNext.includes("\\")) ? rawNext : null;
      const userRole = (data.user && data.user.role) || "member";
      if (userRole !== "admin") {
        window.location.href = (safeNext && !safeNext.startsWith("/admin") && safeNext !== "/login") ? safeNext : "/";
        return;
      }
      if (safeNext && safeNext !== window.location.pathname && safeNext !== "/login" && safeNext !== "/admin/login") {
        window.location.href = safeNext;
        return;
      }
      if (window.location.pathname === "/login") {
        window.location.href = "/";
        return;
      }
      if (window.location.pathname === "/admin/login" && window.history && window.history.replaceState) {
        window.history.replaceState({}, "", "/admin");
      }
      initialDataLoaded = false;
      checkAuthStatus();
    } else {
      showToast(data.error || "Invalid username or password.", "danger");
    }
  } catch (err) {
    showToast("Sign in failed. Check connection.", "danger");
  }
}

function handleSsoLogin() {
  const urlParams = new URLSearchParams(window.location.search);
  const rawNext = urlParams.get("next");
  let safeNext = (rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//") && !rawNext.includes("\\"))
    ? rawNext
    : null;
  if (!safeNext) {
    if (window.location.pathname === "/login") {
      safeNext = "/";
    } else if (window.location.pathname.startsWith("/admin")) {
      safeNext = "/admin";
    } else {
      safeNext = window.location.pathname || "/";
    }
  }
  window.location.href = `/api/auth/sso/login?next=${encodeURIComponent(safeNext)}`;
}

async function logout() {
  if (typeof logoutCurrentUser === "function") {
    await logoutCurrentUser();
    return;
  }
  location.reload();
}
