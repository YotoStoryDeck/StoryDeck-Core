// ============================================================================
// Admin Household Users Modal & Per-User Audiobookshelf Account Linking
// ============================================================================

function _isSsoEnabledInUi() {
  const cfg = (cachedSsoMeta && cachedSsoMeta.sso_config) || {};
  return Boolean(cfg.forward_auth_enabled || cfg.oidc_enabled);
}

function _configureUserModalOptions(isEditingSuperAdmin) {
  const superOpt = document.getElementById("editUserRoleSuperAdmin");
  const actorIsSuper = Boolean(typeof currentUser !== "undefined" && currentUser && currentUser.is_super_admin);
  if (superOpt) {
    superOpt.style.display = (actorIsSuper || isEditingSuperAdmin) ? "" : "none";
  }
  const ssoOpt = document.getElementById("editUserAuthSourceSso");
  if (ssoOpt) {
    ssoOpt.style.display = _isSsoEnabledInUi() ? "" : "none";
  }
}

function onEditUserAuthSourceChange() {
  const isSso = Boolean(document.getElementById("editUserAuthSource")?.value);
  const pwdEl = document.getElementById("editUserPassword");
  const isCreate = !(document.getElementById("editUserId")?.value || "").trim();
  if (pwdEl) {
    pwdEl.placeholder = isSso
      ? "Optional (SSO handles sign-in)"
      : (isCreate ? "Required (min 8 chars)" : "Leave blank to keep current (min 8 chars)");
  }
}

let _adminClearAbsAccount = false;
let _adminVerifiedAbs = null;
let _adminAutoMatchAbs = false;

function _renderAdminUserAbsBadge(linkedName, hasKey) {
  const statusEl = document.getElementById("editUserAbsStatus");
  const unlinkBtn = document.getElementById("btnEditUserUnlinkAbs");
  const autoMatchBtn = document.getElementById("btnAutoMatchEditUserAbs");
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
      : "Match email or username in Audiobookshelf and generate a scoped key";
  }
  if (unlinkBtn) {
    unlinkBtn.style.display = isLinked ? "inline-flex" : "none";
  }
}

async function autoMatchAdminUserAbsAccount() {
  const uid = document.getElementById("editUserId")?.value.trim() || "";
  const username = document.getElementById("editUserUsername")?.value.trim() || "";
  const email = document.getElementById("editUserEmail")?.value.trim() || "";
  const btn = document.getElementById("btnAutoMatchEditUserAbs");
  const hint = document.getElementById("editUserAbsHint");
  const existingUser = uid ? cachedUsersList.find(item => (item.id || item.user_id) === uid) : null;
  const wasLinked = Boolean((existingUser && (existingUser.has_abs_api_key || existingUser.abs_username)) || _adminVerifiedAbs);
  if (!username && !email) {
    showToast("Enter the user's username or email first.", "warning");
    return;
  }
  if (btn) { btn.disabled = true; btn.innerText = wasLinked ? "🔄 Recreating..." : "🔗 Matching..."; }
  try {
    const res = await fetch("/api/auth/abs/auto-match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: uid || "",
        username,
        email,
        preview_only: !uid,
      })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      _adminClearAbsAccount = false;
      _adminAutoMatchAbs = !uid;
      _adminVerifiedAbs = { abs_username: data.abs_username || "", abs_user_id: data.abs_user_id || "" };
      _renderAdminUserAbsBadge(data.abs_username, true);
      if (hint) {
        hint.innerText = uid
          ? (wasLinked
            ? `Recreated ABS API key for '${data.abs_username}' (old key deleted).`
            : `Linked ABS user '${data.abs_username}' (matched by ${data.matched_by || "username"}, ${data.libraries_count || 0} libraries).`)
          : `Matched ABS user '${data.abs_username}' (by ${data.matched_by || "username"}). Scoped key will be minted on Create Account.`;
      }
      if (uid) {
        await loadHouseholdUsers();
      }
      showToast(
        wasLinked && uid
          ? `Recreated ABS API key for ${data.abs_username} (old key deleted).`
          : `Matched ABS user: ${data.abs_username}`,
        "success"
      );
    } else {
      _adminAutoMatchAbs = false;
      showToast(data.error || "No matching Audiobookshelf user found.", "warning");
    }
  } catch {
    showToast("Network error auto-matching ABS user.", "danger");
  } finally {
    const isNowLinked = Boolean(_adminVerifiedAbs || (existingUser && existingUser.has_abs_api_key));
    if (btn) {
      btn.disabled = false;
      btn.innerText = isNowLinked ? "🔄 Recreate Key" : "🔗 Auto-Match ABS";
    }
  }
}

