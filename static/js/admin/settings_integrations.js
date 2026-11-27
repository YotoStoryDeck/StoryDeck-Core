// ============================================================================
// Admin Integration Settings (Yoto, ABS, Tinker, OpenRouter) & Health Checks
// ============================================================================

async function saveApiCredentialsPartial(payload, notify = true) {
  try {
    const res = await fetch("/api/admin/settings/credentials", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      if (notify) showToast("Settings saved and services reloaded!", "success");
      return true;
    } else {
      if (notify) showToast(data.error || "Failed to save settings.", "danger");
      return false;
    }
  } catch (err) {
    if (notify) showToast("Network error saving settings.", "danger");
    return false;
  }
}

async function saveYotoSettings() {
  const btn = document.getElementById("btnSaveYoto");
  if (btn) { btn.disabled = true; btn.innerText = "💾 Saving..."; }

  const clientId = document.getElementById("credYotoClientId")?.value.trim() || "";
  const refresh = document.getElementById("credYotoRefresh")?.value.trim() || "";

  const success = await saveApiCredentialsPartial({
    yoto_client_id: clientId,
    yoto_refresh_token: refresh
  });

  if (btn) { btn.disabled = false; btn.innerText = "💾 Save Yoto Settings"; }
  if (success) {
    snapshotBaseline("yoto");
    checkDirty("yoto");
    updateConnectionBadges();
    const statusEl = document.getElementById("yotoStatusInline");
    if (statusEl) statusEl.innerText = "Saved. Click Test Connection to verify.";
  }
}

function renderAbsAdminPermissionNotice(perm) {
  const el = document.getElementById("absAdminPermissionNotice");
  if (!el) return;
  if (!perm || !perm.connected) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  el.style.display = "block";
  if (perm.is_admin) {
    el.style.background = "rgba(16, 185, 129, 0.08)";
    el.style.border = "1px solid rgba(16, 185, 129, 0.25)";
    el.style.color = "var(--text-primary)";
    const rootNote = (!perm.is_root && perm.note)
      ? `<div class="text-secondary" style="margin-top: 4px; font-size: 11px;">ℹ️ ${escapeHtml(perm.note)}</div>`
      : "";
    el.innerHTML = `✅ <strong>Admin Master Key Verified</strong> (<code>${escapeHtml(perm.username || "admin")}</code> • <code>${escapeHtml(perm.type || "admin")}</code>) — StoryDeck can automatically match users and auto-generate scoped ABS API keys for profiles and household users.${rootNote}`;
  } else {
    el.style.background = "rgba(245, 158, 11, 0.12)";
    el.style.border = "1px solid rgba(245, 158, 11, 0.4)";
    el.style.color = "#fde68a";
    const warnMsg = perm.warning || "Master API key is not affiliated with an Admin Audiobookshelf user. An ABS Admin will need to manually generate 'Act on behalf of' API keys for users and profiles for personalized listening settings.";
    el.innerHTML = `⚠️ <strong>Non-Admin Master API Key (${escapeHtml(perm.username || "user")} • ${escapeHtml(perm.type || "user")}):</strong> ${escapeHtml(warnMsg)}`;
  }
}

async function checkAbsMasterKeyPermissions() {
  try {
    const res = await fetch("/api/admin/abs/permission-status");
    if (!res.ok) return null;
    const data = await res.json();
    if (data && data.ok) {
      const toggleEl = document.getElementById("toggleAbsUserSelfServiceKeyGen");
      if (toggleEl && "allow_user_self_service_key_gen" in data) {
        toggleEl.checked = Boolean(data.allow_user_self_service_key_gen);
      }
      renderAbsAdminPermissionNotice(data);
      return data;
    }
  } catch (err) {
    console.debug("ABS permission status check notice:", err);
  }
  return null;
}

async function updateAbsSelfServiceSetting(enabled) {
  try {
    const res = await fetch("/api/admin/abs/permission-status", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_user_self_service_key_gen: Boolean(enabled) })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(
        enabled
          ? "User self-service ABS linking & key generation enabled."
          : "User self-service ABS key generation disabled (Admin updates required).",
        "success"
      );
    } else {
      showToast(data.error || "Failed to update ABS self-service policy.", "danger");
    }
  } catch {
    showToast("Network error updating ABS self-service policy.", "danger");
  }
}

