// -----------------------------------------------------------------------------
// TAB 3: Cards Inventory & Physical Card Pairing
// -----------------------------------------------------------------------------

let _pairPollTimer = null;
let _detectedPairCard = null;
let _rePairTargetSlotId = null;
let _cardsTranscodePollTimer = null;

function openPairCardModal(rePairSlotId = null) {
  const modal = document.getElementById("pairCardModal");
  if (!modal) return;
  _detectedPairCard = null;
  _rePairTargetSlotId = rePairSlotId || null;

  const devSel = document.getElementById("pairDeviceSelect");
  if (devSel) {
    devSel.innerHTML = "";
    (currentDevices || []).forEach(d => {
      const opt = document.createElement("option");
      const devId = d.deviceId || d.id;
      opt.value = devId;
      opt.textContent = typeof formatPlayerOptionLabel === "function"
        ? formatPlayerOptionLabel(d)
        : `${d.online ? "🟢" : "🔴"} ${d.name || devId}`;
      if (devId === activeDeviceId) opt.selected = true;
      devSel.appendChild(opt);
    });
  }

  const profSel = document.getElementById("pairProfileSelect");
  if (profSel) {
    profSel.innerHTML = "";
    Object.values(currentProfiles || {}).forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.display_name || p.id;
      if (p.id === activeProfileId) opt.selected = true;
      profSel.appendChild(opt);
    });
  }

  document.getElementById("pairCardOptions").style.display = "none";
  const confirmBtn = document.getElementById("btnConfirmPairCard");
  if (confirmBtn) {
    confirmBtn.style.display = "none";
    confirmBtn.innerText = _rePairTargetSlotId ? "🔄 Update Linked Card" : "🔗 Pair Card";
  }
  document.getElementById("pairDetectStatus").innerHTML = _rePairTargetSlotId
    ? `🔄 <strong>Re-Pairing Slot:</strong> Insert the physical card into the selected Yoto Player to update its linked Card ID.`
    : "📡 Listening... Insert a physical card into the selected Yoto Player.";
  modal.style.display = "flex";

  pollPairCard();
  if (_pairPollTimer) clearInterval(_pairPollTimer);
  _pairPollTimer = setInterval(pollPairCard, 2500);
}

function closePairCardModal() {
  if (_pairPollTimer) { clearInterval(_pairPollTimer); _pairPollTimer = null; }
  const modal = document.getElementById("pairCardModal");
  if (modal) modal.style.display = "none";
}

function onPairTargetTypeChange() {
  const t = document.getElementById("pairTargetType")?.value || "profile";
  document.getElementById("pairProfileGroup").style.display = t === "profile" ? "block" : "none";
  document.getElementById("pairFleetNameGroup").style.display = t === "fleet" ? "block" : "none";
}