async function verifyAdminUserAbsKey() {
  const key = document.getElementById("editUserAbsApiKey")?.value.trim() || "";
  const btn = document.getElementById("btnVerifyEditUserAbsKey");
  const hint = document.getElementById("editUserAbsHint");
  if (!key) {
    showToast("Paste an Audiobookshelf API key first.", "warning");
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
      _adminClearAbsAccount = false;
      _adminAutoMatchAbs = false;
      _adminVerifiedAbs = { abs_username: data.abs_username || "", abs_user_id: data.abs_user_id || "" };
      _renderAdminUserAbsBadge(data.abs_username, true);
      if (hint) hint.innerText = `Detected ABS user '${data.abs_username}' (${data.libraries_count || 0} libraries). Save to apply.`;
      showToast(`Detected ABS user: ${data.abs_username}`, "success");
    } else {
      _adminVerifiedAbs = null;
      showToast(data.error || "Could not verify ABS API key.", "danger");
    }
  } catch {
    showToast("Network error verifying ABS key.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = "🔍 Detect Key"; }
  }
}

async function unlinkAdminUserAbsAccount() {
  const uid = document.getElementById("editUserId")?.value.trim() || "";
  const unlinkBtn = document.getElementById("btnEditUserUnlinkAbs");
  const hint = document.getElementById("editUserAbsHint");
  const absInput = document.getElementById("editUserAbsApiKey");
  if (!uid) {
    _adminClearAbsAccount = true;
    _adminVerifiedAbs = null;
    _adminAutoMatchAbs = false;
    if (absInput) {
      absInput.value = "";
      absInput.placeholder = "Paste user's personal ABS API key (optional)";
    }
    _renderAdminUserAbsBadge("", false);
    if (hint) hint.innerText = "ABS account cleared.";
    return;
  }
  if (unlinkBtn) { unlinkBtn.disabled = true; unlinkBtn.innerText = "Unlinking..."; }
  try {
    const res = await fetch("/api/auth/abs/unlink", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      _adminClearAbsAccount = false;
      _adminVerifiedAbs = null;
      _adminAutoMatchAbs = false;
      if (absInput) {
        absInput.value = "";
        absInput.placeholder = "Paste user's personal ABS API key (optional)";
      }
      _renderAdminUserAbsBadge("", false);
      if (hint) hint.innerText = "ABS account unlinked and old API key deleted from Audiobookshelf.";
      await loadHouseholdUsers();
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

function openCreateUserModal() {
  const modal = document.getElementById("editUserModal");
  if (!modal) return;
  _adminClearAbsAccount = false;
  _adminVerifiedAbs = null;
  _adminAutoMatchAbs = false;
  _configureUserModalOptions(false);
  document.getElementById("editUserId").value = "";
  document.getElementById("editUserUsername").value = "";
  document.getElementById("editUserDisplayName").value = "";
  const emailEl = document.getElementById("editUserEmail");
  if (emailEl) emailEl.value = "";
  const roleEl = document.getElementById("editUserRole");
  if (roleEl) {
    roleEl.disabled = false;
    roleEl.value = "member";
  }
  const roleHint = document.getElementById("editUserRoleHint");
  if (roleHint) { roleHint.style.display = "none"; roleHint.innerText = ""; }
  const authEl = document.getElementById("editUserAuthSource");
  if (authEl) authEl.value = "";
  document.getElementById("editUserPassword").value = "";
  const absInput = document.getElementById("editUserAbsApiKey");
  if (absInput) { absInput.value = ""; absInput.placeholder = "Paste user's personal ABS API key"; }
  _renderAdminUserAbsBadge("", false);
  const absHint = document.getElementById("editUserAbsHint");
  if (absHint) absHint.innerText = "Used when this user overrides a Target Player to listen from their personal ABS library.";
  const titleEl = document.getElementById("editUserModalTitle");
  if (titleEl) titleEl.innerText = "➕ Add Household User";
  const subtitle = document.getElementById("editUserModalSubtitle");
  if (subtitle) subtitle.innerText = "Create a new household account.";
  const pwdLabel = document.getElementById("editUserPasswordLabel");
  if (pwdLabel) pwdLabel.innerText = "Password";
  const saveBtn = document.getElementById("btnSaveEditUser");
  if (saveBtn) saveBtn.innerText = "+ Create Account";
  onEditUserAuthSourceChange();
  modal.style.display = "flex";
}

function openEditUserModal(userId) {
  const u = cachedUsersList.find(item => (item.id || item.user_id) === userId);
  if (!u) {
    showToast("Could not locate user profile details.", "warning");
    return;
  }
  const modal = document.getElementById("editUserModal");
  if (!modal) return;
  _adminClearAbsAccount = false;
  _adminVerifiedAbs = null;

  const isSuper = Boolean(u.is_super_admin);
  const isSelf = Boolean(u.is_self || (currentUsersMeta.current_user_id && currentUsersMeta.current_user_id === (u.id || u.user_id)));

  document.getElementById("editUserId").value = u.id || u.user_id || "";
  document.getElementById("editUserUsername").value = u.username || "";
  document.getElementById("editUserDisplayName").value = u.display_name || "";
  const emailEl = document.getElementById("editUserEmail");
  if (emailEl) emailEl.value = u.email || "";
  _configureUserModalOptions(isSuper);
  document.getElementById("editUserRole").value = isSuper ? "super_admin" : (u.role || "member");
  const titleEl = document.getElementById("editUserModalTitle");
  if (titleEl) titleEl.innerText = "✏️ Edit User Profile";
  const pwdLabel = document.getElementById("editUserPasswordLabel");
  if (pwdLabel) pwdLabel.innerText = "New Password (Optional)";
  const saveBtn = document.getElementById("btnSaveEditUser");
  if (saveBtn) saveBtn.innerText = "💾 Save Changes";
  document.getElementById("editUserAuthSource").value = (u.sso_provider || "").trim() ? "sso" : "";
  onEditUserAuthSourceChange();
  document.getElementById("editUserPassword").value = "";
  const absInput = document.getElementById("editUserAbsApiKey");
  if (absInput) {
    absInput.value = "";
    absInput.placeholder = u.has_abs_api_key ? "API key linked (paste new key to replace)" : "Paste user's personal ABS API key";
  }
  _renderAdminUserAbsBadge(u.abs_username || "", Boolean(u.has_abs_api_key));
  const absHint = document.getElementById("editUserAbsHint");
  if (absHint) absHint.innerText = "Used when this user overrides a Target Player to listen from their personal ABS library.";

  const subtitle = document.getElementById("editUserModalSubtitle");
  if (subtitle) {
    subtitle.innerText = isSuper
      ? "Editing primary Super Admin profile. Role changes remain locked for safety."
      : `Editing profile for ${u.display_name || u.username}.`;
  }

  const roleEl = document.getElementById("editUserRole");
  const roleHint = document.getElementById("editUserRoleHint");
  if (roleEl && roleHint) {
    if (isSuper) {
      roleEl.disabled = true;
      roleHint.style.display = "block";
      roleHint.innerText = "Primary Super Admin role is permanently locked to Admin.";
    } else if (isSelf) {
      roleEl.disabled = true;
      roleHint.style.display = "block";
      roleHint.innerText = "You cannot demote your own active administrator account.";
    } else {
      roleEl.disabled = false;
      roleHint.style.display = "none";
      roleHint.innerText = "";
    }
  }

  modal.style.display = "flex";
}

function closeEditUserModal() {
  const modal = document.getElementById("editUserModal");
  if (modal) modal.style.display = "none";
}

async function openAdminSelfProfileModal() {
  if (typeof openSelfServiceProfileModal === "function") {
    await openSelfServiceProfileModal();
    return;
  }
}

async function saveEditedUserProfile() {
  const uid = document.getElementById("editUserId")?.value || "";
  const username = document.getElementById("editUserUsername")?.value.trim() || "";
  const displayName = document.getElementById("editUserDisplayName")?.value.trim() || username;
  const email = document.getElementById("editUserEmail")?.value.trim() || "";
  const roleEl = document.getElementById("editUserRole");
  const ssoProvider = document.getElementById("editUserAuthSource")?.value || "";
  const password = document.getElementById("editUserPassword")?.value || "";
  const absApiKey = document.getElementById("editUserAbsApiKey")?.value.trim() || "";

  if (!username) {
    showToast("Username is required.", "warning");
    return;
  }
  if (!uid) {
    if (!ssoProvider && password.length < 8) {
      showToast("Password must be at least 8 characters for local accounts.", "warning");
      return;
    }
    const createBtn = document.getElementById("btnSaveEditUser");
    if (createBtn) { createBtn.disabled = true; createBtn.innerText = "Creating..."; }
    try {
      const provName = ssoProvider ? ((cachedSsoMeta?.sso_config?.provider_name || "sso").toLowerCase()) : "";
      const createPayload = {
        username, display_name: displayName, email,
        role: roleEl ? roleEl.value : "member",
        sso_provider: provName,
        password
      };
      if (absApiKey) {
        createPayload.abs_api_key = absApiKey;
        if (_adminVerifiedAbs) {
          createPayload.abs_username = _adminVerifiedAbs.abs_username;
          createPayload.abs_user_id = _adminVerifiedAbs.abs_user_id;
        }
      } else if (_adminAutoMatchAbs) {
        createPayload.auto_match_abs = true;
      }
      const cRes = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createPayload)
      });
      const cData = await cRes.json();
      if (cRes.ok && cData.ok) {
        closeEditUserModal();
        showToast(`Created user '${username}'.`, "success");
        await loadHouseholdUsers();
      } else {
        showToast(cData.error || "Failed to create user.", "danger");
      }
    } catch {
      showToast("Network error creating user.", "danger");
    } finally {
      if (createBtn) { createBtn.disabled = false; createBtn.innerText = "+ Create Account"; }
    }
    return;
  }
  if (password && password.length < 8) {
    showToast("New password must be at least 8 characters.", "warning");
    return;
  }

  const payload = {
    username,
    display_name: displayName,
    email,
    sso_provider: ssoProvider
  };
  if (roleEl && !roleEl.disabled) {
    payload.role = roleEl.value;
  }
  if (password) {
    payload.password = password;
  }
  if (absApiKey) {
    payload.abs_api_key = absApiKey;
    if (_adminVerifiedAbs) {
      payload.abs_username = _adminVerifiedAbs.abs_username;
      payload.abs_user_id = _adminVerifiedAbs.abs_user_id;
    }
  } else if (_adminClearAbsAccount) {
    payload.clear_abs_account = true;
  }

  const btn = document.getElementById("btnSaveEditUser");
  if (btn) {
    btn.disabled = true;
    btn.innerText = "💾 Saving...";
  }

  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(uid)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Updated user profile '${username}'.`, "success");
      closeEditUserModal();
      if (currentUsersMeta.current_user_id && currentUsersMeta.current_user_id === uid && data.user) {
        if (typeof currentUser === "object" && currentUser) {
          currentUser.username = data.user.username;
          currentUser.display_name = data.user.display_name;
        }
        const greetEl = document.getElementById("userGreeting");
        if (greetEl) greetEl.innerText = `Welcome, ${data.user.display_name || data.user.username}`;
        const sideNameEl = document.getElementById("sidebarAdminName");
        if (sideNameEl) sideNameEl.innerText = data.user.display_name || data.user.username;
      }
      await loadHouseholdUsers();
    } else {
      showToast(data.error || "Failed to update user profile.", "danger");
    }
  } catch (err) {
    showToast("Network error saving user profile.", "danger");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = "💾 Save Changes";
    }
  }
}

