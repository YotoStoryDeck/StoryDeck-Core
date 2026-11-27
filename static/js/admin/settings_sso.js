// ============================================================================
// LAN Guest Policy & SSO / Authentik / OIDC Configuration
// ============================================================================

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

  setChecked("ssoForwardAuthEnabled", Boolean(cfg.forward_auth_enabled));
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
    const fwdActive = Boolean(cfg.forward_auth_enabled);
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
