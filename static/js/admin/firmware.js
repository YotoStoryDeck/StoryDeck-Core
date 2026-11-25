// ============================================================================
// Firmware Repository, Remote OTA Deployment & MicroSD Cache Controls (DLP-010, DLP-020)
// ============================================================================

let currentFirmwareReleases = [];
let pushFirmwareTargetMac = null;
let selectedPushFirmwareVersion = null;
let otaProgressPollTimer = null;

async function loadFirmwareReleases() {
  try {
    const res = await fetch("/api/admin/firmware");
    if (!res.ok) return;
    currentFirmwareReleases = await res.json();
    renderFirmwareReleasesTable();
    // Non-blocking soft check for upstream releases to update notifications
    loadUpstreamTouchReleases(false).catch(() => {});
  } catch (err) {
    console.error("Error loading firmware releases:", err);
  }
}

function renderFirmwareReleasesTable() {
  const tbody = document.getElementById("firmwareReleasesBody");
  const countBadge = document.getElementById("firmwareCountBadge");
  const latestBadge = document.getElementById("firmwareLatestBadge");
  if (!tbody) return;

  const list = Array.isArray(currentFirmwareReleases) ? currentFirmwareReleases : [];
  if (countBadge) {
    countBadge.textContent = `${list.length} release${list.length === 1 ? "" : "s"}`;
  }
  const activeLatest = list.find(r => r.is_active_latest);
  if (latestBadge) {
    latestBadge.textContent = activeLatest ? `Latest: ${activeLatest.version}` : "Latest: None";
  }

  tbody.innerHTML = "";
  if (list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">No firmware binaries uploaded yet. Click 'Upload Firmware (.bin)' to add one.</td></tr>`;
    return;
  }

  list.forEach(r => {
    const tr = document.createElement("tr");
    const shortSha = (r.sha256_hash || "").slice(0, 12);
    const statusBadge = r.is_active_latest
      ? `<span class="badge badge-success">★ Active Latest</span>`
      : `<span class="badge badge-subtle">Archived</span>`;
    const fileMissingWarn = !r.file_exists
      ? `<span class="badge badge-danger" style="margin-left: 4px;">Missing .bin</span>`
      : "";
    const commitHtml = r.git_commit
      ? `<div style="font-size: 11px; font-family: monospace; color: var(--text-muted);">${escapeHtml(r.git_commit)}</div>`
      : "";

    tr.innerHTML = `
      <td><strong style="font-family: monospace;">${escapeHtml(r.version)}</strong></td>
      <td>
        <div>${escapeHtml(r.release_tag || r.version)}</div>
        ${commitHtml}
      </td>
      <td>${escapeHtml(r.file_size_human || "")}</td>
      <td><code title="${escapeHtml(r.sha256_hash || "")}" style="font-size: 11px;">${escapeHtml(shortSha)}...</code></td>
      <td style="max-width: 360px; font-size: 12px; color: var(--text-secondary); line-height: 1.5;">${formatTruncatedReleaseNotes(r.release_notes, r.version, 500)}</td>
      <td>${statusBadge}${fileMissingWarn}</td>
      <td style="white-space: nowrap;">
        <div style="display: flex; gap: 6px;">
          ${!r.is_active_latest
            ? `<button type="button" class="btn btn-secondary btn-sm" onclick="setLatestFirmwareRelease('${escapeHtml(r.version)}')">★ Set Latest</button>`
            : ""
          }
          <button type="button" class="btn btn-secondary btn-sm" style="color: var(--accent-danger);" onclick="deleteFirmwareRelease('${escapeHtml(r.version)}')">🗑️ Delete</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function renderDeviceOtaBadge(device) {
  const status = (device.ota_status || "idle").toLowerCase();
  const prog = Number(device.ota_progress || 0);
  const target = device.target_firmware_version ? ` → ${escapeHtml(device.target_firmware_version)}` : "";

  if (status === "idle" && prog === 0 && !device.ota_last_error) {
    return "";
  }
  if (status === "idle" && prog === 100) {
    return `<div style="margin-top: 4px;"><span class="badge badge-success" style="font-size: 10px;">✓ OTA Verified</span></div>`;
  }
  if (status === "pending" || status === "downloading" || status === "flashing" || status === "flashing_done") {
    return `
      <div style="margin-top: 4px;">
        <span class="badge badge-warning" style="font-size: 10px;">⏳ ${escapeHtml(status.toUpperCase())} ${prog}%${target}</span>
      </div>
    `;
  }
  if (status === "failed" || status === "rolled_back") {
    const errTitle = device.ota_last_error ? `title="${escapeHtml(device.ota_last_error)}"` : "";
    return `
      <div style="margin-top: 4px;">
        <span class="badge badge-danger" ${errTitle} style="font-size: 10px;">⚠️ ${escapeHtml(status.replace("_", " ").toUpperCase())}</span>
      </div>
    `;
  }
  return "";
}

function syncOtaProgressPolling() {
  const list = Array.isArray(currentDevices) ? currentDevices : [];
  const activeOta = list.some(d => {
    const st = (d.ota_status || "idle").toLowerCase();
    return st === "pending" || st === "downloading" || st === "flashing" || st === "flashing_done";
  });

  if (activeOta && !otaProgressPollTimer) {
    otaProgressPollTimer = setInterval(() => {
      if (typeof loadDevices === "function") loadDevices();
    }, 3000);
  } else if (!activeOta && otaProgressPollTimer) {
    clearInterval(otaProgressPollTimer);
    otaProgressPollTimer = null;
  }
}

function openUploadFirmwareModal() {
  const fileEl = document.getElementById("uploadFirmwareFile");
  if (fileEl) fileEl.value = "";
  document.getElementById("uploadFirmwareVersion").value = "";
  document.getElementById("uploadFirmwareTag").value = "";
  document.getElementById("uploadFirmwareCommit").value = "";
  document.getElementById("uploadFirmwareNotes").value = "";
  document.getElementById("uploadFirmwareSetLatest").checked = true;
  document.getElementById("uploadFirmwareModal").style.display = "flex";
}

function closeUploadFirmwareModal() {
  const modal = document.getElementById("uploadFirmwareModal");
  if (modal) modal.style.display = "none";
}

async function submitUploadFirmware() {
  const fileEl = document.getElementById("uploadFirmwareFile");
  const version = document.getElementById("uploadFirmwareVersion").value.trim();
  const tag = document.getElementById("uploadFirmwareTag").value.trim();
  const commit = document.getElementById("uploadFirmwareCommit").value.trim();
  const notes = document.getElementById("uploadFirmwareNotes").value.trim();
  const setLatest = document.getElementById("uploadFirmwareSetLatest").checked;

  if (!fileEl || !fileEl.files || fileEl.files.length === 0) {
    showToast("Please select a .bin firmware file.", "danger");
    return;
  }
  if (!version) {
    showToast("Please enter a semantic version (e.g. v1.2.1).", "danger");
    return;
  }

  const formData = new FormData();
  formData.append("file", fileEl.files[0]);
  formData.append("version", version);
  if (tag) formData.append("release_tag", tag);
  if (commit) formData.append("git_commit", commit);
  if (notes) formData.append("release_notes", notes);
  formData.append("is_active_latest", setLatest ? "true" : "false");

  try {
    const res = await fetch("/api/admin/firmware/upload", {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Uploaded firmware ${version}!`, "success");
      closeUploadFirmwareModal();
      await loadFirmwareReleases();
    } else {
      showToast(data.error || `Upload failed (HTTP ${res.status})`, "danger");
    }
  } catch (err) {
    showToast("Network error uploading firmware: " + err.message, "danger");
  }
}

