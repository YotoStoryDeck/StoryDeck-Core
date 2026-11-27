// ============================================================================
// StoryDeck Core Shared Collapsible Sidebar, Identity & Self-Service Profile
// Centralizes sidebar drawer state, user footer badge, self-service profile
// editor (including ABS auto-match / recreate / unlink), and logout across
// both the Parent Portal (/) and Admin Console (/admin).
// ============================================================================

const SIDEBAR_STORAGE_KEY = "storydeck_sidebar_collapsed";

function initSidebar() {
  try {
    if (localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1") {
      document.body.classList.add("sidebar-collapsed");
    }
  } catch (e) {
    // Ignore storage access restriction errors
  }

  const collapseBtn = document.getElementById("sidebarCollapseBtn");
  if (collapseBtn) {
    const collapsed = document.body.classList.contains("sidebar-collapsed");
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
    collapseBtn.title = collapsed ? "Expand menu" : "Collapse menu";
  }
}

function toggleSidebarCollapse() {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
  } catch (e) {
    // Ignore storage access restriction errors
  }
  const collapseBtn = document.getElementById("sidebarCollapseBtn");
  if (collapseBtn) {
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
    collapseBtn.title = collapsed ? "Expand menu" : "Collapse menu";
  }
}

function openMobileSidebar() {
  document.body.classList.add("sidebar-mobile-open");
}

function closeMobileSidebar() {
  document.body.classList.remove("sidebar-mobile-open");
}

function toggleMobileSidebar() {
  document.body.classList.toggle("sidebar-mobile-open");
}

// ============================================================================
// Centralized Sidebar User Identity & Role Rendering
// ============================================================================

function renderSidebarUserIdentity(u, ssoMeta = null) {
  const nameEl = document.getElementById("sidebarUserName") || document.getElementById("sidebarAdminName");
  const roleEl = document.getElementById("sidebarUserRole") || document.getElementById("sidebarAdminRole");
  const loginBtn = document.getElementById("sidebarLoginBtn");
  const userActions = document.getElementById("sidebarUserActions") || document.getElementById("sidebarAdminActions");

  if (!u) {
    if (nameEl) nameEl.innerText = "Sign In Required";
    if (roleEl) roleEl.innerText = "Unauthenticated";
    if (loginBtn) loginBtn.style.display = "inline-flex";
    if (userActions) userActions.style.display = "none";
    return;
  }

  window.currentPortalUser = u;
  window.currentPortalUserRole = u.role || "guest";
  if (typeof currentUser !== "undefined") {
    currentUser = u;
  }

  const isGuest = window.currentPortalUserRole === "guest" || Boolean(u.is_lan_guest);
  const displayName = u.is_lan_guest ? "Guest" : (u.display_name || u.username || "Household");

  let roleTitle = "Member";
  if (u.is_super_admin) {
    roleTitle = "Super Admin";
  } else if (u.role === "admin") {
    roleTitle = "Admin";
  } else if (isGuest) {
    roleTitle = "Guest";
  }

  let roleLabel = isGuest ? "Logged in as Guest" : `Signed in • ${roleTitle}`;
  if (u.is_lan_guest) {
    roleLabel = "Logged in as Guest (LAN)";
  } else if (u.auth_type === "sso" || u.auth_type === "oidc") {
    const prov = u.sso_provider || (ssoMeta && ssoMeta.provider_name) || "Authentik";
    roleLabel = `SSO (${prov}) • ${roleTitle}`;
  } else if (u.auth_type === "api_key") {
    roleLabel = `API Key • ${roleTitle}`;
  }

  if (loginBtn) {
    loginBtn.style.display = u.is_lan_guest ? "inline-flex" : "none";
  }
  if (userActions) {
    userActions.style.display = u.is_lan_guest ? "none" : "flex";
  }

  // Enforce Guest & non-Admin nav visibility on Portal
  document.querySelectorAll('[data-tab="cards"], [data-tab="playlists"]').forEach(el => {
    el.style.display = isGuest ? "none" : "";
  });
  document.querySelectorAll('a[href="/admin"]').forEach(el => {
    el.style.display = (u.role === "admin") ? "" : "none";
  });

  if (nameEl) nameEl.innerText = displayName;
  if (roleEl) roleEl.innerText = roleLabel;
}

