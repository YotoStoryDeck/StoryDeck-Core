// ============================================================================
// Admin Users & Access Control Page (Household Accounts, LAN Access & SSO)
// ============================================================================

let cachedUsersList = [];
let currentUsersMeta = {
  current_user_id: "",
  current_is_super_admin: false
};
let cachedSsoMeta = {
  sso_config: {},
  env_trusted_proxies: ["127.0.0.1/32"],
  lan_sitter_access_enabled: true,
  current_auth_type: "session",
  oidc_callback_path: "/api/auth/oidc/callback"
};
let ssoBaselineSnapshot = null;
let ssoDirtyBound = false;

async function loadUsersPage() {
  await Promise.all([loadHouseholdUsers(), loadSsoAccessConfig()]);
  updateUsersKpis();
}

async function loadHouseholdUsers() {
  const tbody = document.getElementById("adminUsersTableBody");
  if (!tbody) return;
  try {
    const res = await fetch("/api/admin/users", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    cachedUsersList = Array.isArray(data.users) ? data.users : [];
    currentUsersMeta = {
      current_user_id: data.current_user_id || "",
      current_is_super_admin: Boolean(data.current_is_super_admin)
    };
    renderHouseholdUsers(cachedUsersList);
    updateUsersKpis();
  } catch (err) {
    console.error("Failed to load household users:", err);
  }
}

function updateUsersKpis() {
  const total = cachedUsersList.length;
  const superAdmins = cachedUsersList.filter(u => Boolean(u.is_super_admin)).length;
  const admins = cachedUsersList.filter(u => u.role === "admin" && !u.is_super_admin).length;
  const members = cachedUsersList.filter(u => u.role !== "admin").length;
  const ssoUsers = cachedUsersList.filter(u => Boolean(u.sso_provider || u.sso_subject)).length;

  const kpiTotal = document.getElementById("kpiTotalUsers");
  const kpiBreak = document.getElementById("kpiUsersBreakdown");
  const countBadge = document.getElementById("userCountBadge");
  if (kpiTotal) kpiTotal.innerText = String(total);
  if (kpiBreak) {
    const parts = [];
    if (superAdmins) parts.push(`${superAdmins} Super Admin`);
    parts.push(`${admins} Admin${admins === 1 ? "" : "s"}`);
    parts.push(`${members} Member${members === 1 ? "" : "s"}`);
    if (ssoUsers) parts.push(`${ssoUsers} SSO`);
    kpiBreak.innerText = parts.join(" · ");
  }
  if (countBadge) countBadge.innerText = `${total} Account${total === 1 ? "" : "s"}`;

  const authTypeMap = {
    sso: "SSO Proxy",
    oidc: "SSO (OIDC)",
    api_key: "Master API Key",
    session: "Local Session"
  };
  const authType = cachedSsoMeta.current_auth_type || (typeof currentUser === "object" && currentUser ? currentUser.auth_type : "session") || "session";
  const kpiMethod = document.getElementById("kpiActiveAuthMethod");
  const kpiMethodSub = document.getElementById("kpiActiveAuthSub");
  if (kpiMethod) kpiMethod.innerText = authTypeMap[authType] || "Local Session";
  if (kpiMethodSub && typeof currentUser === "object" && currentUser) {
    const tier = currentUser.is_super_admin ? "Super Admin" : "Admin";
    kpiMethodSub.innerText = `Signed in as ${currentUser.display_name || currentUser.username} (${tier})`;
  }

  const lanEnabled = Boolean(cachedSsoMeta.lan_sitter_access_enabled);
  const kpiLan = document.getElementById("kpiLanGuestStatus");
  const kpiLanSub = document.getElementById("kpiLanGuestSub");
  if (kpiLan) {
    kpiLan.innerText = lanEnabled ? "Enabled" : "Disabled";
    kpiLan.style.color = lanEnabled ? "var(--accent-success)" : "var(--text-secondary)";
  }
  if (kpiLanSub) {
    kpiLanSub.innerText = lanEnabled ? "LAN portals open without sign-in" : "Private LAN sign-in required";
  }

  const cfg = cachedSsoMeta.sso_config || {};
  const fwdEnabled = cfg.forward_auth_enabled !== false;
  const oidcEnabled = Boolean(cfg.oidc_enabled);
  const rawProv = String(cfg.provider_name || "Authentik").trim();
  const providerName = (!rawProv || ["reverse proxy", "generic forwardauth"].includes(rawProv.toLowerCase()))
    ? "SSO"
    : rawProv;
  const kpiSso = document.getElementById("kpiSsoStatus");
  const kpiSsoSub = document.getElementById("kpiSsoSub");
  if (kpiSso) {
    kpiSso.innerText = oidcEnabled ? `${providerName} (OIDC Ready)` : (fwdEnabled ? `${providerName} Proxy` : "Disabled");
    kpiSso.style.color = (fwdEnabled || oidcEnabled) ? "#93c5fd" : "var(--text-muted)";
  }
  if (kpiSsoSub) {
    if (oidcEnabled && !fwdEnabled) {
      kpiSsoSub.innerText = cfg.oidc_issuer_url ? "OpenID Connect active" : "OIDC enabled (configure Issuer URL)";
    } else if (fwdEnabled) {
      kpiSsoSub.innerText = `UID Header: ${cfg.header_subject || "X-authentik-uid"}`;
    } else {
      kpiSsoSub.innerText = "Local password authentication only";
    }
  }
}

function renderHouseholdUsers(users) {
  const tbody = document.getElementById("adminUsersTableBody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!users || users.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="padding: 14px; color: var(--text-secondary);">No user accounts configured.</td></tr>`;
    return;
  }

  users.forEach(u => {
    const uid = u.id || u.user_id || "";
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid var(--border-subtle)";
    const isAdmin = u.role === "admin";
    const isSuper = Boolean(u.is_super_admin);
    const isSelf = Boolean(u.is_self || (currentUsersMeta.current_user_id && currentUsersMeta.current_user_id === uid));
    const canEdit = (u.can_edit !== undefined) ? Boolean(u.can_edit) : (!isSuper || currentUsersMeta.current_is_super_admin || isSelf);
    const canChangeRole = (u.can_change_role !== undefined) ? Boolean(u.can_change_role) : (!isSuper && !isSelf);
    const canDelete = (u.can_delete !== undefined) ? Boolean(u.can_delete) : (!isSuper && !isSelf);

    const ssoProv = (u.sso_provider || "").trim();
    const ssoSub = (u.sso_subject || "").trim();
    let authBadge = `<span class="badge badge-subtle">🔑 Local</span>`;
    if (ssoProv && ssoSub) {
      authBadge = `<span class="badge badge-info" title="Immutable SSO Subject bound: ${escapeHtml(ssoSub)}">🛡️ ${escapeHtml(ssoProv)} · UID</span>`;
    } else if (ssoProv) {
      authBadge = `<span class="badge badge-info" title="Pre-linked to ${escapeHtml(ssoProv)}; UID binds on first SSO login">🛡️ ${escapeHtml(ssoProv)} · Pending</span>`;
    } else if (ssoSub) {
      authBadge = `<span class="badge badge-info" title="Bound SSO Subject: ${escapeHtml(ssoSub)}">🛡️ SSO · UID</span>`;
    }

    const emailSub = u.email
      ? `<div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${escapeHtml(u.email)}</div>`
      : "";
    const absSub = u.abs_username
      ? `<div style="font-size: 11px; color: #93c5fd; margin-top: 2px;">🎧 ABS: ${escapeHtml(u.abs_username)}</div>`
      : (u.has_abs_api_key ? `<div style="font-size: 11px; color: #93c5fd; margin-top: 2px;">🎧 ABS Linked</div>` : "");
    const lastLogin = u.last_login ? escapeHtml(String(u.last_login).replace("T", " ").slice(0, 16)) : `<span style="color: var(--text-muted);">Never</span>`;

    const superBadge = isSuper ? ` <span class="badge badge-warning" title="Primary Super Admin account — cannot be modified or deleted by other admins">👑 Super Admin</span>` : "";
    const selfBadge = isSelf ? ` <span class="badge badge-subtle">You</span>` : "";

    let roleCellHtml = "";
    if (isSuper) {
      roleCellHtml = `<span class="badge badge-warning">👑 Super Admin (Locked)</span>`;
    } else if (!canChangeRole) {
      roleCellHtml = `<span class="badge badge-info">${isAdmin ? "🛡️ Admin" : "👤 Member"} (Current Session)</span>`;
    } else {
      const rVal = (u.role || "member").toLowerCase();
      roleCellHtml = `
        <select class="form-control" style="width: 155px; font-size: 12px; padding: 4px 8px;" onchange="changeHouseholdUserRole('${escapeHtml(uid)}', this.value)">
          <option value="admin" ${rVal === 'admin' ? 'selected' : ''}>🛡️ Admin</option>
          <option value="member" ${rVal === 'member' ? 'selected' : ''}>👤 Member</option>
          <option value="guest" ${rVal === 'guest' ? 'selected' : ''}>🏠 Guest</option>
        </select>
      `;
    }

    let actionsCellHtml = "";
    if (!canEdit) {
      actionsCellHtml = `<span class="badge badge-subtle" title="The primary Super Admin account cannot be managed by other administrators">🔒 Protected</span>`;
    } else {
      actionsCellHtml = `
        <button type="button" class="btn btn-secondary btn-sm" style="font-size: 11px;" onclick="openEditUserModal('${escapeHtml(uid)}')">✏️ Edit</button>
        ${canDelete ? `<button type="button" class="btn btn-danger btn-sm" style="font-size: 11px; margin-left: 4px;" onclick="deleteHouseholdUser('${escapeHtml(uid)}', '${escapeHtml(u.username)}')">🗑️</button>` : ""}
      `;
    }

    tr.innerHTML = `
      <td style="padding: 10px 12px; font-weight: 600;">${escapeHtml(u.username)}${superBadge}${selfBadge}${emailSub}${absSub}</td>
      <td style="padding: 10px 12px;">${escapeHtml(u.display_name || u.username)}</td>
      <td style="padding: 10px 12px;">${authBadge}</td>
      <td style="padding: 10px 12px;">${roleCellHtml}</td>
      <td style="padding: 10px 12px; font-size: 12px; color: var(--text-secondary);">${lastLogin}</td>
      <td style="padding: 10px 12px; text-align: right; white-space: nowrap;">${actionsCellHtml}</td>
    `;
    tbody.appendChild(tr);
  });
}


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

function onNewUserAuthSourceChange() {
  const sourceEl = document.getElementById("newAdminAuthSource");
  const pwEl = document.getElementById("newAdminPassword");
  if (!sourceEl || !pwEl) return;

  if (sourceEl.value) {
    pwEl.value = "";
    pwEl.disabled = true;
    pwEl.placeholder = "Managed by SSO / IdP";
  } else {
    pwEl.disabled = false;
    pwEl.placeholder = "Min 8 chars";
  }
}

async function createNewHouseholdUser() {
  const usernameEl = document.getElementById("newAdminUsername");
  const displayEl = document.getElementById("newAdminDisplayName");
  const emailEl = document.getElementById("newAdminEmail");
  const roleEl = document.getElementById("newAdminRole");
  const sourceEl = document.getElementById("newAdminAuthSource");
  const passwordEl = document.getElementById("newAdminPassword");

  const username = usernameEl?.value.trim() || "";
  const display_name = displayEl?.value.trim() || username;
  const email = emailEl?.value.trim() || "";
  const role = roleEl?.value || "member";
  const sso_provider = sourceEl?.value.trim() || "";
  const password = passwordEl?.value || "";

  if (!username) {
    showToast("Username is required.", "warning");
    return;
  }
  if (!sso_provider && password.length < 8) {
    showToast("Password must be at least 8 characters for local accounts.", "warning");
    return;
  }

  try {
    const res = await fetch("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, display_name, email, role, sso_provider, password })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Created ${sso_provider ? "SSO-linked " : ""}user '${username}' (${role}).`, "success");
      if (usernameEl) usernameEl.value = "";
      if (displayEl) displayEl.value = "";
      if (emailEl) emailEl.value = "";
      if (passwordEl) passwordEl.value = "";
      if (sourceEl) sourceEl.value = "";
      onNewUserAuthSourceChange();
      await loadHouseholdUsers();
    } else {
      showToast(data.error || "Failed to create user.", "danger");
    }
  } catch (err) {
    showToast("Network error creating user.", "danger");
  }
}

async function changeHouseholdUserRole(userId, role) {
  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast("Updated user role.", "success");
      await loadHouseholdUsers();
    } else {
      showToast(data.error || "Failed to update role.", "danger");
      await loadHouseholdUsers();
    }
  } catch (err) {
    showToast("Network error updating role.", "danger");
  }
}

