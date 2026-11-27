// -----------------------------------------------------------------------------
// TAB 2: Library Operations
// -----------------------------------------------------------------------------

async function loadLibrary() {
  if (!activeProfileId) return;
  try {
    const res = await fetch(`/api/library/profile/${activeProfileId}`);
    if (!res.ok) return;
    libraryItems = await res.json();
    populateSeriesFilter();
    renderLibraryGrid(libraryItems);
  } catch (err) {
    console.error("Library load error:", err);
  }
}

function populateSeriesFilter() {
  const seriesSet = new Set();
  libraryItems.forEach(item => {
    if (item.series) seriesSet.add(item.series);
    if (item.series_list) {
      item.series_list.forEach(s => { if (s.name) seriesSet.add(s.name); });
    }
  });

  const select = document.getElementById("librarySeriesSelect");
  select.innerHTML = "<option value=''>All Series</option>";
  Array.from(seriesSet).sort().forEach(s => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.innerText = s;
    select.appendChild(opt);
  });
}

function filterLibraryGrid() {
  const q = document.getElementById("librarySearchInput").value.toLowerCase().trim();
  const sourceFilter = document.getElementById("librarySourceSelect")?.value || "";
  const seriesFilter = document.getElementById("librarySeriesSelect").value;

  const filtered = libraryItems.filter(item => {
    const title = (item.title || "").toLowerCase();
    const author = (item.author || "").toLowerCase();
    const matchesQ = !q || title.includes(q) || author.includes(q);

    let matchesSource = true;
    if (sourceFilter === "abs") {
      matchesSource = item.type === "abs";
    } else if (sourceFilter === "yoto") {
      matchesSource = item.type !== "abs";
    }

    let matchesSeries = true;
    if (seriesFilter) {
      const inPrimary = (item.series || "") === seriesFilter;
      const inList = item.series_list && item.series_list.some(s => s.name === seriesFilter);
      matchesSeries = inPrimary || inList;
    }
    return matchesQ && matchesSource && matchesSeries;
  });

  renderLibraryGrid(filtered);
}

function renderLibraryGrid(items) {
  const grid = document.getElementById("libraryGrid");
  grid.innerHTML = "";

  if (items.length === 0) {
    grid.innerHTML = "<p class='text-secondary' style='grid-column: 1/-1; text-align: center; padding: 30px;'>No items match your search.</p>";
    return;
  }

  items.forEach(item => {
    const card = document.createElement("div");
    card.className = "book-card";
    card.onclick = () => openBookModal(item);

    const thumbUrl = item.thumb || "/static/img/placeholder.png";
    const seriesText = item.series ? (item.sequence_display ? `${item.series} #${item.sequence_display}` : item.series) : "";
    const isYoto = item.type && item.type !== "abs";
    const sourceBadge = isYoto
      ? `<span class="badge badge-subtle" style="font-size: 9px;">${item.is_myo ? "💳 MYO" : "💿 Yoto"}</span>`
      : `<span class="badge badge-subtle" style="font-size: 9px;">🎧 ABS</span>`;

    card.innerHTML = `
      <div class="book-thumb">
        <img src="${escapeHtml(thumbUrl)}" alt="${escapeHtml(item.title)}" loading="lazy">
      </div>
      <div class="book-info">
        <div class="book-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
        <div class="book-author">${escapeHtml(item.author || (isYoto ? "Yoto" : "Unknown"))}</div>
        <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; align-items: center;">
          ${sourceBadge}
          ${seriesText ? `<span class="badge badge-info" style="font-size: 9px;">${escapeHtml(seriesText)}</span>` : ""}
        </div>
      </div>
    `;
    grid.appendChild(card);
  });
}