async function loadSidebarUserIdentity() {
  try {
    const res = await fetch("/api/auth/me", { cache: "no-store" });
    if (!res.ok) return null;
    const data = await res.json();
    if (!data.authenticated || !data.user) {
      renderSidebarUserIdentity(null);
      return null;
    }
    renderSidebarUserIdentity(data.user, window.currentSsoMeta || null);
    return data.user;
  } catch (err) {
    if (window.StoryDeckHealth) {
      window.StoryDeckHealth.reportNetworkError("Failed checking user session.");
    }
    return null;
  }
}

// Keep backwards-compatible alias for Portal callers
const loadPortalUserIdentity = loadSidebarUserIdentity;

// ============================================================================
// Centralized Self-Service Profile Modal (Shared by Portal & Admin)
// ============================================================================

let _portalClearAbsAccount = false;
let _portalVerifiedAbs = null;
let _portalAutoMatchRequested = false;

function _renderPortalAbsBadge(linkedName, hasKey) {
  const statusEl = document.getElementById("portalEditAbsStatus");
  const unlinkBtn = document.getElementById("btnPortalUnlinkAbs");
  const autoMatchBtn = document.getElementById("btnAutoMatchPortalAbs");
  const isLinked = Boolean(linkedName || hasKey);
  if (statusEl) {
    if (linkedName) {
      statusEl.className = "badge badge-success";
      statusEl.innerText = `✅ Linked: ${linkedName}`;
    } else if (hasKey) {
      statusEl.className = "badge badge-info";
      statusEl.innerText = "✅ Key Saved";
    } else {
      statusEl.className = "badge badge-subtle";
      statusEl.innerText = "Not linked";
    }
  }
  if (autoMatchBtn) {
    autoMatchBtn.innerText = isLinked ? "🔄 Recreate Key" : "🔗 Auto-Match ABS";
    autoMatchBtn.title = isLinked
      ? "Delete the old ABS API key and generate a fresh key"
      : "Match your email or username in Audiobookshelf and generate a scoped key";
  }
  if (unlinkBtn) {
    unlinkBtn.style.display = isLinked ? "inline-flex" : "none";
  }
}

async function openSelfServiceProfileModal() {
  let u = window.currentPortalUser;
  if (!u || u.abs_self_service_enabled === undefined) {
    u = await loadSidebarUserIdentity();
  }
  if (!u || u.is_lan_guest) return;
  const modal = document.getElementById("portalEditProfileModal");
  if (!modal) return;
  _portalClearAbsAccount = false;
  _portalVerifiedAbs = null;
  _portalAutoMatchRequested = false;
  document.getElementById("portalEditDisplayName").value = u.display_name || u.username || "";
  document.getElementById("portalEditEmail").value = u.email || "";
  document.getElementById("portalEditPassword").value = "";
  const absInput = document.getElementById("portalEditAbsApiKey");
  if (absInput) {
    absInput.value = "";
    absInput.placeholder = u.has_abs_api_key ? "API key linked (paste new key to replace)" : "Paste personal ABS API key (optional)";
  }
  _renderPortalAbsBadge(u.abs_username || "", Boolean(u.has_abs_api_key));
  const canSelfManageAbs = Boolean(u.role === "admin" || u.abs_self_service_enabled !== false);
  const autoMatchBtn = document.getElementById("btnAutoMatchPortalAbs");
  const unlinkBtn = document.getElementById("btnPortalUnlinkAbs");
  const advDetails = document.getElementById("portalEditAbsAdvancedDetails");
  const hint = document.getElementById("portalEditAbsHint");
  if (!canSelfManageAbs) {
    if (autoMatchBtn) autoMatchBtn.style.display = "none";
    if (unlinkBtn) unlinkBtn.style.display = "none";
    if (advDetails) advDetails.style.display = "none";
    if (hint) hint.innerText = "Audiobookshelf account linking is managed by your Administrator.";
  } else {
    if (autoMatchBtn) autoMatchBtn.style.display = "inline-flex";
    if (advDetails) advDetails.style.display = "";
    if (hint) hint.innerText = "Matches your email or username in Audiobookshelf and automatically provisions a scoped user API key.";
  }
  const sub = document.getElementById("portalProfileSubtitle");
  const roleDisplay = u.is_super_admin ? "super admin" : (u.role || "member");
  if (sub) sub.innerText = `Account: ${u.username || ""} (${roleDisplay})`;
  modal.style.display = "flex";
}

