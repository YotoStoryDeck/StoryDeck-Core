// -----------------------------------------------------------------------------
// Portal Card Modals: Pair Physical Card, Edit Card Slot & Add Card Slot
// -----------------------------------------------------------------------------

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

