"""
Audible / Audnexus Chapter Finder for StoryDeck Core.
Identifies Audiobookshelf books lacking real chapter markers (none, single chapter, or one per audio file),
queries Audible/Audnexus via ABS's built-in `/api/search/books` & `/api/search/chapters` providers,
automatically strips Audible intro/outro branding when it improves duration alignment,
and writes approved chapters back to Audiobookshelf (`POST /api/items/<id>/chapters`).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
import clients

logger = logging.getLogger("storydeck.bridge.chapter_finder")

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
GOOD_SECONDS = 15.0
CHECK_SECONDS = 120.0
REGIONS = ("us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es")


def chapter_reason(item: Dict[str, Any], every: bool = False) -> str:
    """
    Returns why a book's chapters are worth replacing:
    - 'none': 0 or 1 chapter
    - 'files': 1 chapter per audio file (auto-generated from filenames)
    - 'own': has chapters, only included when `every=True`
    - '': does not need chapter lookup
    """
    media = item.get("media") or {}
    tracks = media.get("numTracks") or media.get("numAudioFiles") or len(media.get("tracks") or media.get("audioFiles") or [])
    if not tracks or not media.get("duration"):
        return ""
    n = media.get("numChapters")
    if n is None:
        n = len(media.get("chapters") or [])
    if n <= 1:
        return "none"
    if tracks > 1 and n == tracks:
        return "files"
    return "own" if every else ""


def _mmss(s: float) -> str:
    sec = int(round(abs(s)))
    h, m, rem = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}h {m}m" if h else (f"{m}m {rem}s" if m else f"{rem}s")


def _strip_branding(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    """Removes Audible's 'This is Audible' intro and outro when present."""
    intro = float(data.get("brandIntroDurationMs") or 0.0)
    outro = float(data.get("brandOutroDurationMs") or 0.0)
    out = []
    for i, c in enumerate(data.get("chapters") or []):
        start = float(c.get("startOffsetMs") or 0.0)
        start = i * 1000.0 if start < intro else (start - intro)
        out.append({**c, "startOffsetMs": start})
    if out and float(out[-1].get("lengthMs") or 0.0) <= outro:
        out.pop()
    runtime_sec = (float(data.get("runtimeLengthMs") or 0.0) - intro - outro) / 1000.0
    return out, runtime_sec


def build_proposal(data: Dict[str, Any], duration: float) -> Dict[str, Any]:
    """Builds a chapter proposal from Audnexus chapter data for audio of `duration` seconds."""
    raw = list(data.get("chapters") or [])
    runtime = float(data.get("runtimeLengthMs") or 0.0) / 1000.0
    branding = bool(data.get("brandIntroDurationMs") or data.get("brandOutroDurationMs"))
    removed = False
    if branding and duration:
        stripped, stripped_runtime = _strip_branding(data)
        if abs(stripped_runtime - duration) < abs(runtime - duration):
            raw, runtime, removed = stripped, stripped_runtime, True

    chapters = []
    dropped = 0
    for c in raw:
        start = float(c.get("startOffsetMs") or 0.0) / 1000.0
        if duration and start >= duration:
            dropped += 1
            continue
        end = start + (float(c.get("lengthMs") or 0.0) / 1000.0)
        chapters.append({
            "title": str(c.get("title") or f"Chapter {len(chapters) + 1}").strip(),
            "start": round(start, 3),
            "end": round(min(end, duration) if duration else end, 3),
        })
    chapters.sort(key=lambda ch: ch["start"])
    for a, b in zip(chapters, chapters[1:]):
        a["end"] = b["start"]
    tail = 0.0
    if chapters and duration:
        tail = duration - chapters[-1]["end"]
        chapters[-1]["end"] = round(duration, 3)

    diff = (duration - runtime) if runtime else 0.0
    warnings = []
    if not chapters:
        warnings.append("Audible has no chapters for this book.")
    elif len(chapters) == 1:
        warnings.append("Audible has only one chapter for this book.")
    if runtime and abs(diff) > GOOD_SECONDS:
        warnings.append(
            f"Audio is {_mmss(diff)} {'longer' if diff > 0 else 'shorter'} than Audible edition"
            + ("; likely a different recording." if abs(diff) > CHECK_SECONDS else ".")
        )
    if dropped:
        warnings.append(f"{dropped} Audible chapter(s) start after audio ends and were dropped.")
    if tail > 60:
        warnings.append(f"The last chapter was extended by {_mmss(tail)} to reach the end of the audio.")
    if data.get("isAccurate") is False:
        warnings.append("Audible marks these chapter times as approximate.")

    quality = (
        "bad"
        if (not chapters or len(chapters) == 1 or (runtime and abs(diff) > CHECK_SECONDS))
        else (
            "check"
            if ((runtime and abs(diff) > GOOD_SECONDS) or dropped or data.get("isAccurate") is False)
            else "good"
        )
    )
    return {
        "chapters": chapters,
        "runtime": round(runtime, 1),
        "duration": round(duration, 1),
        "difference": round(diff, 1),
        "branding_removed": removed,
        "dropped": dropped,
        "warnings": warnings,
        "quality": quality,
    }