async function setLatestFirmwareRelease(version) {
  try {
    const res = await fetch(`/api/admin/firmware/${encodeURIComponent(version)}/set-latest`, {
      method: "POST",
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`${version} marked as active latest release.`, "success");
      await loadFirmwareReleases();
    } else {
      showToast(data.error || "Failed updating active latest firmware.", "danger");
    }
  } catch (err) {
    showToast("Network error updating firmware release.", "danger");
  }
}

async function deleteFirmwareRelease(version) {
  if (!confirm(`Delete firmware release ${version} and its binary image from disk?`)) return;
  try {
    const res = await fetch(`/api/admin/firmware/${encodeURIComponent(version)}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Deleted firmware release ${version}.`, "success");
      await loadFirmwareReleases();
    } else {
      showToast(data.error || "Failed deleting firmware release.", "danger");
    }
  } catch (err) {
    showToast("Network error deleting firmware release.", "danger");
  }
}

async function openPushFirmwareModal(mac) {
  if (!Array.isArray(currentFirmwareReleases) || currentFirmwareReleases.length === 0) {
    await loadFirmwareReleases();
  }
  const dev = (currentDevices || []).find(d => d.mac_address === mac);
  if (!dev) return;

  pushFirmwareTargetMac = mac;
  document.getElementById("pushFirmwareDeviceName").textContent = dev.friendly_name || "ESP32 Touch";
  document.getElementById("pushFirmwareDeviceMac").textContent = mac;
  const curVer = dev.firmware_version || "1.0.0";
  document.getElementById("pushFirmwareCurrentVer").textContent = curVer.startsWith("v") ? curVer : `v${curVer}`;
  document.getElementById("pushFirmwareForce").checked = false;

  const listEl = document.getElementById("pushFirmwareVersionList");
  listEl.innerHTML = "";
  if (currentFirmwareReleases.length === 0) {
    listEl.innerHTML = `<div class="text-secondary" style="padding: 12px; font-size: 12px;">No firmware releases cataloged yet. Upload a .bin first.</div>`;
    document.getElementById("pushFirmwareChangelog").textContent = "—";
    selectedPushFirmwareVersion = null;
  } else {
    const defaultRel = currentFirmwareReleases.find(r => r.is_active_latest) || currentFirmwareReleases[0];
    selectedPushFirmwareVersion = defaultRel.version;

    currentFirmwareReleases.forEach(r => {
      const checked = r.version === selectedPushFirmwareVersion ? "checked" : "";
      const isCur = (r.version === curVer || r.version === `v${curVer}`) ? `<span class="badge badge-subtle">CURRENT</span>` : "";
      const isLat = r.is_active_latest ? `<span class="badge badge-success">LATEST</span>` : "";
      const row = document.createElement("label");
      row.style.cssText = "display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 6px; cursor: pointer;";
      row.innerHTML = `
        <div style="display: flex; align-items: center; gap: 8px;">
          <input type="radio" name="pushFirmwareVersionRadio" value="${escapeHtml(r.version)}" ${checked} onchange="selectPushFirmwareVersion('${escapeHtml(r.version)}')">
          <strong style="font-family: monospace;">${escapeHtml(r.version)}</strong>
          <span class="text-secondary" style="font-size: 12px;">${escapeHtml(r.release_tag || "")}</span>
        </div>
        <div style="display: flex; gap: 6px; align-items: center;">
          ${isCur}${isLat}
          <span class="text-muted" style="font-size: 11px;">${escapeHtml(r.file_size_human || "")}</span>
        </div>
      `;
      listEl.appendChild(row);
    });
    selectPushFirmwareVersion(selectedPushFirmwareVersion);
  }

  document.getElementById("pushFirmwareModal").style.display = "flex";
}