async function openBookModal(book) {
  activeBook = book;
  const isYoto = Boolean(book.type && book.type !== "abs");
  const modal = document.getElementById("bookModal");
  document.getElementById("bookModalThumb").src = book.thumb || "/static/img/placeholder.png";
  document.getElementById("bookModalBookTitle").innerText = book.title;
  document.getElementById("bookModalAuthor").innerText = book.author || (isYoto ? "Yoto" : "Audiobookshelf");

  const seriesEl = document.getElementById("bookModalSeries");
  if (book.series) {
    seriesEl.innerText = book.sequence_display ? `${book.series} #${book.sequence_display}` : book.series;
    seriesEl.style.display = "inline-block";
  } else {
    seriesEl.style.display = "none";
  }

  // Populate Profile Default indicator in delivery mode select (hide delivery mode for native Yoto cards)
  const deliveryGroup = document.getElementById("bookModalDeliveryGroup");
  if (deliveryGroup) {
    deliveryGroup.style.display = isYoto ? "none" : "block";
  }
  const activeProf = currentProfiles[activeProfileId];
  const defMode = (activeProf && activeProf.default_playback_mode) ? activeProf.default_playback_mode : "streaming";
  document.getElementById("optProfileDefaultMode").innerText = defMode === "offline" ? "Offline (Cache)" : "Streaming (Instant)";
  document.getElementById("bookModalModeSelect").value = "default";

  const followSeriesWrap = document.getElementById("bookModalFollowSeriesWrap");
  const followSeriesChk = document.getElementById("bookModalFollowSeries");
  const followSeriesLbl = document.getElementById("bookModalFollowSeriesLabel");
  const seriesStartSel = document.getElementById("bookModalSeriesStart");
  if (seriesStartSel) seriesStartSel.value = "selected";
  if (followSeriesWrap && followSeriesChk && followSeriesLbl) {
    if (!isYoto && book.series) {
      followSeriesWrap.style.display = "block";
      followSeriesChk.checked = false;
      followSeriesLbl.innerText = `📚 Follow '${book.series}' series in order on this card (auto-advances across books)`;
    } else {
      followSeriesWrap.style.display = "none";
      followSeriesChk.checked = false;
    }
  }
  onBookModalSeriesToggle();

  // Show Adult User ABS Override toggle when user is signed in with a linked ABS account or in All Books view (ABS items only)
  const useAbsWrap = document.getElementById("bookModalUseMyAbsWrap");
  const useAbsChk = document.getElementById("bookModalUseMyAbs");
  const useAbsLbl = document.getElementById("bookModalUseMyAbsLabel");
  const u = window.currentPortalUser || null;
  const userHasLinkedAbs = Boolean(u && !u.is_lan_guest && (u.has_abs_api_key || u.abs_username || u.abs_user_id));
  const canUsePersonalAbs = !isYoto && Boolean(u && !u.is_lan_guest && (userHasLinkedAbs || activeProfileId === "__all__"));
  if (useAbsWrap && useAbsChk && useAbsLbl) {
    if (canUsePersonalAbs) {
      useAbsWrap.style.display = "block";
      useAbsChk.checked = Boolean(userHasLinkedAbs || activeProfileId === "__all__");
      useAbsLbl.innerText = userHasLinkedAbs && u.abs_username
        ? `🎧 Play using my ABS account (${u.abs_username}) — overrides Target Player profile`
        : "🎧 Play using my StoryDeck session — overrides Target Player profile (link ABS key in Profile)";
    } else {
      useAbsWrap.style.display = "none";
      useAbsChk.checked = false;
    }
  }

  // Update Play Now button text with the currently targeted Yoto Player's name
  updateBookModalTargetPlayerButton();

  // Reset and fetch book details (synopsis, chapters, and suggested resume chapter)
  const synContainer = document.getElementById("bookModalSynopsisContainer");
  const synText = document.getElementById("bookModalSynopsis");
  const durBadge = document.getElementById("bookModalDurationBadge");
  const chapSelect = document.getElementById("bookModalChapterSelect");
  const progHint = document.getElementById("bookModalProgressHint");
  if (synContainer) synContainer.style.display = "none";
  if (durBadge) durBadge.style.display = "none";
  if (progHint) progHint.style.display = "none";
  if (chapSelect) chapSelect.innerHTML = `<option value="1">Chapter 1</option>`;

  modal.style.display = "flex";

  await refreshBookModalDetailsAndSuggestedTrack();
}

function updateBookModalTargetPlayerButton() {
  const playBtn = document.getElementById("btnBookModalPlayNow");
  if (!playBtn) return;
  const dev = (currentDevices || []).find(d => (d.deviceId || d.id) === activeDeviceId);
  const playerName = dev && (dev.name || dev.deviceId || dev.id) ? (dev.name || dev.deviceId || dev.id) : "";
  playBtn.innerText = playerName ? `▶️ Play Now on ${playerName}` : "▶️ Play Now on Selected Player";
}

