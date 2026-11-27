<p align="center">
  <img src="https://raw.githubusercontent.com/YotoStoryDeck/StoryDeck-Core/main/static/kiosk/icon.svg" alt="StoryDeck Logo" width="88" height="88">
</p>

# StoryDeck Core v0.2.0 — First Full Stable Release

**Release Tag:** `v0.2.0`
**Classification:** First Full Production Release (Stable · `:latest`)
**Target Architecture:** Docker Multi-Arch (`linux/amd64`, `linux/arm64`) & Python 3.11+ (`ffmpeg` included)
**Release Date:** November 27, 2026

---

## 📻 Overview

Welcome to **StoryDeck Core `v0.2.0`** — the **first full stable release** (`:latest`) of the open-source, self-hosted orchestration hub for the StoryDeck smart audio ecosystem!

StoryDeck Core unifies [Yoto](https://my.yotoplay.com/) players and cards with optional [Audiobookshelf](https://www.audiobookshelf.org/) audiobook libraries inside a **single container**, delivering real-time MQTT playback telemetry, embedded audio streaming and offline Yoto card uploads, a **Parent Portal (`/` & `/admin`)**, a self-contained **Tablet Kiosk (`/kiosk/`)**, and microcontroller-optimized APIs for [**StoryDeck Touch**](https://github.com/YotoStoryDeck/StoryDeck-Touch) (`ESP32-S3`).

---

## 🌟 Major Highlights

### 1. 🎧 Consolidated Streaming, Offline & Native MQTT Engine (`core/bridge/`)
Zero external bridge container required — all streaming, transcoding, icon rendering, and MQTT telemetry run natively inside StoryDeck Core:
* **On-the-Fly Streaming (`64kbps` mono MP3, HTTP `206`):** Embedded `ffmpeg` audio streamer with 5-second `WarmBuffer` pre-encoding, `QuietSpots` silence-snapped chapter cuts, 99-track sliding window (`window_start`, step `90`), tap-time ABS resume with pause-without-`Range` handling (`2s`/`15s` rewind), and 2.5s silent cue + MQTT chapter auto-jump.
* **Three Offline MYO Upload Modes:**
  * `⏱️ 5h Next Window` (`offline_next`) with whole-book shortcut for books $\le 5\text{h}$ and 1.5h chapter-snapped bedtime lookback.
  * `💾 Whole Book` (`offline_book`) with binary-search packing into $\le 99$ tracks.
  * `📚 Whole Series` (`offline_series`) supporting `chapters` and `condense` packing, `selected`/`active`/`beginning` start points, and automatic rolling advancement to the next book on completion.
* **Live Hybrid Mixed Playlists & Hardware Flash Retention:** Publishes playable mixed playlists every 5 tracks during builds (`save_mixed_playlist`), preserves distinct `yoto_card_id` entries across `offline_retention_limit` with series-aware LRU pruning, reuses cached Content-Addressed `yoto_sha256` audio blocks, and recovers automatically from interrupted builds (`resume_interrupted_jobs`) as well as transient DNS, network, `5xx`/`429`, and `401` token-expiry blips during long uploads.
* **Card Chapter Tools:** `own_place` local SQLite progress isolation, Yoto CDN cover uploads & content-digest deduplicated 16×16 pixel chapter icons (with `resolve_source_chapter_idx()` mapping for split/multi-book tracks), Audible/Audnexus Chapter Finder, and legacy MYO card-to-ABS Viterbi position transfer.

---

### 2. 📱 Parent Portal (`/`), Admin Console (`/admin`) & Physical MYO Pairing
* **Unified Responsive Navigation:** Shared collapsible left sidebar and self-service **✏️ Profile** modal across `/` and `/admin` with live Core status/version indicator (`#coreVersionBadge`) and upstream release checker.
* **Profile Avatar Carousel & Target Yoto Player Selector:** Quick-switch between household child profiles, **`🎧 My ABS`** personal library override (with dedicated `user_<uid>` Personal Card or ephemeral `guest_stream`), and household Yoto players (`🟢`/`🔴`/`✈️ AWAY` indicators, `Default` & `Online` tags, and cover-art MQTT playback confirmation overlay).
* **Full Transport & Library Browser:** `0–100%` volume slider honoring Yoto day/night limits, scrub seek, `±30s`, speed, sleep timers, Continue Listening, strict audio-only filtering (`ABSClient.is_valid_audio_item`), and source filters (`All`, `Audiobookshelf`, `Yoto Cards & MYO`).
* **Live Card Pairing & Switcher:** Live MQTT physical MYO card detection (`Editable MYO` & blank `hMkni`), permanent nicknames, instant mode switching (`⚡ Stream` ↔ `⏱️ 5h` ↔ `💾 Whole Book` ↔ `📚 Series`), and Yoto Cloud playlist management.

---

### 3. 🤹 Self-Contained Tablet Kiosk (`/kiosk/`)
Built specifically for modern bedside iPads (iPadOS 14+), Android/Fire tablets, and wall displays:
* **Zero External CDN Dependencies:** Self-hosted `Fredoka` font (`static/kiosk/fonts/fredoka-latin.woff2`), custom **StoryDeck** vector & high-DPI raster app icons (`icon.svg`, `icon-192.png`, `icon-512.png`, `180×180` `apple-touch-icon.png`), Screen Wake Lock API, `100dvh` viewport, and iOS safe-area insets.
* **Smooth Responsive Performance:** Single-request `GET /api/kids/<child>/catalog` endpoint with stale-while-revalidate caching, in-place telemetry & `PlayerScreen.tick()` updates without DOM wipes, deterministic intrinsic-ratio catalog grid tiles, and fluid Night Clock screensaver.
* **Smart Mode Adaptation:** Client-side search across title/author/series, `abs_configured`-gated `All` / `ABS` / `Yoto` source filter chips (automatically hidden in Yoto-only standalone deployments), profile & player-scoped **Recently Played** (`device:<id>` history), pointer-drag scrub/volume bars, resolution presets (`?tablet10`, `?tablet43`, `?tablet8`, `?1080p`), and instant 4-digit PIN Grown-Ups gate.

---

### 4. 🔐 Users, RBAC, SSO / OIDC & Audiobookshelf Account Linking
* **Household Accounts & Roles:** Initial Super Admin protection, `admin` / `member` / `guest` tiers, and optional Trusted LAN / Sitter access.
* **Unified Sign-In & Enterprise SSO (`/login`):** Native **OpenID Connect (OIDC) Authorization Code + PKCE (`S256`)** and Reverse-Proxy ForwardAuth (`Authentik`, `Authelia`, `Keycloak`, `Pocket ID`, `Cloudflare Access`, or generic OIDC) with `groups` role sync and `post_logout_redirect_uri`.
* **Audiobookshelf Account Auto-Linking:** Matches signed-in users by email or username and mints/revokes user-scoped ABS API keys automatically, with automatic master-key fallback and self-service controls.

---

### 5. 🗄️ Modular SQLite Schema v17, Curation & Hardware Fleet Management
* **Automated Schema Migrations:** Zero-downtime SQLite schema progression through `v17` under `database/` (`schema.py`, `profiles.py`, `devices.py`, `curation.py`, `history.py`, `settings.py`, `auth.py`).
* **Dual-Limit Curation & Manual Series Overrides:** Separate ESP32 Touch (`60` default) and Tablet Kiosk (`0` = unlimited) limits following deterministic Superset ordering, atomic whole-series rotation, `has_physical` tracking, and drag-and-drop **Yoto Card Series & Sequence Overrides**.
* **Zero-Trust Device Onboarding & Remote OTA:** Automatic quarantine for new Touch/Kiosk displays, rotating per-device JWTs, Core-hosted ESP32 `firmware.bin` 1-click OTA push/rollback, remote MicroSD artwork cache pruning, and `✈️ AWAY` travel proximity interlocks protecting roaming Yoto players from bedtime curfews.

---

### 6. 🧪 Overhauled Test Suite & GitHub Actions CI
* **Hardened Synthetic Unit Suite (`126` tests):** Domain-organized `tests/unit/` suite guarded by `NetworkGuard` and ephemeral SQLite databases, requiring zero local DB or credentials.
* **Optional Sanitized Local DB Snapshot Mode:** Reads `data/broker.db` strictly read-only (`?mode=ro`) into temporary space and scrubs all secrets, tokens, emails, and LAN IPs before validation (`python scripts/run_tests.py --with-local-db`).
* **CI & Failure Commit Reporting:** Automated Python 3.11 + `ffmpeg` unit tests, Node 24 frontend JS syntax checks, and structured failure commit comments.

---

## 🚀 Deployment & Upgrade Guide

### Docker Compose (`:latest` / `:v0.2.0`)

```yaml
services:
  storydeck-core:
    image: ghcr.io/yotostorydeck/storydeck-core:latest
    container_name: storydeck-core
    restart: unless-stopped
    ports:
      - "${PORT:-5055}:5055"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./cache:/app/cache
```

Pull and recreate the container (SQLite migrations through schema `v17` apply automatically on boot without manual intervention):

```bash
docker compose pull
docker compose up -d
```

> [!NOTE]
> StoryDeck Core is fully self-contained and does **not** require a separate bridge container or `YOTO_CLIENT_SECRET` (public OAuth2 PKCE is handled natively inside Admin Settings). Audiobookshelf is optional—Core also runs standalone using only Yoto Cloud.

---

## 📋 Compatibility Matrix

| Component | Minimum Version | Recommended | Notes |
| :--- | :--- | :--- | :--- |
| **StoryDeck Touch Firmware** | `v0.1.0` | `v0.1.0+` | Physical ESP32-S3 bedside display (`?client=touch` / OTA) |
| **Audiobookshelf** | `v2.4.0` | `v2.10.0+` | Optional audiobook & podcast library source |
| **Yoto Play Account** | Any | MyYoto + Developer Client ID | Public PKCE OAuth configured in Admin UI |
| **Container Runtime** | Docker 20.10+ | Docker 26.0+ | Multi-arch `linux/amd64` & `linux/arm64` |

---

## 🤝 Ecosystem Links

* **StoryDeck Core:** [github.com/YotoStoryDeck/StoryDeck-Core](https://github.com/YotoStoryDeck/StoryDeck-Core)
* **StoryDeck Touch Firmware:** [github.com/YotoStoryDeck/StoryDeck-Touch](https://github.com/YotoStoryDeck/StoryDeck-Touch)
* **Touch Web Flasher:** [yotostorydeck.github.io/StoryDeck-Touch](https://yotostorydeck.github.io/StoryDeck-Touch/)