function selectPushFirmwareVersion(version) {
  selectedPushFirmwareVersion = version;
  const rel = currentFirmwareReleases.find(r => r.version === version);
  const logEl = document.getElementById("pushFirmwareChangelog");
  if (!rel || !logEl) return;
  const sha = (rel.sha256_hash || "").slice(0, 16);
  logEl.textContent = `${rel.release_notes || "No release notes provided."}\nSHA-256: ${sha}...`;
}

function closePushFirmwareModal() {
  pushFirmwareTargetMac = null;
  const modal = document.getElementById("pushFirmwareModal");
  if (modal) modal.style.display = "none";
}

async function submitPushFirmware() {
  if (!pushFirmwareTargetMac || !selectedPushFirmwareVersion) {
    showToast("Select a firmware version first.", "danger");
    return;
  }
  const force = document.getElementById("pushFirmwareForce").checked;
  try {
    const res = await fetch(`/api/admin/devices/${encodeURIComponent(pushFirmwareTargetMac)}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: selectedPushFirmwareVersion, force }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`OTA initiated for ${pushFirmwareTargetMac} -> ${selectedPushFirmwareVersion}`, "success");
      closePushFirmwareModal();
      if (typeof loadDevices === "function") await loadDevices();
    } else {
      showToast(data.error || `OTA trigger failed (HTTP ${res.status})`, "danger");
    }
  } catch (err) {
    showToast("Network error dispatching OTA: " + err.message, "danger");
  }
}

async function pushFirmwareUpdateAll() {
  if (!Array.isArray(currentFirmwareReleases) || currentFirmwareReleases.length === 0) {
    await loadFirmwareReleases();
  }
  const latest = currentFirmwareReleases.find(r => r.is_active_latest) || currentFirmwareReleases[0];
  if (!latest) {
    showToast("Upload a firmware release first before pushing fleet updates.", "warning");
    return;
  }
  if (!confirm(`Push firmware ${latest.version} to all approved ESP32 Touch displays on the LAN?`)) {
    return;
  }
  try {
    const res = await fetch("/api/admin/devices/update-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: latest.version, force: false, delay_seconds: 0 }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`OTA update-all (${latest.version}): ${data.dispatched || 0} initiated, ${data.skipped || 0} skipped.`, "success");
      if (typeof loadDevices === "function") await loadDevices();
    } else {
      showToast(data.error || "Batch OTA update failed.", "danger");
    }
  } catch (err) {
    showToast("Network error running batch OTA update: " + err.message, "danger");
  }
}

// ============================================================================
// Touch MicroSD Cache Controls (DLP-020)
// ============================================================================

async function fetchDeviceCacheStatus(mac) {
  const safeMac = mac.replace(/[:]/g, "");
  const slot = document.getElementById(`sd-status-${safeMac}`);
  try {
    const res = await fetch(`/api/admin/devices/${encodeURIComponent(mac)}/cache/status`);
    const data = await res.json();
    if (res.ok && data.ok && data.sd) {
      const sd = data.sd;
      const used = sd.used_mb !== undefined ? `${sd.used_mb} MB` : "?";
      const files = sd.files !== undefined ? `${sd.files} files` : "";
      const orphans = sd.orphans > 0 ? ` (${sd.orphans} orphans)` : "";
      const text = `💾 SD: ${used}${files ? " • " + files : ""}${orphans}`;
      if (slot) {
        slot.innerHTML = `<span class="badge badge-subtle" style="font-size: 10px;">${escapeHtml(text)}</span>`;
      }
      showToast(`${mac}: ${text}`, "success");
    } else {
      showToast(data.error || `Failed fetching SD status for ${mac}`, "warning");
    }
  } catch (err) {
    showToast("Network error checking Touch MicroSD status: " + err.message, "danger");
  }
}

async function pruneDeviceCache(mac) {
  try {
    const res = await fetch(`/api/admin/devices/${encodeURIComponent(mac)}/cache/prune`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      const devResp = data.device_response || {};
      const deleted = devResp.deleted_count !== undefined ? ` (${devResp.deleted_count} orphan file(s) removed)` : "";
      showToast(`MicroSD orphan prune triggered on ${mac}${deleted}.`, "success");
    } else {
      showToast(data.error || `Failed pruning MicroSD cache on ${mac}`, "danger");
    }
  } catch (err) {
    showToast("Network error pruning Touch MicroSD cache: " + err.message, "danger");
  }
}

async function purgeDeviceCache(mac) {
  if (!confirm(`Wipe the entire MicroSD /ythumbs/ artwork cache on Touch controller ${mac}? It will rebuild automatically in the background.`)) {
    return;
  }
  try {
    const res = await fetch(`/api/admin/devices/${encodeURIComponent(mac)}/cache`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`MicroSD cache purged on ${mac}; background rebuild started.`, "success");
    } else {
      showToast(data.error || `Failed purging MicroSD cache on ${mac}`, "danger");
    }
  } catch (err) {
    showToast("Network error purging Touch MicroSD cache: " + err.message, "danger");
  }
}


// ============================================================================
// Upstream Touch GitHub Releases (Notification & On-Demand Download)
// ============================================================================

let currentUpstreamReleases = null;

function formatFileSizeHuman(bytes) {
  const b = Number(bytes) || 0;
  if (b >= 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(2)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

function openUpstreamFirmwareModal() {
  const modal = document.getElementById("upstreamFirmwareModal");
  if (modal) modal.style.display = "flex";
  loadUpstreamTouchReleases(false);
}

function closeUpstreamFirmwareModal() {
  const modal = document.getElementById("upstreamFirmwareModal");
  if (modal) modal.style.display = "none";
}

async function toggleIncludePrereleases(checked) {
  try {
    const res = await fetch("/api/admin/firmware/upstream/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_prereleases: Boolean(checked) }),
    });
    if (res.ok) {
      showToast(checked ? "Pre-releases (RC builds) enabled." : "Stable-only releases enabled.", "info");
      await loadUpstreamTouchReleases(true);
    }
  } catch (err) {
    showToast("Failed to save pre-release preference: " + err.message, "danger");
  }
}

async function loadUpstreamTouchReleases(force = false) {
  try {
    const url = `/api/admin/firmware/upstream${force ? "?force=true" : ""}`;
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    currentUpstreamReleases = data;

    // Update banner & badge notification
    const banner = document.getElementById("touchUpdateNotificationBanner");
    const bannerVer = document.getElementById("touchUpdateBannerVersion");
    const noticeBadge = document.getElementById("upstreamReleaseNoticeBadge");

    if (data.has_update && data.latest_version) {
      if (banner) {
        banner.style.display = "flex";
        if (bannerVer) bannerVer.textContent = data.latest_version;
      }
      if (noticeBadge) {
        noticeBadge.style.display = "inline-block";
        noticeBadge.textContent = data.latest_version;
      }
    } else {
      if (banner) banner.style.display = "none";
      if (noticeBadge) noticeBadge.style.display = "none";
    }

    // Update modal filter checkbox
    const chk = document.getElementById("upstreamIncludePrereleases");
    if (chk && data.include_prereleases !== undefined) {
      chk.checked = Boolean(data.include_prereleases);
    }

    const lastChecked = document.getElementById("upstreamLastChecked");
    if (lastChecked && data.checked_at) {
      const dt = new Date(data.checked_at);
      lastChecked.textContent = `Checked: ${dt.toLocaleTimeString()}`;
    }

    renderUpstreamReleasesList(data);
  } catch (err) {
    console.warn("Failed fetching upstream Touch releases:", err);
  }
}

function renderUpstreamReleasesList(data) {
  const container = document.getElementById("upstreamReleasesList");
  if (!container) return;

  const releases = Array.isArray(data.releases) ? data.releases : [];
  if (releases.length === 0) {
    container.innerHTML = `
      <div style="text-align: center; color: var(--text-muted); padding: 24px; background: var(--bg-surface); border: 1px dashed var(--border-subtle); border-radius: 8px;">
        No releases found matching current criteria.
        ${!data.include_prereleases ? '<br><small class="text-secondary">Try enabling "Include Pre-Releases (RC Builds)" above.</small>' : ''}
      </div>
    `;
    return;
  }

  container.innerHTML = "";
  releases.forEach(r => {
    const isPrerelease = Boolean(r.prerelease);
    const badgeHtml = isPrerelease
      ? `<span class="badge badge-warning" style="font-size: 11px;">RC Pre-Release</span>`
      : `<span class="badge badge-success" style="font-size: 11px;">★ Stable</span>`;

    const pubDate = r.published_at ? new Date(r.published_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
    const sizeStr = r.asset_size ? formatFileSizeHuman(r.asset_size) : "";
    const assetStr = r.asset_name ? `<code>${escapeHtml(r.asset_name)}</code> (${sizeStr})` : `<span class="text-muted">No .bin asset attached</span>`;

    const notesSummary = (r.body || "No changelog provided.").trim();

    let actionBtn = "";
    if (r.is_cataloged) {
      actionBtn = `<button type="button" class="btn btn-secondary btn-sm" disabled style="opacity: 0.75; cursor: default;">✓ In Repository</button>`;
    } else if (r.asset_download_url) {
      actionBtn = `<button type="button" class="btn btn-primary btn-sm" id="btn-dl-${escapeHtml(r.tag_name)}" onclick="downloadUpstreamRelease('${escapeHtml(r.tag_name)}')">⬇️ Download to Core</button>`;
    } else {
      actionBtn = `<button type="button" class="btn btn-secondary btn-sm" disabled style="opacity: 0.5;">No Binary Available</button>`;
    }

    const card = document.createElement("div");
    card.style.cssText = "background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 12px 14px;";
    card.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; flex-wrap: wrap;">
        <div>
          <div style="display: flex; align-items: center; gap: 8px;">
            <strong style="font-size: 14px; font-family: monospace; color: var(--text-primary);">${escapeHtml(r.tag_name)}</strong>
            ${badgeHtml}
            ${pubDate ? `<span class="text-secondary" style="font-size: 11px;">• ${escapeHtml(pubDate)}</span>` : ''}
          </div>
          <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
            Asset: ${assetStr}
          </div>
        </div>
        <div>
          ${actionBtn}
        </div>
      </div>
      <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-subtle); font-size: 12px; color: var(--text-secondary); line-height: 1.5;">${formatTruncatedReleaseNotes(r.body, r.tag_name, 500)}</div>
    `;
    container.appendChild(card);
  });
}