function onBookModalSeriesToggle() {
  const startWrap = document.getElementById("bookModalSeriesStartWrap");
  const startSel = document.getElementById("bookModalSeriesStart");
  if (!startWrap) return;
  const isYoto = Boolean(activeBook && activeBook.type && activeBook.type !== "abs");
  const modeVal = document.getElementById("bookModalModeSelect")?.value || "default";
  const followChk = Boolean(document.getElementById("bookModalFollowSeries")?.checked);
  const showStart = !isYoto && Boolean(activeBook && activeBook.series) && (followChk || modeVal === "offline_series");
  startWrap.style.display = showStart ? "block" : "none";
  if (startSel && startSel.options && startSel.options[0]) {
    const titleLabel = activeBook && activeBook.title ? `"${activeBook.title}"` : "this book";
    startSel.options[0].textContent = `📖 Start from this book (${titleLabel})`;
  }
}

function onBookModalUseMyAbsToggle() {
  refreshBookModalDetailsAndSuggestedTrack();
}

async function refreshBookModalDetailsAndSuggestedTrack() {
  if (!activeBook) return;
  const isYoto = Boolean(activeBook.type && activeBook.type !== "abs");
  const synContainer = document.getElementById("bookModalSynopsisContainer");
  const synText = document.getElementById("bookModalSynopsis");
  const durBadge = document.getElementById("bookModalDurationBadge");
  const chapSelect = document.getElementById("bookModalChapterSelect");
  const progHint = document.getElementById("bookModalProgressHint");
  const useUserAbs = !isYoto && (Boolean(document.getElementById("bookModalUseMyAbs")?.checked) || (activeProfileId === "__all__"));
  const qs = new URLSearchParams({
    profile_id: activeProfileId || "",
    use_user_abs: useUserAbs ? "1" : "0",
  });

  try {
    const res = await fetch(`/api/library/book/${encodeURIComponent(activeBook.id)}/details?${qs.toString()}`);
    if (res.ok) {
      const details = await res.json();
      if (details.description && synContainer && synText) {
        synText.innerText = details.description.replace(/<[^>]*>/g, "");
        synContainer.style.display = "block";
      }
      if (details.total_duration && durBadge) {
        const hrs = Math.floor(details.total_duration / 3600);
        const mins = Math.round((details.total_duration % 3600) / 60);
        durBadge.innerText = hrs > 0 ? `⏱️ ${hrs}h ${mins}m` : `⏱️ ${mins}m`;
        durBadge.style.display = "inline-block";
      }
      const suggestedCh = parseInt(details.suggested_chapter || 1, 10) || 1;
      const hasProg = Boolean(details.has_progress);
      if (Array.isArray(details.tracks) && details.tracks.length > 0 && chapSelect) {
        chapSelect.innerHTML = "";
        details.tracks.forEach((tr, idx) => {
          const chNum = parseInt(tr.index || tr.number || (idx + 1), 10) || (idx + 1);
          const durStr = tr.duration ? ` (${formatSeconds(tr.duration)})` : "";
          const isSuggested = hasProg && chNum === suggestedCh;
          const tagStr = isSuggested ? " — ★ Last Played / Resume" : ( !hasProg && chNum === 1 && !isYoto ? " — ★ Start Here (New)" : "" );
          const opt = document.createElement("option");
          opt.value = String(chNum);
          opt.innerText = `${chNum}. ${tr.title || "Chapter " + chNum}${durStr}${tagStr}`;
          if (chNum === suggestedCh) {
            opt.selected = true;
          }
          chapSelect.appendChild(opt);
        });
        chapSelect.value = String(suggestedCh);
      }
      if (progHint && !isYoto) {
        const ownerTag = details.progress_owner ? ` (${details.progress_owner})` : "";
        if (hasProg) {
          progHint.className = "badge badge-warning";
          progHint.innerText = `🔖 Resume Track ${suggestedCh} @ ${formatSeconds(details.current_time || 0)}${ownerTag}`;
          progHint.style.display = "inline-block";
        } else if (details.is_finished) {
          progHint.className = "badge badge-success";
          progHint.innerText = `✅ Finished — Starting at Track 1${ownerTag}`;
          progHint.style.display = "inline-block";
        } else {
          progHint.className = "badge badge-info";
          progHint.innerText = `✨ New${ownerTag} — Starting at Track 1`;
          progHint.style.display = "inline-block";
        }
      }
    }
  } catch (err) {
    console.error("Book details fetch error:", err);
  }
}