async function pollPairCard() {
  const devId = document.getElementById("pairDeviceSelect")?.value;
  const statusEl = document.getElementById("pairDetectStatus");
  const optsEl = document.getElementById("pairCardOptions");
  const btnEl = document.getElementById("btnConfirmPairCard");
  if (!devId || !statusEl) return;

  try {
    const res = await fetch(`/api/cards/detect?device_id=${encodeURIComponent(devId)}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      statusEl.innerHTML = `⚠️ ${data.error || "Unable to read player status."}`;
      return;
    }
    if (!data.detected) {
      _detectedPairCard = null;
      statusEl.style.background = "rgba(59,130,246,0.08)";
      statusEl.style.borderColor = "rgba(59,130,246,0.25)";
      statusEl.innerHTML = `📡 Listening on selected player... Insert a physical card now.${data.online ? "" : " <em>(Player appears offline)</em>"}`;
      if (optsEl) optsEl.style.display = "none";
      if (btnEl) btnEl.style.display = "none";
      return;
    }

    _detectedPairCard = data;
    const linkedNote = data.already_linked
      ? `<div style="margin-top:4px; font-size:11px; color:#93c5fd;">ℹ️ Already linked to slot: <code>${data.already_linked.slot_id}</code> (${data.already_linked.title || ""})</div>`
      : "";

    if (data.is_editable) {
      const heading = data.is_blank_myo
        ? "✨ Blank MYO Card Detected (Make Your Own Guide)"
        : "✅ Editable MYO Card Detected";
      statusEl.style.background = "rgba(16,185,129,0.1)";
      statusEl.style.borderColor = "rgba(16,185,129,0.4)";
      statusEl.innerHTML =
        `<div style="font-weight:700; color:#4ade80;">${heading}</div>` +
        `<div style="margin-top:4px;"><strong>${data.title}</strong> &nbsp;<code>${data.card_id}</code></div>` +
        `<div class="text-secondary" style="font-size:11px; margin-top:2px;">${data.reason}</div>` +
        linkedNote;
      if (optsEl) optsEl.style.display = _rePairTargetSlotId ? "none" : "block";
      if (btnEl) btnEl.style.display = "inline-flex";
      if (!_rePairTargetSlotId) onPairTargetTypeChange();
    } else {
      statusEl.style.background = "rgba(245,158,11,0.1)";
      statusEl.style.borderColor = "rgba(245,158,11,0.45)";
      statusEl.innerHTML =
        `<div style="font-weight:700; color:#fbbf24;">🔒 Locked Card Detected (Retail / Yoto Store)</div>` +
        `<div style="margin-top:4px;"><strong>${data.title}</strong> &nbsp;<code>${data.card_id}</code></div>` +
        `<div class="text-secondary" style="font-size:11px; margin-top:4px;">${data.reason}</div>`;
      if (optsEl) optsEl.style.display = "none";
      if (btnEl) btnEl.style.display = "none";
    }
  } catch {
    statusEl.innerHTML = "⚠️ Network error polling player.";
  }
}

async function confirmPairCard() {
  if (!_detectedPairCard || !_detectedPairCard.is_editable) return;
  const btn = document.getElementById("btnConfirmPairCard");
  if (btn) { btn.disabled = true; btn.innerText = _rePairTargetSlotId ? "Updating..." : "Pairing..."; }

  try {
    let res, data;
    if (_rePairTargetSlotId) {
      res = await fetch(`/api/cards/${encodeURIComponent(_rePairTargetSlotId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yoto_card_id: _detectedPairCard.card_id })
      });
      data = await res.json();
      if (res.ok) {
        closePairCardModal();
        await loadCards();
        showToast(`Re-paired card to '${_detectedPairCard.card_id}'!`, "success");
      } else {
        showToast(data.error || "Failed to re-pair card.", "danger");
      }
      return;
    }

    const target = document.getElementById("pairTargetType")?.value || "profile";
    const profileId = document.getElementById("pairProfileSelect")?.value || "";
    const name = document.getElementById("pairFleetName")?.value.trim() || _detectedPairCard.title || "";
    res = await fetch("/api/cards/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        card_id: _detectedPairCard.card_id,
        target,
        profile_id: profileId,
        name
      })
    });
    data = await res.json();
    if (res.ok && data.ok) {
      closePairCardModal();
      await loadCards();
      showToast(`Paired card '${_detectedPairCard.card_id}' successfully!`, "success");
    } else {
      showToast(data.error || "Failed to pair card.", "danger");
    }
  } catch {
    showToast("Network error pairing card.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = _rePairTargetSlotId ? "🔄 Update Linked Card" : "🔗 Pair Card"; }
  }
}

function startRePairFromEditModal() {
  const slotId = document.getElementById("editCardSlotId")?.value || "";
  if (!slotId) return;
  closeEditCardSlotModal();
  openPairCardModal(slotId);
}

async function loadCards() {
  try {
    const res = await fetch("/api/cards", { cache: "no-store" });
    if (!res.ok) return;
    cardSlots = await res.json();
    renderCardsList();
  } catch (err) {
    console.error("Cards load error:", err);
  }
}

async function refreshCardsTranscodeProgress() {
  // Do not wipe & rebuild the DOM if the user has a <select> dropdown focused inside #tabCards
  const activeEl = document.activeElement;
  if (activeEl && activeEl.tagName === "SELECT" && activeEl.closest("#tabCards")) {
    return;
  }
  await loadCards();
}