async function deleteHouseholdUser(userId, username) {
  if (!confirm(`Delete user account '${username}'?`)) return;
  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "DELETE"
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Deleted user '${username}'.`, "success");
      await loadHouseholdUsers();
    } else {
      showToast(data.error || "Failed to delete user.", "danger");
    }
  } catch (err) {
    showToast("Network error deleting user.", "danger");
  }
}

// ----------------------------------------------------------------------------
// LAN Guest Policy & SSO / Authentik Configuration
// ----------------------------------------------------------------------------

function readSsoFormValues() {
  const extraRaw = document.getElementById("ssoExtraTrustedProxies")?.value || "";
  const extraList = extraRaw.split(",").map(s => s.trim()).filter(Boolean);
  return {
    forward_auth_enabled: Boolean(document.getElementById("ssoForwardAuthEnabled")?.checked),
    provider_name: document.getElementById("ssoProviderName")?.value || "Authentik",
    default_role: document.getElementById("ssoDefaultRole")?.value || "member",
    admin_group: document.getElementById("ssoAdminGroup")?.value.trim() || "",
    member_group: document.getElementById("ssoMemberGroup")?.value.trim() || "",
    header_subject: document.getElementById("ssoHeaderSubject")?.value.trim() || "X-authentik-uid",
    header_username: document.getElementById("ssoHeaderUsername")?.value.trim() || "X-authentik-username",
    header_email: document.getElementById("ssoHeaderEmail")?.value.trim() || "X-authentik-email",
    header_display_name: document.getElementById("ssoHeaderDisplayName")?.value.trim() || "X-authentik-name",
    header_groups: document.getElementById("ssoHeaderGroups")?.value.trim() || "X-authentik-groups",
    extra_trusted_proxies: extraList,
    logout_url: document.getElementById("ssoLogoutUrl")?.value.trim() || "",
    auto_link_existing_users: Boolean(document.getElementById("ssoAutoLinkExistingUsers")?.checked),
    auto_provision_users: Boolean(document.getElementById("ssoAutoProvisionUsers")?.checked),
    oidc_enabled: Boolean(document.getElementById("ssoOidcEnabled")?.checked),
    oidc_issuer_url: document.getElementById("ssoOidcIssuerUrl")?.value.trim() || "",
    oidc_client_id: document.getElementById("ssoOidcClientId")?.value.trim() || "",
    oidc_client_secret: document.getElementById("ssoOidcClientSecret")?.value.trim() || ""
  };
}

function setSsoDirty(isDirty) {
  const badge = document.getElementById("unsaved-sso");
  if (badge) badge.style.display = isDirty ? "inline-flex" : "none";
  const btn = document.getElementById("btnSaveSsoConfig");
  if (btn) btn.classList.toggle("btn-unsaved", Boolean(isDirty));
}

function checkSsoDirty() {
  if (!ssoBaselineSnapshot) return;
  const cur = JSON.stringify(readSsoFormValues());
  setSsoDirty(cur !== ssoBaselineSnapshot);
}

function bindSsoDirtyListeners() {
  if (ssoDirtyBound) return;
  const form = document.getElementById("ssoConfigForm");
  if (!form) return;
  ssoDirtyBound = true;
  form.querySelectorAll("input, select").forEach((el) => {
    if (el.id === "ssoOidcCallbackUri") return;
    el.addEventListener("input", checkSsoDirty);
    el.addEventListener("change", checkSsoDirty);
  });
}

async function loadSsoAccessConfig() {
  try {
    const res = await fetch("/api/admin/auth/sso-config", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    cachedSsoMeta = data;
    renderSsoConfig(data);
  } catch (err) {
    console.error("Failed to load SSO config:", err);
  }
}

function renderSsoConfig(data) {
  const cfg = data.sso_config || {};

  const toggleLan = document.getElementById("toggleLanSitterAccess");
  const lanLabel = document.getElementById("lanSitterToggleLabel");
  if (toggleLan) {
    toggleLan.checked = Boolean(data.lan_sitter_access_enabled);
  }
  if (lanLabel) {
    lanLabel.innerText = data.lan_sitter_access_enabled ? "Enabled" : "Disabled";
  }

  const envEl = document.getElementById("ssoEnvTrustedProxies");
  if (envEl) {
    const envProxies = Array.isArray(data.env_trusted_proxies) ? data.env_trusted_proxies : [];
    envEl.innerText = envProxies.length ? envProxies.join(", ") : "None";
  }

  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = val ?? "";
  };
  const setChecked = (id, flag) => {
    const el = document.getElementById(id);
    if (el) el.checked = Boolean(flag);
  };

  const rawProvider = String(cfg.provider_name || "Authentik").trim();
  const provEl = document.getElementById("ssoProviderName");
  const knownProviders = provEl ? Array.from(provEl.options).map(o => o.value) : [];
  const normalizedProvider = (!rawProvider || ["reverse proxy", "generic forwardauth"].includes(rawProvider.toLowerCase()) || (knownProviders.length && !knownProviders.includes(rawProvider)))
    ? "SSO"
    : rawProvider;

  setChecked("ssoForwardAuthEnabled", cfg.forward_auth_enabled !== false);
  setVal("ssoProviderName", normalizedProvider);
  setVal("ssoDefaultRole", cfg.default_role || "member");
  setVal("ssoAdminGroup", cfg.admin_group || "");
  setVal("ssoMemberGroup", cfg.member_group || "");
  setVal("ssoHeaderSubject", cfg.header_subject || "X-authentik-uid");
  setVal("ssoHeaderUsername", cfg.header_username || "X-authentik-username");
  setVal("ssoHeaderEmail", cfg.header_email || "X-authentik-email");
  setVal("ssoHeaderDisplayName", cfg.header_display_name || "X-authentik-name");
  setVal("ssoHeaderGroups", cfg.header_groups || "X-authentik-groups");
  setVal("ssoExtraTrustedProxies", Array.isArray(cfg.extra_trusted_proxies) ? cfg.extra_trusted_proxies.join(", ") : "");
  setVal("ssoLogoutUrl", cfg.logout_url || "");
  setChecked("ssoAutoLinkExistingUsers", cfg.auto_link_existing_users !== false);
  setChecked("ssoAutoProvisionUsers", cfg.auto_provision_users !== false);
  setChecked("ssoOidcEnabled", cfg.oidc_enabled);
  setVal("ssoOidcIssuerUrl", cfg.oidc_issuer_url || "");
  setVal("ssoOidcClientId", cfg.oidc_client_id || "");
  setVal("ssoOidcClientSecret", cfg.oidc_client_secret || "");

  const cbPath = data.oidc_callback_path || "/api/auth/oidc/callback";
  setVal("ssoOidcCallbackUri", `${window.location.origin}${cbPath}`);

  const suggestedLogoutUri = `${window.location.origin}/login`;
  setVal("ssoOidcLogoutReturnUri", suggestedLogoutUri);
  const suggestedLogoutText = document.getElementById("ssoSuggestedLogoutText");
  if (suggestedLogoutText) suggestedLogoutText.innerText = suggestedLogoutUri;

  const badge = document.getElementById("ssoConfigStatusBadge");
  if (badge) {
    const fwdActive = cfg.forward_auth_enabled !== false;
    const oidcActive = Boolean(cfg.oidc_enabled);
    badge.className = (fwdActive || oidcActive) ? "badge badge-info" : "badge badge-subtle";
    if (oidcActive && fwdActive) {
      badge.innerText = `OIDC + ForwardAuth: ${normalizedProvider}`;
    } else if (oidcActive) {
      badge.innerText = `OIDC: ${normalizedProvider}`;
    } else if (fwdActive) {
      badge.innerText = `ForwardAuth: ${normalizedProvider}`;
    } else {
      badge.innerText = "SSO Disabled";
    }
  }

  ssoBaselineSnapshot = JSON.stringify(readSsoFormValues());
  setSsoDirty(false);
  updateSsoModeWarning();
  bindSsoDirtyListeners();
  updateUsersKpis();
}

function updateSsoModeWarning() {
  const warn = document.getElementById("ssoDualModeWarning");
  if (!warn) return;
  const fa = Boolean(document.getElementById("ssoForwardAuthEnabled")?.checked);
  const oidc = Boolean(document.getElementById("ssoOidcEnabled")?.checked);
  warn.style.display = (fa && oidc) ? "block" : "none";
}

async function saveLanSitterToggle() {
  const toggleLan = document.getElementById("toggleLanSitterAccess");
  const lanLabel = document.getElementById("lanSitterToggleLabel");
  if (!toggleLan) return;
  const enabled = Boolean(toggleLan.checked);

  try {
    const res = await fetch("/api/admin/auth/sso-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lan_sitter_access_enabled: enabled })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      cachedSsoMeta = data;
      if (lanLabel) lanLabel.innerText = enabled ? "Enabled" : "Disabled";
      updateUsersKpis();
      showToast(`LAN Guest Access ${enabled ? "enabled" : "disabled"}.`, "success");
    } else {
      toggleLan.checked = !enabled;
      showToast(data.error || "Failed to update LAN guest setting.", "danger");
    }
  } catch (err) {
    toggleLan.checked = !enabled;
    showToast("Network error updating LAN guest policy.", "danger");
  }
}

async function saveSsoConfig() {
  const btn = document.getElementById("btnSaveSsoConfig");
  if (btn) {
    btn.disabled = true;
    btn.innerText = "💾 Saving...";
  }

  const ssoPayload = readSsoFormValues();
  try {
    const res = await fetch("/api/admin/auth/sso-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sso_config: ssoPayload })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      cachedSsoMeta = data;
      renderSsoConfig(data);
      showToast("SSO & Identity Provider configuration saved.", "success");
    } else {
      showToast(data.error || "Failed to save SSO configuration.", "danger");
    }
  } catch (err) {
    showToast("Network error saving SSO configuration.", "danger");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = "💾 Save Access & SSO Settings";
    }
  }
}

function copyOidcCallbackUri() {
  const el = document.getElementById("ssoOidcCallbackUri");
  if (!el || !el.value) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(el.value).then(() => {
      showToast("Copied OIDC Callback URI to clipboard.", "success");
    }).catch(() => {
      el.select();
      showToast("Select and copy the callback URI field.", "info");
    });
  } else {
    el.select();
    showToast("Select and copy the callback URI field.", "info");
  }
}

function copyOidcLogoutReturnUri() {
  const el = document.getElementById("ssoOidcLogoutReturnUri");
  if (!el || !el.value) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(el.value).then(() => {
      showToast("Copied Suggested Logout Return URI to clipboard.", "success");
    }).catch(() => {
      el.select();
      showToast("Select and copy the logout return URI field.", "info");
    });
  } else {
    el.select();
    showToast("Select and copy the logout return URI field.", "info");
  }
}

function applySuggestedLogoutUri() {
  const logoutEl = document.getElementById("ssoLogoutUrl");
  if (!logoutEl) return;
  logoutEl.value = `${window.location.origin}/login`;
  checkSsoDirty();
  showToast("Set SSO Logout Redirect URL to StoryDeck login page.", "success");
}

async function testOidcDiscovery() {
  const urlInput = document.getElementById("ssoOidcIssuerUrl");
  const btn = document.getElementById("btnTestOidcDiscovery");
  const box = document.getElementById("oidcDiscoveryResult");
  const rawUrl = (urlInput?.value || "").trim();

  if (!rawUrl) {
    showToast("Enter an OIDC Discovery or Issuer URL first.", "warning");
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerText = "🔍 Probing...";
  }
  if (box) {
    box.style.display = "none";
  }

  try {
    const res = await fetch("/api/admin/auth/oidc-discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: rawUrl })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      if (box) {
        box.style.display = "block";
        box.style.background = "rgba(16, 185, 129, 0.08)";
        box.style.borderColor = "rgba(16, 185, 129, 0.35)";
        box.innerHTML =
          `<strong>✅ OIDC Auto-Discovery Succeeded</strong><br>` +
          `<span class="text-secondary">Issuer:</span> <code>${escapeHtml(data.issuer || "")}</code><br>` +
          `<span class="text-secondary">Authorization:</span> <code>${escapeHtml(data.authorization_endpoint || "")}</code><br>` +
          `<span class="text-secondary">Token:</span> <code>${escapeHtml(data.token_endpoint || "")}</code>` +
          (data.end_session_endpoint ? `<br><span class="text-secondary">End Session:</span> <code>${escapeHtml(data.end_session_endpoint)}</code>` : "");
      }
      const logoutEl = document.getElementById("ssoLogoutUrl");
      if (logoutEl && !logoutEl.value.trim()) {
        logoutEl.value = `${window.location.origin}/login`;
        checkSsoDirty();
        showToast("OIDC discovered! Suggested StoryDeck return URL filled for Logout Redirect.", "success");
      } else {
        showToast("OIDC Auto-Discovery verified successfully.", "success");
      }
    } else {
      if (box) {
        box.style.display = "block";
        box.style.background = "rgba(239, 68, 68, 0.08)";
        box.style.borderColor = "rgba(239, 68, 68, 0.35)";
        box.innerHTML = `<strong>❌ Discovery Failed:</strong> ${escapeHtml(data.error || "Unable to fetch .well-known/openid-configuration")}`;
      }
      showToast(data.error || "OIDC discovery failed.", "danger");
    }
  } catch (err) {
    showToast("Network error probing OIDC discovery URL.", "danger");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = "🔍 Test Auto-Discovery";
    }
  }
}

async function testForwardAuthProxy() {
  const btn = document.getElementById("btnTestProxyHeaders");
  const box = document.getElementById("proxyCheckResult");
  if (btn) { btn.disabled = true; btn.innerText = "🔍 Checking..."; }
  if (box) { box.style.display = "none"; }

  try {
    const res = await fetch("/api/admin/auth/proxy-check", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      showToast(data.error || "Proxy check failed.", "danger");
      return;
    }
    const trustBadge = data.peer_trusted
      ? `<span style="color:#4ade80; font-weight:600;">✅ Trusted</span>`
      : `<span style="color:#fbbf24; font-weight:600;">⚠️ Not Trusted (add <code>${escapeHtml(data.peer_ip)}</code> to Trusted Proxies)</span>`;

    const hdrRows = Object.entries(data.headers || {}).map(([label, info]) => {
      const status = info.present
        ? `<span style="color:#4ade80;">✅ ${escapeHtml(info.value)}</span>`
        : `<span class="text-secondary">— not present</span>`;
      return `<div><span class="text-secondary">${escapeHtml(label)} (<code>${escapeHtml(info.header)}</code>):</span> ${status}</div>`;
    }).join("");

    if (box) {
      box.style.display = "block";
      box.style.background = data.peer_trusted ? "rgba(16, 185, 129, 0.08)" : "rgba(245, 158, 11, 0.08)";
      box.style.borderColor = data.peer_trusted ? "rgba(16, 185, 129, 0.35)" : "rgba(245, 158, 11, 0.4)";
      box.innerHTML =
        `<div><strong>Peer IP:</strong> <code>${escapeHtml(data.peer_ip)}</code> — ${trustBadge}</div>` +
        `<div><strong>Client IP:</strong> <code>${escapeHtml(data.effective_client_ip)}</code> &nbsp;|&nbsp; <strong>Proto:</strong> <code>${escapeHtml(data.x_forwarded_proto)}</code></div>` +
        `<div style="margin-top:6px; padding-top:6px; border-top:1px solid rgba(255,255,255,0.08);">${hdrRows}</div>`;
    }
  } catch {
    showToast("Network error running proxy check.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = "🔍 Check Proxy Headers"; }
  }
}
