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