async function downloadUpstreamRelease(tag) {
  const btn = document.getElementById(`btn-dl-${tag}`);
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `⏳ Downloading...`;
  }

  try {
    const res = await fetch("/api/admin/firmware/upstream/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(`Downloaded firmware ${tag} to Core repository! Ready for OTA.`, "success");
      await loadFirmwareReleases();
      await loadUpstreamTouchReleases(true);
    } else {
      showToast(data.error || `Download failed (HTTP ${res.status})`, "danger");
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `⬇️ Download to Core`;
      }
    }
  } catch (err) {
    showToast("Network error downloading release: " + err.message, "danger");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `⬇️ Download to Core`;
    }
  }
}


// ============================================================================
// Markdown Parser & Truncation Helpers for Release Notes (Full GFM + GitHub Alerts)
// ============================================================================

function renderMarkdown(text) {
  if (!text) return "";

  let src = text;

  // 1. Pre-process GitHub Alert Callouts: > [!NOTE], > [!TIP], > [!IMPORTANT], > [!WARNING], > [!CAUTION]
  const multiLineAlertPattern = /(?:^|\n)>[ \t]*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*\n((?:>[ \t]*.*(?:\n|$))*)/gi;
  src = src.replace(multiLineAlertPattern, (match, type, body) => {
    const alertType = type.toUpperCase();
    const lines = body
      .split("\n")
      .map(l => l.replace(/^>[ \t]?/, ""))
      .join("\n")
      .trim();

    let icon = "ℹ️";
    let title = "Note";
    let color = "#3b82f6";
    let bg = "rgba(59, 130, 246, 0.1)";

    if (alertType === "TIP") {
      icon = "💡"; title = "Tip"; color = "#22c55e"; bg = "rgba(34, 197, 94, 0.1)";
    } else if (alertType === "IMPORTANT") {
      icon = "❗"; title = "Important"; color = "#a855f7"; bg = "rgba(168, 85, 247, 0.1)";
    } else if (alertType === "WARNING") {
      icon = "⚠️"; title = "Warning"; color = "#f59e0b"; bg = "rgba(245, 158, 11, 0.1)";
    } else if (alertType === "CAUTION") {
      icon = "🛑"; title = "Caution"; color = "#ef4444"; bg = "rgba(239, 68, 68, 0.1)";
    }

    return `\n<div class="markdown-alert markdown-alert-${alertType.toLowerCase()}" style="margin: 12px 0; padding: 12px 14px; border-left: 4px solid ${color}; background: ${bg}; border-radius: 6px;">\n<div style="display: flex; align-items: center; gap: 6px; font-weight: 700; font-size: 13px; color: ${color}; margin-bottom: 6px;"><span>${icon}</span> <span>${title}</span></div>\n<div style="font-size: 13px; color: var(--text-primary); line-height: 1.5;">\n\n${lines}\n\n</div>\n</div>\n`;
  });

  // Single-line alert fallback: > [!WARNING] Some text
  const singleLineAlertPattern = /(?:^|\n)>[ \t]*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]+([^\n]+)/gi;
  src = src.replace(singleLineAlertPattern, (match, type, singleText) => {
    const alertType = type.toUpperCase();
    let icon = "ℹ️"; let title = "Note"; let color = "#3b82f6"; let bg = "rgba(59, 130, 246, 0.1)";
    if (alertType === "TIP") { icon = "💡"; title = "Tip"; color = "#22c55e"; bg = "rgba(34, 197, 94, 0.1)"; }
    else if (alertType === "IMPORTANT") { icon = "❗"; title = "Important"; color = "#a855f7"; bg = "rgba(168, 85, 247, 0.1)"; }
    else if (alertType === "WARNING") { icon = "⚠️"; title = "Warning"; color = "#f59e0b"; bg = "rgba(245, 158, 11, 0.1)"; }
    else if (alertType === "CAUTION") { icon = "🛑"; title = "Caution"; color = "#ef4444"; bg = "rgba(239, 68, 68, 0.1)"; }

    return `\n<div class="markdown-alert markdown-alert-${alertType.toLowerCase()}" style="margin: 12px 0; padding: 12px 14px; border-left: 4px solid ${color}; background: ${bg}; border-radius: 6px;">\n<div style="display: flex; align-items: center; gap: 6px; font-weight: 700; font-size: 13px; color: ${color}; margin-bottom: 6px;"><span>${icon}</span> <span>${title}</span></div>\n<div style="font-size: 13px; color: var(--text-primary); line-height: 1.5;">\n\n${singleText.trim()}\n\n</div>\n</div>\n`;
  });

  // 2. Render via vendored marked.min.js
  let html = "";
  if (typeof marked !== "undefined" && typeof marked.parse === "function") {
    try {
      html = marked.parse(src, {
        gfm: true,
        breaks: true,
      });
    } catch (err) {
      console.warn("marked.parse error, using fallback rendering", err);
      html = escapeHtml(src).replace(/\n/g, "<br>");
    }
  } else {
    html = escapeHtml(src).replace(/\n/g, "<br>");
  }

  // 3. Ensure all links have target="_blank" and rel="noopener noreferrer"
  html = html.replace(/<a\s+([^>]*?)href="([^"]*)"([^>]*)>/gi, (match, before, href, after) => {
    return `<a ${before}href="${href}" target="_blank" rel="noopener noreferrer" style="color: var(--primary); text-decoration: underline; word-break: break-all;"${after}>`;
  });

  // 4. Autolink bare URLs not inside HTML tags
  html = html.replace(/(^|[^"'>])(https?:\/\/[^\s<"']+)/gi, (match, prefix, url) => {
    return `${prefix}<a href="${url}" target="_blank" rel="noopener noreferrer" style="color: var(--primary); text-decoration: underline; word-break: break-all;">${url}</a>`;
  });

  return html;
}