// Keep backwards-compatible aliases for Portal & Admin callers
const openPortalProfileModal = openSelfServiceProfileModal;

async function _syncAfterSelfServiceChange() {
  await loadSidebarUserIdentity();
  if (typeof loadHouseholdUsers === "function") {
    try {
      await loadHouseholdUsers();
    } catch { /* ignore if not on users tab */ }
  }
}

async function autoMatchPortalAbsAccount() {
  const btn = document.getElementById("btnAutoMatchPortalAbs");
  const hint = document.getElementById("portalEditAbsHint");
  const email = document.getElementById("portalEditEmail")?.value.trim() || "";
  const wasLinked = Boolean(window.currentPortalUser?.has_abs_api_key || window.currentPortalUser?.abs_username || _portalVerifiedAbs);
  if (btn) { btn.disabled = true; btn.innerText = wasLinked ? "🔄 Recreating..." : "🔗 Matching..."; }
  try {
    const res = await fetch("/api/auth/abs/auto-match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      _portalClearAbsAccount = false;
      _portalAutoMatchRequested = false;
      _portalVerifiedAbs = { abs_username: data.abs_username || "", abs_user_id: data.abs_user_id || "" };
      _renderPortalAbsBadge(data.abs_username, true);
      if (hint) {
        hint.innerText = wasLinked
          ? `Recreated ABS API key for '${data.abs_username}' (old key deleted).`
          : `Linked ABS user '${data.abs_username}' (matched by ${data.matched_by || "username"}, ${data.libraries_count || 0} libraries).`;
      }
      await _syncAfterSelfServiceChange();
      showToast(
        wasLinked
          ? `Recreated ABS API key for ${data.abs_username} (old key deleted).`
          : `Linked ABS account: ${data.abs_username}`,
        "success"
      );
    } else {
      showToast(data.error || "Could not auto-match ABS account.", "warning");
    }
  } catch {
    showToast("Network error auto-matching ABS account.", "danger");
  } finally {
    const isNowLinked = Boolean(window.currentPortalUser?.has_abs_api_key || _portalVerifiedAbs);
    if (btn) {
      btn.disabled = false;
      btn.innerText = isNowLinked ? "🔄 Recreate Key" : "🔗 Auto-Match ABS";
    }
  }
}