function renderCardsList() {
  const profContainer = document.getElementById("profileCardsList");
  const fleetContainer = document.getElementById("fleetCardsList");
  profContainer.innerHTML = "";
  fleetContainer.innerHTML = "";

  const hiddenKinds = new Set(["profile_cache", "ephemeral_offline", "guest_stream"]);
  const visibleSlots = (cardSlots || []).filter(s => !hiddenKinds.has(s.kind) && (!s.slot_id || !s.slot_id.startsWith("cache_")));
  const isPersonalSlot = (s) => (
    s.kind === "profile_primary"
    || s.kind === "user_primary"
    || s.kind === "user_session"
    || Boolean(s.owner_user_id)
    || (s.slot_id && (s.slot_id.startsWith("profile_") || s.slot_id.startsWith("user_")))
  );
  const profileSlots = visibleSlots.filter(isPersonalSlot);
  const fleetSlots = visibleSlots.filter(s => !isPersonalSlot(s));

  if (profileSlots.length === 0) {
    profContainer.innerHTML = "<p class='text-secondary' style='font-size: 12px;'>No profile cards linked yet. Play a book from the Library to pair automatically.</p>";
  } else {
    profileSlots.forEach(s => profContainer.appendChild(createCardItemElement(s, true)));
  }

  if (fleetSlots.length === 0) {
    fleetContainer.innerHTML = "<p class='text-secondary' style='font-size: 12px;'>No fleet cards added yet. Click '🔗 Pair Physical Card' above.</p>";
  } else {
    fleetSlots.forEach(s => fleetContainer.appendChild(createCardItemElement(s, false)));
  }
}