function closeBookModal() {
  document.getElementById("bookModal").style.display = "none";
}

async function executeBookPlay() {
  if (!activeBook) return;
  const isYoto = Boolean(activeBook.type && activeBook.type !== "abs");
  const modeVal = document.getElementById("bookModalModeSelect").value;
  const chapVal = parseInt(document.getElementById("bookModalChapterSelect")?.value || "1", 10) || 1;
    const u = window.currentPortalUser || null;
    const isLanGuest = Boolean(!u || u.is_lan_guest || !u.id);
    const userHasLinkedAbs = Boolean(u && !u.is_lan_guest && (u.has_abs_api_key || u.abs_username || u.abs_user_id));
    const useUserAbs = !isYoto && (Boolean(document.getElementById("bookModalUseMyAbs")?.checked) || (activeProfileId === "__all__") || isLanGuest);
    const targetProfId = (activeProfileId && activeProfileId !== "__all__") ? activeProfileId : Object.keys(currentProfiles)[0];

  try {
    // Direct Yoto card / MYO playlist playback on Target Player
    if (isYoto) {
      if (!activeDeviceId) {
        showToast("Select a Target Yoto Player first.", "warning");
        return;
      }
      const resPlay = await fetch("/api/player/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          device_id: activeDeviceId,
          card_id: activeBook.id,
          chapter: chapVal,
          profile_id: targetProfId || ""
        })
      });
      if (!resPlay.ok) {
        const errData = await resPlay.json().catch(() => ({}));
        showToast(errData.error || "Failed to start Yoto card playback.", "danger");
        return;
      }
      closeBookModal();
      switchTab("player");
      if (typeof setPendingPlayConfirmation === "function") {
        setPendingPlayConfirmation({
          deviceId: activeDeviceId,
          cardId: activeBook.id,
          title: activeBook.title,
          author: activeBook.author,
          thumb: activeBook.thumb,
        });
      }
      setTimeout(pollPlayerStatus, 600);
      return;
    }

    if (!useUserAbs && !targetProfId) return;

    // 1. Assign ABS book to personal session slot (if overriding ABS) or profile's primary card
    const followSeries = Boolean(document.getElementById("bookModalFollowSeries")?.checked) || modeVal === "offline_series";
    const groupKind = followSeries ? "series" : "book";
    const seriesName = activeBook.series || "";
    const seriesStart = followSeries ? (document.getElementById("bookModalSeriesStart")?.value || "selected") : "selected";
    const assignPayload = useUserAbs
      ? {
          use_user_abs: true,
          abs_item_id: activeBook.id,
          mode: modeVal.startsWith("offline") ? modeVal : "streaming",
          group_kind: groupKind,
          series_name: seriesName,
          series_start: seriesStart,
          title: activeBook.title,
          author: activeBook.author
        }
      : {
          profile_id: targetProfId,
          abs_item_id: activeBook.id,
          mode: modeVal,
          group_kind: groupKind,
          series_name: seriesName,
          series_start: seriesStart,
          title: activeBook.title,
          author: activeBook.author
        };

    const res = await fetch("/api/cards/assign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(assignPayload)
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      alert(errData.error || "Failed to assign book to player card.");
      return;
    }
    const slot = await res.json();
    closeBookModal();

    // 2. Begin playback on active Target Player
    const switchedBook = Boolean(slot.abs_item_id && slot.abs_item_id !== activeBook.id);
    const playTitle = switchedBook ? (slot.title || activeBook.title) : activeBook.title;
    const playThumb = switchedBook
      ? `/api/cover/${encodeURIComponent(slot.abs_item_id)}`
      : (activeBook.thumb || `/api/cover/${encodeURIComponent(activeBook.id)}`);
    const playChapter = switchedBook ? 1 : chapVal;
    if (activeDeviceId && slot.yoto_card_id) {
      if (typeof setPendingPlayConfirmation === "function") {
        setPendingPlayConfirmation({
          deviceId: activeDeviceId,
          cardId: slot.yoto_card_id,
          title: playTitle,
          author: activeBook.author,
          thumb: playThumb,
        });
      }
      await fetch("/api/player/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          device_id: activeDeviceId,
          card_id: slot.yoto_card_id,
          chapter: playChapter,
          use_user_abs: useUserAbs
        })
      });
    }

    switchTab("player");
    setTimeout(pollPlayerStatus, 600);
  } catch (err) {
    console.error("Play error:", err);
  }
}