async function saveAbsSettings() {
  const btn = document.getElementById("btnSaveAbs");
  if (btn) { btn.disabled = true; btn.innerText = "💾 Saving..."; }

  const url = document.getElementById("credAbsUrl").value.trim();
  const key = document.getElementById("credAbsKey").value.trim();
  const allowSelf = document.getElementById("toggleAbsUserSelfServiceKeyGen")?.checked ?? true;

  const success = await saveApiCredentialsPartial({
    abs_base_url: url,
    abs_api_key: key,
    abs_allow_user_self_service_key_gen: allowSelf
  });

  if (btn) { btn.disabled = false; btn.innerText = "💾 Save ABS Settings"; }
  if (success) {
    snapshotBaseline("abs");
    checkDirty("abs");
    updateConnectionBadges();
    const statusEl = document.getElementById("absStatusInline");
    if (statusEl) statusEl.innerText = "Saved. Checking ABS Master Key permissions...";
    const perm = await checkAbsMasterKeyPermissions();
    if (perm && perm.connected && !perm.is_admin && perm.warning) {
      showToast(perm.warning, "warning");
    }
  }
}

async function saveTinkerSettings() {
  const btn = document.getElementById("btnSaveTinker");
  if (btn) { btn.disabled = true; btn.innerText = "💾 Saving..."; }

  const key = document.getElementById("credTinkerKey")?.value.trim() || "";

  const success = await saveApiCredentialsPartial({
    tinker_api_key: key
  });

  if (btn) { btn.disabled = false; btn.innerText = "💾 Save Tinker Settings"; }
  if (success) {
    snapshotBaseline("tinker");
    checkDirty("tinker");
    updateConnectionBadges();
    const statusEl = document.getElementById("tinkerStatusInline");
    if (statusEl) statusEl.innerText = "Saved. Click Test API Key to verify.";
  }
}

async function saveAiSettings() {
  const btn = document.getElementById("btnSaveAi");
  if (btn) { btn.disabled = true; btn.innerText = "💾 Saving..."; }

  const key = document.getElementById("credOpenRouterKey")?.value.trim() || "";

  const success = await saveApiCredentialsPartial({
    openrouter_api_key: key
  });

  if (btn) { btn.disabled = false; btn.innerText = "💾 Save AI Settings"; }
  if (success) {
    snapshotBaseline("ai");
    checkDirty("ai");
    updateConnectionBadges();
    const statusEl = document.getElementById("aiStatusInline");
    if (statusEl) statusEl.innerText = "Saved. Click Test API Key to verify.";
  }
}

async function testServiceConnection(target) {
  const statusEl = document.getElementById(`${target}StatusInline`);
  const btn = document.getElementById(`btnTest${target.charAt(0).toUpperCase() + target.slice(1)}`);

  if (btn) btn.disabled = true;
  if (statusEl) {
    statusEl.innerHTML = `<span style="color: var(--accent-info);">⏳ Testing ${target.toUpperCase()}...</span>`;
  }

  showToast(`Testing ${target.toUpperCase()}...`, "info");

  try {
    const res = await fetch(`/api/admin/test/${target}`, { method: "POST" });
    const data = await res.json();
    if (res.ok && data.ok) {
      const msg = data.message || "OK";
      if (statusEl) {
        statusEl.innerHTML = `<span style="color: var(--accent-success);">✅ ${escapeHtml(msg)}</span>`;
      }
      updateBadge(target, true);
      if (target === "abs" && "is_abs_admin" in data) {
        renderAbsAdminPermissionNotice({
          connected: true,
          is_admin: Boolean(data.is_abs_admin),
          username: data.abs_username || "",
          type: data.abs_account_type || "",
          warning: data.warning || "",
        });
        if (!data.is_abs_admin && data.warning) {
          showToast(data.warning, "warning");
        } else {
          showToast(`${target.toUpperCase()} PASSED: ${msg}`, "success");
        }
      } else {
        showToast(`${target.toUpperCase()} PASSED: ${msg}`, "success");
      }
    } else {
      const err = data.error || "Connection check failed.";
      if (statusEl) {
        statusEl.innerHTML = `<span style="color: var(--accent-danger);">❌ ${escapeHtml(err)}</span>`;
      }
      updateBadge(target, false);
      showToast(`${target.toUpperCase()} failed: ${err}`, "danger");
    }
  } catch (err) {
    if (statusEl) {
      statusEl.innerHTML = `<span style="color: var(--accent-danger);">❌ Network error reaching endpoint.</span>`;
    }
    updateBadge(target, false);
    showToast(`Network error testing ${target}.`, "danger");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ----------------------------------------------------------------------------
// Cache Maintenance
// ----------------------------------------------------------------------------

async function purgeImageCache() {
  if (!confirm("Are you sure you want to clear all downscaled thumbnails and AI icons?")) return;
  try {
    const res = await fetch("/api/admin/cache/purge", { method: "POST" });
    const data = await res.json();
    showToast(`Purged ${data.files_purged || 0} cached images.`, "success");
  } catch (err) {
    showToast("Failed to purge image cache.", "danger");
  }
}