function createCardItemElement(slot, isProfile) {
  const el = document.createElement("div");
  el.className = "list-item-card";

  const offlineSubmode = (slot.offline_mode || "book").toLowerCase();
  const offlineLabel = offlineSubmode === "next"
    ? `⏱️ Offline (${slot.offline_hours || 5}h Window)`
    : (offlineSubmode === "series" ? "📚 Offline (Series)" : "💾 Offline (Book)");
  const modeBadge = slot.mode === "offline"
    ? `<span class="badge badge-warning" style="font-size: 10px;">${escapeHtml(offlineLabel)}</span>`
    : (slot.mode === "retail"
      ? '<span class="badge badge-success" style="font-size: 10px;">💿 Official Yoto</span>'
      : '<span class="badge badge-info" style="font-size: 10px;">⚡ Streaming</span>');

  const transcodeJob = slot.transcode;
  let progressHtml = "";
  if (transcodeJob && transcodeJob.status === "failed") {
    const errText = escapeHtml(transcodeJob.error || "Unknown offline build error");
    progressHtml = `
      <div style="margin-top: 8px; padding: 8px 10px; border-radius: 6px; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.35);">
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 4px;">
          <span style="font-size: 11px; font-weight: 600; color: #fca5a5;">❌ Offline build failed (${transcodeJob.done || 0}/${transcodeJob.total || 0} tracks)</span>
          <button class="btn btn-secondary btn-sm" style="font-size: 10px; padding: 2px 8px; flex-shrink: 0;" onclick="retryCardSync('${escapeHtml(slot.slot_id)}')">🔄 Retry</button>
        </div>
        <div style="font-size: 11px; color: #fecaca; word-break: break-word;">${errText}</div>
      </div>
    `;
  } else if (transcodeJob && transcodeJob.status !== "ready") {
    const pct = Math.round((transcodeJob.progress || 0) * 100);
    const curLabel = transcodeJob.current_title
      ? ` — Track ${transcodeJob.current_track || (transcodeJob.done + 1)}: ${escapeHtml(transcodeJob.current_title)}`
      : "";
    progressHtml = `
      <div style="margin-top: 6px;">
        <div style="display: flex; justify-content: space-between; font-size: 10px; color: var(--accent-abs); margin-bottom: 2px; gap: 6px;">
          <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">⏳ ${escapeHtml(transcodeJob.status)} (${transcodeJob.done}/${transcodeJob.total} tracks)${curLabel}</span>
          <span style="flex-shrink: 0;">${pct}%</span>
        </div>
        <div style="height: 4px; background: rgba(0,0,0,0.3); border-radius: 2px; overflow: hidden;">
          <div style="width: ${pct}%; height: 100%; background: var(--accent-abs); transition: width 0.3s ease;"></div>
        </div>
      </div>
    `;
  }

  const curSpeed = parseFloat(slot.speed || 1.0);
  const curDeliveryKey = slot.mode === "offline"
    ? (offlineSubmode === "next" ? "offline_next" : (offlineSubmode === "series" ? "offline_series" : "offline_book"))
    : "streaming";

  const cardNickname = (slot.nickname || "").trim() || slot.slot_id;
  const lastContent = (slot.title || "").trim() || "No content loaded yet";
  const updatedStr = slot.updated_at ? String(slot.updated_at).slice(0, 16).replace("T", " ") : "";

  el.innerHTML = `
    <div class="list-item-thumb">
      ${slot.abs_item_id ? `<img src="/api/cover/${escapeHtml(slot.abs_item_id)}" alt="Cover" onerror="this.onerror=null;this.parentElement.innerText='💳'">` : '💳'}
    </div>
    <div class="list-item-body">
      <div class="list-item-title">${escapeHtml(cardNickname)}</div>
      <div class="list-item-desc">📖 ${escapeHtml(lastContent)}</div>
      ${updatedStr ? `<div class="text-secondary" style="font-size: 10px; margin-top: 2px;">🕒 Updated ${escapeHtml(updatedStr)}</div>` : ''}
      <div class="list-item-tags">
        ${modeBadge}
        ${slot.group_kind === "series" && slot.series_name ? `<span class="badge badge-info" style="font-size: 10px;">📚 ${escapeHtml(slot.series_name)}</span>` : ''}
        <span class="badge badge-subtle" style="font-size: 10px; font-family: monospace;" title="StoryDeck Unique Card ID — mark on physical card to identify later">ID: ${escapeHtml(slot.slot_id)}</span>
        ${slot.is_pinned ? '<span class="badge badge-success" style="font-size: 10px;">📌 Pinned</span>' : ''}
        ${slot.abs_item_id ? (slot.own_place ? '<span class="badge badge-info" style="font-size: 10px;">📍 Own Place</span>' : '<span class="badge badge-subtle" style="font-size: 10px;">🔗 ABS Synced</span>') : ''}
      </div>
      <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center;">
        <select class="form-control" style="width: 68px; font-size: 10px; padding: 2px 4px; height: 24px;" onchange="changeCardSpeed('${escapeHtml(slot.slot_id)}', this.value)" title="Slot Playback Speed">
          <option value="0.75" ${curSpeed === 0.75 ? 'selected' : ''}>0.75x</option>
          <option value="1" ${curSpeed === 1.0 ? 'selected' : ''}>1.0x</option>
          <option value="1.25" ${curSpeed === 1.25 ? 'selected' : ''}>1.25x</option>
          <option value="1.5" ${curSpeed === 1.5 ? 'selected' : ''}>1.5x</option>
          <option value="2" ${curSpeed === 2.0 ? 'selected' : ''}>2.0x</option>
        </select>
        ${slot.abs_item_id ? `<button class="btn btn-secondary btn-sm" style="font-size: 10px; padding: 2px 6px;" onclick="toggleCardOwnPlace('${escapeHtml(slot.slot_id)}', ${!slot.own_place})" title="Toggle Own-Place Progress Isolation">${slot.own_place ? "🔗 Sync ABS" : "📍 Own Place"}</button>` : ''}
        ${slot.abs_item_id ? `
          <select class="form-control" style="width: 132px; font-size: 10px; padding: 2px 4px; height: 24px;" onchange="switchCardDeliveryMode('${escapeHtml(slot.slot_id)}', this.value)" title="Delivery Mode">
            <option value="streaming" ${curDeliveryKey === 'streaming' ? 'selected' : ''}>⚡ Stream</option>
            <option value="offline_next" ${curDeliveryKey === 'offline_next' ? 'selected' : ''}>⏱️ 5h Offline Window</option>
            <option value="offline_book" ${curDeliveryKey === 'offline_book' ? 'selected' : ''}>💾 Whole Book Offline</option>
            <option value="offline_series" ${curDeliveryKey === 'offline_series' ? 'selected' : ''}>📚 Series Offline</option>
          </select>
        ` : ''}
      </div>
      ${progressHtml}
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-end;">
      <button class="btn btn-secondary btn-sm" style="font-size: 11px; padding: 3px 8px;" onclick="openEditCardSlotModal('${escapeHtml(slot.slot_id)}')">✏️ Edit</button>
      <button class="btn btn-secondary btn-sm" style="font-size: 11px; padding: 3px 8px;" onclick="toggleCardPin('${escapeHtml(slot.slot_id)}', ${!slot.is_pinned})">${slot.is_pinned ? "Unpin" : "📌 Pin"}</button>
      ${!isProfile ? `<button class="btn btn-danger btn-sm" style="font-size: 11px; padding: 3px 8px;" onclick="deleteCardSlot('${escapeHtml(slot.slot_id)}')">Delete</button>` : ''}
    </div>
  `;
  return el;
}