function formatTruncatedReleaseNotes(notes, version, maxChars = 500) {
  if (!notes || !notes.trim()) {
    return `<span class="text-muted">—</span>`;
  }
  const raw = notes.trim();
  if (raw.length <= maxChars) {
    return `<div class="formatted-release-notes" style="font-size: 12px; line-height: 1.5;">${renderMarkdown(raw)}</div>`;
  }

  // Look for a clean breakpoint at or before maxChars ending on sentence (.!?) or newline
  const sub = raw.slice(0, maxChars);
  let cutIdx = -1;

  // 1. Look for newline in the last 150 chars of the slice
  const lastNl = sub.lastIndexOf("\n");
  if (lastNl > maxChars - 150) {
    cutIdx = lastNl;
  }

  // 2. Look for sentence punctuation in the last 150 chars
  if (cutIdx === -1) {
    const sMatches = [...sub.matchAll(/[.!?](?:\s+|$)/g)];
    if (sMatches.length > 0) {
      const lastM = sMatches[sMatches.length - 1];
      if (lastM.index > maxChars - 150) {
        cutIdx = lastM.index + 1;
      }
    }
  }

  // 3. Fallback: last whitespace
  if (cutIdx === -1) {
    const lastSp = sub.lastIndexOf(" ");
    if (lastSp > maxChars - 100) {
      cutIdx = lastSp;
    } else {
      cutIdx = maxChars;
    }
  }

  const truncated = raw.slice(0, cutIdx).trim();
  const safeVer = escapeHtml(version);

  return `
    <div class="truncated-release-notes" style="font-size: 12px; line-height: 1.5;">
      <div>${renderMarkdown(truncated)}...</div>
      <div style="margin-top: 6px;">
        <button type="button" class="btn-link" onclick="openReleaseNotesModal('${safeVer}')" style="background: none; border: none; padding: 0; color: var(--primary); font-size: 12px; font-weight: 700; text-decoration: underline; cursor: pointer;">
          Read more ↗
        </button>
      </div>
    </div>
  `;
}

function openReleaseNotesModal(version) {
  let notes = "";
  // Check in downloaded currentFirmwareReleases
  const downloadedRel = (currentFirmwareReleases || []).find(r => r.version === version);
  if (downloadedRel && downloadedRel.release_notes) {
    notes = downloadedRel.release_notes;
  } else if (currentUpstreamReleases && Array.isArray(currentUpstreamReleases.releases)) {
    const upstreamRel = currentUpstreamReleases.releases.find(r => r.tag_name === version || r.name === version);
    if (upstreamRel && upstreamRel.body) {
      notes = upstreamRel.body;
    }
  }

  const verEl = document.getElementById("releaseNotesModalVersion");
  const contentEl = document.getElementById("releaseNotesModalContent");
  const modal = document.getElementById("releaseNotesModal");

  if (verEl) verEl.textContent = version;
  if (contentEl) {
    contentEl.innerHTML = renderMarkdown(notes || "No release notes available.");
  }
  if (modal) modal.style.display = "flex";
}

function closeReleaseNotesModal() {
  const modal = document.getElementById("releaseNotesModal");
  if (modal) modal.style.display = "none";
}
