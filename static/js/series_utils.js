// -----------------------------------------------------------------------------
// Shared Series Normalization, Dropdown Builder & Library Sorting Utilities
// Used by both Parent Portal (static/js/portal/library.js) and Admin Console
// (static/js/admin/library.js) to guarantee identical series grouping & sorting.
// -----------------------------------------------------------------------------

(function (global) {
  "use strict";

  function cleanTitleForSort(str) {
    return String(str || "")
      .trim()
      .replace(/^(the|a|an)\s+/i, "")
      .toLowerCase();
  }

  function normalizeSeriesEntry(rawName, rawOrder = "") {
    let name = String(rawName || "").trim();
    let order = String(rawOrder || "").trim();
    if (!name) return null;

    const matchSeq = name.match(/\s*#\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–—,&]\s*[0-9]+(?:\.[0-9]+)?)*)$/);
    if (matchSeq) {
      if (!order) order = matchSeq[1].replace(/\s+/g, "");
      name = name.slice(0, matchSeq.index).trim();
    } else {
      const matchBook = name.match(/[,:\s]+(?:Books?|Volumes?|Vol\.?|Parts?)\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–—,&]\s*[0-9]+(?:\.[0-9]+)?)*)$/i);
      if (matchBook) {
        if (!order) order = matchBook[1].replace(/\s+/g, "");
        name = name.slice(0, matchBook.index).trim();
      }
    }

    return name ? { name, order } : null;
  }

  function extractItemSeriesList(item) {
    const out = [];
    const seen = new Set();
    const pushEntry = (entry) => {
      if (!entry || !entry.name) return;
      const key = entry.name.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(entry);
    };

    const rawOrderFallback = (item && (item.series_order || item.sequence_display)) || "";
    if (item && Array.isArray(item.series_list) && item.series_list.length > 0) {
      item.series_list.forEach((s) => {
        if (!s || typeof s !== "object") return;
        pushEntry(normalizeSeriesEntry(s.name, s.order || s.sequence || rawOrderFallback));
      });
    } else if (item && item.series) {
      const parts = String(item.series).split(",");
      if (parts.length > 1) {
        parts.forEach((p) => pushEntry(normalizeSeriesEntry(p)));
      } else {
        pushEntry(normalizeSeriesEntry(item.series, rawOrderFallback));
      }
    }
    return out;
  }

  function buildSeriesDropdownOptions(items, opts = {}) {
    const excludeSingle = opts.excludeSingleCardSeries !== false;
    const displayByLower = new Map();
    const countByLower = new Map();

    (items || []).forEach((it) => {
      const entries = extractItemSeriesList(it);
      entries.forEach((entry) => {
        const key = entry.name.toLowerCase();
        if (!displayByLower.has(key)) {
          displayByLower.set(key, entry.name);
        }
        countByLower.set(key, (countByLower.get(key) || 0) + 1);
      });
    });

    const sortByName = (a, b) => cleanTitleForSort(a).localeCompare(cleanTitleForSort(b));
    const allSeriesSorted = Array.from(displayByLower.values()).sort(sortByName);
    const filteredSeriesSorted = allSeriesSorted
      .filter((name) => !excludeSingle || (countByLower.get(name.toLowerCase()) || 0) > 1);

    return { allSeriesSorted, filteredSeriesSorted };
  }

  function populateSeriesSelect(selectEl, items, opts = {}) {
    const {
      excludeSingleCardSeries = true,
      allValue = "",
      allLabel = "All Series / Groups",
      preserveSelection = true
    } = opts;

    const result = buildSeriesDropdownOptions(items, { excludeSingleCardSeries });
    if (!selectEl) return result;

    const prevVal = preserveSelection ? selectEl.value : allValue;
    selectEl.innerHTML = "";

    const allOpt = document.createElement("option");
    allOpt.value = allValue;
    allOpt.textContent = allLabel;
    selectEl.appendChild(allOpt);

    result.filteredSeriesSorted.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name.toLowerCase();
      opt.textContent = `📚 ${name}`;
      selectEl.appendChild(opt);
    });

    if (preserveSelection && prevVal && Array.from(selectEl.options).some((o) => o.value === prevVal)) {
      selectEl.value = prevVal;
    } else {
      selectEl.value = allValue;
    }
    return result;
  }

  function itemMatchesSeries(item, selectedSeriesValue) {
    const needle = String(selectedSeriesValue || "").trim().toLowerCase();
    if (!needle || needle === "all") return true;
    return extractItemSeriesList(item).some((s) => s.name.toLowerCase() === needle);
  }

  function parseSequenceOrder(rawOrder) {
    const raw = String(rawOrder || "").trim();
    const m = raw.match(/^\s*(\d+(?:\.\d+)?)/);
    if (m) {
      return { hasNum: true, num: parseFloat(m[1]), raw };
    }
    return { hasNum: false, num: Infinity, raw };
  }

  function _compareSequence(orderA, orderB) {
    const a = parseSequenceOrder(orderA);
    const b = parseSequenceOrder(orderB);
    if (a.hasNum && b.hasNum) {
      if (a.num !== b.num) return a.num - b.num;
    } else if (a.hasNum !== b.hasNum) {
      return a.hasNum ? -1 : 1;
    }
    if (a.raw && b.raw && a.raw !== b.raw) {
      return a.raw.localeCompare(b.raw);
    }
    return 0;
  }

  function _resolveItemSeriesOrder(item, selectedSeries) {
    const list = extractItemSeriesList(item);
    if (!list.length) return { name: "", order: "", hasSeries: false };
    const needle = String(selectedSeries || "").trim().toLowerCase();
    if (needle && needle !== "all") {
      const match = list.find((s) => s.name.toLowerCase() === needle);
      if (match) return { name: match.name, order: match.order, hasSeries: true };
    }
    return { name: list[0].name, order: list[0].order, hasSeries: true };
  }

  function sortItems(items, opts = {}) {
    const {
      sortBy = "name",
      selectedSeries = "",
      highlightsFirst = false
    } = opts;

    const list = Array.isArray(items) ? [...items] : [];
    const targetSeries = String(selectedSeries || "").trim().toLowerCase();
    const hasSpecificSeries = Boolean(targetSeries && targetSeries !== "all");

    list.sort((a, b) => {
      if (highlightsFirst && sortBy !== "series_order") {
        const hlDiff = (b.is_highlight ? 1 : 0) - (a.is_highlight ? 1 : 0);
        if (hlDiff !== 0) return hlDiff;
      }

      if (sortBy === "series_order") {
        const sa = _resolveItemSeriesOrder(a, hasSpecificSeries ? targetSeries : "");
        const sb = _resolveItemSeriesOrder(b, hasSpecificSeries ? targetSeries : "");

        if (sa.hasSeries !== sb.hasSeries) {
          return sa.hasSeries ? -1 : 1;
        }
        if (sa.hasSeries && sb.hasSeries) {
          if (!hasSpecificSeries) {
            const nameDiff = cleanTitleForSort(sa.name).localeCompare(cleanTitleForSort(sb.name));
            if (nameDiff !== 0) return nameDiff;
          }
          const seqDiff = _compareSequence(sa.order, sb.order);
          if (seqDiff !== 0) return seqDiff;
        }
      }

      return cleanTitleForSort(a && a.title).localeCompare(cleanTitleForSort(b && b.title));
    });

    return list;
  }

  global.StoryDeckSeries = {
    cleanTitleForSort,
    normalizeSeriesEntry,
    extractItemSeriesList,
    buildSeriesDropdownOptions,
    populateSeriesSelect,
    itemMatchesSeries,
    parseSequenceOrder,
    sortItems
  };
})(window);