function openEditCardSlotModal(slotId) {
  const slot = (cardSlots || []).find(s => s.slot_id === slotId);
  if (!slot) return;
  const modal = document.getElementById("editCardSlotModal");
  if (!modal) return;
  document.getElementById("editCardSlotId").value = slot.slot_id;
  document.getElementById("editCardCustomId").value = slot.slot_id;
  document.getElementById("editCardNickname").value = (slot.nickname || "").trim() || slot.slot_id;
  const isPersonal = slot.kind === "profile_primary" || Boolean(slot.owner_profile_id);
  document.getElementById("editCardKind").value = isPersonal ? "profile_primary" : "permanent_myo";

  const profSel = document.getElementById("editCardOwnerProfile");
  if (profSel) {
    profSel.innerHTML = "";
    Object.values(currentProfiles || {}).forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.display_name || p.id;
      if (p.id === slot.owner_profile_id) opt.selected = true;
      profSel.appendChild(opt);
    });
  }
  const packSel = document.getElementById("editCardSeriesPacking");
  if (packSel) {
    const sp = String(slot.series_packing || "").toLowerCase();
    packSel.value = (sp === "chapters" || sp === "condense") ? sp : "";
  }
  onEditCardKindChange();
  modal.style.display = "flex";
}

function closeEditCardSlotModal() {
  const modal = document.getElementById("editCardSlotModal");
  if (modal) modal.style.display = "none";
}

function onEditCardKindChange() {
  const kind = document.getElementById("editCardKind")?.value || "permanent_myo";
  const grp = document.getElementById("editCardProfileGroup");
  if (grp) grp.style.display = kind === "profile_primary" ? "block" : "none";
}

async function saveEditedCardSlot() {
  const slotId = document.getElementById("editCardSlotId")?.value || "";
  const newSlotId = document.getElementById("editCardCustomId")?.value.trim() || slotId;
  const nickname = document.getElementById("editCardNickname")?.value.trim() || "";
  const kind = document.getElementById("editCardKind")?.value || "permanent_myo";
  const ownerProfileId = kind === "profile_primary" ? (document.getElementById("editCardOwnerProfile")?.value || "") : "";
  const seriesPacking = document.getElementById("editCardSeriesPacking")?.value || "";
  if (!slotId || !nickname || !newSlotId) return;
  const btn = document.getElementById("btnSaveEditCardSlot");
  if (btn) { btn.disabled = true; btn.innerText = "Saving..."; }
  try {
    const res = await fetch(`/api/cards/${encodeURIComponent(slotId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        new_slot_id: newSlotId,
        nickname,
        kind,
        owner_profile_id: ownerProfileId,
        series_packing: seriesPacking
      })
    });
    if (res.ok) {
      closeEditCardSlotModal();
      await loadCards();
      showToast("Card updated.", "success");
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || "Failed to update card.", "danger");
    }
  } catch {
    showToast("Network error updating card.", "danger");
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = "💾 Save Card"; }
  }
}

async function changeCardSpeed(slotId, speedVal) {
  try {
    await fetch(`/api/cards/${encodeURIComponent(slotId)}/speed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speed: parseFloat(speedVal) })
    });
    loadCards();
  } catch (err) {
    console.error("Card speed error:", err);
  }
}