async function verifyPortalAbsKey() {
  const key = document.getElementById("portalEditAbsApiKey")?.value.trim() || "";
  const btn = document.getElementById("btnVerifyPortalAbsKey");
  const hint = document.getElementById("portalEditAbsHint");
  if (!key) {
    showToast("Paste your Audiobookshelf API key first.", "warning");
    return;
  }
  if (btn) { btn.disabled = true; btn.innerText = "🔍 Checking..."; }
  try {
    const res = await fetch("/api/auth/abs/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ abs_api_key: key })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      _portalClearAbsAccount = false;
      _portalVerifiedAbs = { abs_username: data.abs_username || "", abs_user_id: data.abs_user_id || "" };
      _renderPortalAbsBadge(data.abs_username, true);
      if (hint) hint.innerText = `Detected ABS user '${data.abs_username}' (${data.libraries_count || 0} accessible libraries). Click Save Changes to store.`;
      showToast(`Detected ABS user: ${data.abs_username}`, "success");
    } else {
      _portalVerifiedAbs = null;
      showToast(data.error || "Could not verify ABS API key.", "danger");
    }
  } catch {
    showToast("Network error verifying ABS key.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = "🔍 Detect Key"; }
  }
}

async function unlinkPortalAbsAccount() {
  const unlinkBtn = document.getElementById("btnPortalUnlinkAbs");
  const hint = document.getElementById("portalEditAbsHint");
  if (unlinkBtn) { unlinkBtn.disabled = true; unlinkBtn.innerText = "Unlinking..."; }
  try {
    const res = await fetch("/api/auth/abs/unlink", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      _portalClearAbsAccount = false;
      _portalVerifiedAbs = null;
      _portalAutoMatchRequested = false;
      const absInput = document.getElementById("portalEditAbsApiKey");
      if (absInput) {
        absInput.value = "";
        absInput.placeholder = "Paste personal ABS API key (optional)";
      }
      _renderPortalAbsBadge("", false);
      if (hint) hint.innerText = "ABS account unlinked and old API key deleted from Audiobookshelf.";
      await _syncAfterSelfServiceChange();
      showToast("Unlinked ABS account and deleted API key.", "success");
    } else {
      showToast(data.error || "Failed to unlink ABS account.", "danger");
    }
  } catch {
    showToast("Network error unlinking ABS account.", "danger");
  } finally {
    if (unlinkBtn) { unlinkBtn.disabled = false; unlinkBtn.innerText = "Unlink"; }
  }
}

function closePortalProfileModal() {
  const modal = document.getElementById("portalEditProfileModal");
  if (modal) modal.style.display = "none";
}

async function savePortalProfile() {
  const displayName = document.getElementById("portalEditDisplayName")?.value.trim() || "";
  const email = document.getElementById("portalEditEmail")?.value.trim() || "";
  const password = document.getElementById("portalEditPassword")?.value || "";
  const absApiKey = document.getElementById("portalEditAbsApiKey")?.value.trim() || "";
  if (password && password.length < 8) {
    showToast("New password must be at least 8 characters.", "warning");
    return;
  }
  const btn = document.getElementById("btnSavePortalProfile");
  if (btn) { btn.disabled = true; btn.innerText = "💾 Saving..."; }
  try {
    const payload = { display_name: displayName, email };
    if (password) payload.password = password;
    if (absApiKey) {
      payload.abs_api_key = absApiKey;
      if (_portalVerifiedAbs) {
        payload.abs_username = _portalVerifiedAbs.abs_username;
        payload.abs_user_id = _portalVerifiedAbs.abs_user_id;
      }
    } else if (_portalClearAbsAccount) {
      payload.clear_abs_account = true;
    }
    const res = await fetch("/api/auth/me", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      closePortalProfileModal();
      await _syncAfterSelfServiceChange();
      showToast("Profile updated.", "success");
    } else {
      showToast(data.error || "Failed to update profile.", "danger");
    }
  } catch {
    showToast("Network error updating profile.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = "💾 Save Changes"; }
  }
}

async function logoutCurrentUser() {
  const u = window.currentPortalUser || (typeof currentUser !== "undefined" ? currentUser : null);
  const wasSso = Boolean(u && (u.auth_type === "sso" || u.auth_type === "oidc"));
  const ssoLogoutUrl = (window.currentSsoMeta && window.currentSsoMeta.logout_url)
    ? window.currentSsoMeta.logout_url.trim()
    : "";
  let serverLogoutUrl = "";
  try {
    const res = await fetch("/api/admin/auth/logout", { method: "POST" });
    if (res.ok) {
      const respData = await res.json();
      if (respData && respData.logout_url) {
        serverLogoutUrl = String(respData.logout_url).trim();
      }
    }
  } catch { /* ignore */ }
  const effectiveLogoutUrl = serverLogoutUrl || ssoLogoutUrl;
  if (wasSso && effectiveLogoutUrl) {
    window.location.href = effectiveLogoutUrl;
    return;
  }
  if ((window.location.pathname.startsWith("/admin") || window.location.pathname === "/login") && typeof checkAuthStatus === "function") {
    if (typeof initialDataLoaded !== "undefined") {
      initialDataLoaded = false;
    }
    showToast("Signed out.");
    checkAuthStatus();
    return;
  }
  window.location.href = "/";
}

const logoutPortalUser = logoutCurrentUser;

document.addEventListener("DOMContentLoaded", initSidebar);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeMobileSidebar();
  }
});