function openLinkToCardModal() {
  const isYoto = Boolean(activeBook && activeBook.type && activeBook.type !== "abs");
  closeBookModal();
  const select = document.getElementById("targetSlotSelect");
  select.innerHTML = "";

  // Add personal user card and profile card option(s) for ABS items — never send synthetic profile:__all__
  if (!isYoto) {
    const u = window.currentPortalUser || null;
    if (u && !u.is_lan_guest && u.id) {
      const userOpt = document.createElement("option");
      userOpt.value = `user:${u.id}`;
      userOpt.innerText = `🎧 My Personal Card (${u.display_name || u.username || u.id})`;
      if (activeProfileId === "__all__" || document.getElementById("bookModalUseMyAbs")?.checked) {
        userOpt.selected = true;
      }
      select.appendChild(userOpt);
    }
    if (activeProfileId && activeProfileId !== "__all__" && currentProfiles[activeProfileId]) {
      const profOpt = document.createElement("option");
      profOpt.value = `profile:${activeProfileId}`;
      profOpt.innerText = `👤 Profile Primary Card (${currentProfiles[activeProfileId].display_name || activeProfileId})`;
      select.appendChild(profOpt);
    } else {
      Object.values(currentProfiles || {}).forEach(p => {
        const profOpt = document.createElement("option");
        profOpt.value = `profile:${p.id}`;
        profOpt.innerText = `👤 ${p.display_name || p.id} — Profile Primary Card`;
        select.appendChild(profOpt);
      });
    }
  }

  // Add fleet card options
  cardSlots.forEach(s => {
    const isPersonal = s.kind === "profile_primary" || s.kind === "user_primary" || s.kind === "user_session" || s.kind === "profile_cache" || s.kind === "guest_stream";
    if (!isPersonal) {
      const opt = document.createElement("option");
      opt.value = `slot:${s.slot_id}`;
      opt.innerText = `💳 ${(s.nickname || s.title || s.slot_id)} (${s.slot_id})`;
      select.appendChild(opt);
    }
  });

  document.getElementById("linkCardModal").style.display = "flex";
}

function closeLinkCardModal() {
  document.getElementById("linkCardModal").style.display = "none";
}

async function executeAssignToSlot() {
  const targetVal = document.getElementById("targetSlotSelect").value;
  if (!targetVal || !activeBook) return;

  const isYoto = Boolean(activeBook.type && activeBook.type !== "abs");
  const modeVal = document.getElementById("bookModalModeSelect").value;
  const followSeries = Boolean(document.getElementById("bookModalFollowSeries")?.checked) || modeVal === "offline_series";
  const seriesStart = followSeries ? (document.getElementById("bookModalSeriesStart")?.value || "selected") : "selected";
  let payload = isYoto
    ? {
        yoto_card_id: activeBook.id,
        title: activeBook.title,
        author: activeBook.author
      }
    : {
        abs_item_id: activeBook.id,
        mode: modeVal,
        group_kind: followSeries ? "series" : "book",
        series_name: activeBook.series || "",
        series_start: seriesStart,
        title: activeBook.title,
        author: activeBook.author
      };

  if (targetVal.startsWith("user:")) {
    payload.use_user_abs = true;
  } else if (targetVal.startsWith("profile:")) {
    payload.profile_id = targetVal.split(":")[1];
  } else {
    payload.slot_id = targetVal.split(":")[1];
  }

  try {
    const res = await fetch("/api/cards/assign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      closeLinkCardModal();
      alert(`Linked '${activeBook.title}' successfully!`);
      loadCards();
    } else {
      alert("Failed to assign book to card slot.");
    }
  } catch (err) {
    console.error("Assign error:", err);
  }
}