async function toggleCardOwnPlace(slotId, ownPlaceState) {
  try {
    await fetch(`/api/cards/${encodeURIComponent(slotId)}/own-place`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ own_place: ownPlaceState })
    });
    loadCards();
  } catch (err) {
    console.error("Own-place toggle error:", err);
  }
}

async function switchCardDeliveryMode(slotId, mode) {
  try {
    const res = await fetch(`/api/cards/${encodeURIComponent(slotId)}/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode })
    });
    if (res.ok) {
      loadCards();
    } else {
      const errData = await res.json();
      alert(errData.error || "Failed to switch card delivery mode.");
    }
  } catch (err) {
    console.error("Mode switch error:", err);
  }
}

async function retryCardSync(slotId) {
  try {
    const res = await fetch(`/api/cards/${encodeURIComponent(slotId)}/retry`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      showToast("Retrying card build...", "info");
      loadCards();
    } else {
      showToast(data.error || "Failed to retry card build.", "danger");
    }
  } catch (err) {
    console.error("Card retry error:", err);
    showToast("Network error retrying card build.", "danger");
  }
}

async function loadHouseholdYotoCards() {
  try {
    const res = await fetch("/api/library/yoto");
    if (!res.ok) return;
    householdYotoCards = await res.json();
    const sel = document.getElementById("newSlotYotoCardSelect");
    if (sel) {
      sel.innerHTML = `<option value="">-- Leave Empty / Assign Audiobook Later --</option>`;
      householdYotoCards.forEach(c => {
        const opt = document.createElement("option");
        opt.value = c.yoto_card_id || c.id;
        opt.innerText = `💿 ${c.title}${c.author ? " (" + c.author + ")" : ""}`;
        sel.appendChild(opt);
      });
    }
  } catch (err) {
    console.error("Failed to load household Yoto cards:", err);
  }
}

async function toggleCardPin(slotId, pinState) {
  try {
    await fetch(`/api/cards/${slotId}/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_pinned: pinState })
    });
    loadCards();
  } catch (err) {
    console.error("Pin error:", err);
  }
}

async function deleteCardSlot(slotId) {
  if (!confirm(`Delete card slot '${slotId}'?`)) return;
  try {
    await fetch(`/api/cards/${slotId}`, { method: "DELETE" });
    loadCards();
  } catch (err) {
    console.error("Delete slot error:", err);
  }
}

function openAddCardModal() {
  document.getElementById("newSlotId").value = "";
  document.getElementById("newSlotTitle").value = "";
  const sel = document.getElementById("newSlotYotoCardSelect");
  if (sel) sel.value = "";
  loadHouseholdYotoCards();
  document.getElementById("addCardModal").style.display = "flex";
}

function closeAddCardModal() {
  document.getElementById("addCardModal").style.display = "none";
}

async function executeCreateCardSlot() {
  const slotId = document.getElementById("newSlotId").value.trim().toLowerCase().replace(/[^a-z0-9_]/g, "_");
  const title = document.getElementById("newSlotTitle").value.trim();
  const yotoCardId = document.getElementById("newSlotYotoCardSelect")?.value || "";
  if (!slotId) return alert("Slot ID is required");

  try {
    const res = await fetch("/api/cards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot_id: slotId, title: title || slotId, kind: "permanent_myo" })
    });
    if (res.ok) {
      if (yotoCardId) {
        const matched = householdYotoCards.find(c => (c.yoto_card_id || c.id) === yotoCardId);
        await fetch("/api/cards/assign", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slot_id: slotId,
            yoto_card_id: yotoCardId,
            title: title || (matched ? matched.title : slotId),
            author: matched ? matched.author : "Official Yoto Card",
            mode: "retail"
          })
        });
      }
      closeAddCardModal();
      loadCards();
    } else {
      alert("Failed to create card slot");
    }
  } catch (err) {
    console.error("Create card error:", err);
  }
}

