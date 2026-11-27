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