def lookup_audible_chapters_for_item(
    item_id: str,
    asin_override: str = "",
    region: str = "us",
) -> Dict[str, Any]:
    """
    Looks up proposed Audible/Audnexus chapters for an Audiobookshelf book `item_id`
    via ABS's `/api/search/books` and `/api/search/chapters` endpoints.
    """
    if not clients.abs_client or not clients.abs_client.base_url:
        return {"ok": False, "error": "Audiobookshelf client is not configured."}

    abs_c = clients.abs_client
    reg = (region or "us").lower()
    if reg not in REGIONS:
        reg = "us"
    provider = "audible" if reg == "us" else f"audible.{reg}"

    item = abs_c.get_item(item_id)
    if not item:
        return {"ok": False, "error": f"ABS item '{item_id}' not found."}

    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    duration = float(media.get("duration") or 0.0)
    title = str(meta.get("title") or item.get("name") or "")
    author = str(meta.get("authorName") or meta.get("author") or "")
    asin = (asin_override or meta.get("asin") or "").strip().upper()
    headers = abs_c.get_headers()

    if not ASIN_RE.match(asin):
        try:
            s_res = requests.get(
                f"{abs_c.base_url.rstrip('/')}/api/search/books",
                headers=headers,
                params={"provider": provider, "title": title, "author": author, "id": item_id},
                timeout=30,
            )
            if s_res.status_code == 200:
                candidates = [m for m in (s_res.json() or []) if isinstance(m, dict) and ASIN_RE.match(str(m.get("asin") or ""))]
                if candidates:
                    asin = str(candidates[0]["asin"]).upper()
        except Exception as e:
            logger.debug("ABS audible book search error for %s: %s", item_id, e)

    if not ASIN_RE.match(asin):
        return {"ok": False, "error": "No matching Audible ASIN found for this book.", "item_id": item_id, "title": title}

    try:
        c_res = requests.get(
            f"{abs_c.base_url.rstrip('/')}/api/search/chapters",
            headers=headers,
            params={"asin": asin, "region": reg.upper()},
            timeout=20,
        )
        if c_res.status_code != 200:
            return {"ok": False, "error": f"ABS chapter lookup returned HTTP {c_res.status_code}", "asin": asin}
        ch_data = c_res.json() or {}
        if ch_data.get("error"):
            return {"ok": False, "error": str(ch_data["error"]), "asin": asin}
        proposal = build_proposal(ch_data, duration)
        return {
            "ok": True,
            "item_id": item_id,
            "title": title,
            "author": author,
            "asin": asin,
            "region": reg,
            "current_chapters_count": len(media.get("chapters") or []),
            "proposal": proposal,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "asin": asin}


def apply_proposed_chapters_to_item(item_id: str, chapters: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Writes approved chapters directly to Audiobookshelf via POST /api/items/<id>/chapters."""
    if not clients.abs_client:
        return False, "Audiobookshelf client is not configured."
    if not chapters:
        return False, "No chapters provided."
    formatted = [
        {
            "id": idx,
            "start": float(ch.get("start") or 0.0),
            "end": float(ch.get("end") or 0.0),
            "title": str(ch.get("title") or f"Chapter {idx + 1}").strip(),
        }
        for idx, ch in enumerate(chapters)
    ]
    ok = clients.abs_client.update_item_chapters(item_id, formatted)
    return (True, "") if ok else (False, "Failed to update chapters on Audiobookshelf (verify Master Key has update permission).")
