<p align="center">
  <img src="https://raw.githubusercontent.com/YotoStoryDeck/StoryDeck-Core/main/static/kiosk/icon.svg" alt="StoryDeck Logo" width="88" height="88">
</p>

# StoryDeck Core v0.2.0 — First Full Stable Release

**Release Tag:** `v0.2.0`
**Release Channel:** Full Production Release (`:latest`)
**Supported Platforms:** Docker (`linux/amd64`, `linux/arm64`) & Python 3.11+
**Release Date:** November 27, 2026

---

## 📻 Overview

Welcome to **StoryDeck Core `v0.2.0`** — the **first full stable release** of the open-source audio hub for your **Yoto Players**, **Audiobookshelf** library, **Tablet Kiosks**, and **StoryDeck Touch** displays!

Everything runs inside a single self-hosted container with no external bridge services required. Whether listeners are browsing curated stories on a tablet, listening offline during road trips, or you are controlling room players from your phone, StoryDeck Core brings it all together in one easy-to-manage place.

---

## 🌟 What You Can Do with StoryDeck Core

### 1. 🎧 Stream Audiobookshelf & Build Offline Yoto Cards in One Place
* **Instant Streaming to Any Yoto Player:** Start any audiobook from Audiobookshelf directly on a physical Yoto Player and automatically pick up right where you left off—including two-way listening progress syncing.
* **Three Flexible Offline Modes for Travel & Bedtime:**
  * **Next 5 Hours:** Great for multi-day trips or long books—loads the next five hours of listening onto a card with gentle rewind, or grabs the whole book automatically if it is under 5 hours.
  * **Whole Book:** Downloads an entire audiobook cleanly onto a single MYO card.
  * **Whole Series:** Packs multiple books in a series onto one card and automatically advances finished books to the next title—choose whether to start from Book #1, the current unfinished book, or the book you tapped.
* **Play While It Builds & Keep Flash Warm:** Offline builds become playable on your Yoto Player within minutes while the remaining chapters finish uploading, recover automatically from temporary internet drops or restarts, and keep recently played books cached on your Yoto Player for Wi-Fi-free travel.
* **Chapter Artwork & Markers:** Automatically generates colorful 16×16 chapter pixel art (or matches community/custom chapter icons) and looks up accurate chapter markers for long audiobooks.

---

### 2. 📱 Control Everything from the Web Portal (`/`) & Admin Console (`/admin`)
* **Mobile-Friendly Player Remote:** Adjust volume smoothly within your Yoto App's day and night limits, skip ±30 seconds, jump to specific chapters, change playback speed, set sleep timers, and view live playback status across every Yoto Player.
* **Quick Profile & Player Switching:** Tap between **Listener Profiles** (or switch to **`🎧 My ABS`** for your own personal Audiobookshelf listening on a dedicated personal card) and target any online player.
* **Easy Physical Card Pairing:** Tap a physical MYO card onto a player to pair, nickname, or switch that card between **Streaming** and **Offline** modes, or browse and organize your existing Yoto Cloud playlists.

---

### 3. 🤹 Turn Any Tablet into a Standalone Audio Jukebox (`/kiosk/`)
Give listeners an independent, distraction-free way to pick stories on any modern iPad (`iPadOS 14+`), Android tablet, Amazon Fire tablet, or desktop browser:
* **Shared Profile Switcher:** A welcome screen with time-of-day greetings lets each listener tap their own avatar to open only the library curated for them.
* **Fast Search, Filtering & Series Browsing:** Browse the cover-art grid, jump into grouped multi-book series, revisit **Recently Played** titles (tracked per listener or per player), or search by title, author, or series—automatically hiding Audiobookshelf filters if you run StoryDeck in Yoto-only mode.
* **Touch-Friendly Player & Night Clock:** Includes smooth drag-to-seek and volume sliders, ±30s skip buttons, a dimmable **Night Clock** screensaver, and full-screen tablet app support when added to your Home Screen.
* **Instant PIN-Protected Settings Menu:** Lock a tablet to a specific room speaker, configure sleep schedules and screen brightness, and protect settings behind a 4-digit **Settings PIN**.

---

### 4. 🎯 Listener Profiles, Curation & Travel Safety
* **Per-Listener Libraries & Discovery Limits:** Choose which Audiobookshelf tags, series, and Yoto cards each Listener Profile can access, prevent sequels from appearing before Book #1, and group commercial and MYO Yoto cards into ordered series.
* **Separate Limits for Bedside Touch & Tablets:** Keep small bedside controllers fast and uncluttered with a tailored rotation while letting larger tablets browse the full approved library.
* **Travel Proximity Protection:** Automatically detects when a Yoto Player is away from home so scheduled sleep timers do not unexpectedly stop travel listening, and warns before starting playback on remote players.

---

### 5. 🔐 Multi-User Access, Single Sign-On & Touch Hardware Support
* **Flexible Access Roles:** Supports **Admin**, **Member**, and optional **Trusted LAN Guest** access, optional Single Sign-On (**Authentik**, **Authelia**, **Keycloak**, **Pocket ID**, **Cloudflare Access**, or OIDC), and automatic personal Audiobookshelf account matching.
* **StoryDeck Touch Support (`ESP32-S3`):** New hardware displays appear in a quarantine approval queue before joining and support 1-click firmware updates, rollbacks, and remote cover cache cleanup from the Admin Console.

---

## 🚀 Quick Start & Upgrading

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

To install or upgrade an existing container (database upgrades run automatically on startup):

```bash
docker compose pull
docker compose up -d
```

> [!NOTE]
> Everything runs inside this single container—no separate bridge service is required. Audiobookshelf is completely optional if you only want to manage and play your Yoto cards.

---

## 📋 System Compatibility

| Component | Recommended | Notes |
| :--- | :--- | :--- |
| **Yoto Account** | Any Yoto account + Developer Client ID | Quick sign-in setup inside Admin Settings |
| **Audiobookshelf** | `v2.10.0+` (minimum `v2.4.0`) | Optional audiobook and podcast library |
| **StoryDeck Touch Firmware** | `v0.1.0+` | Dedicated bedside ESP32-S3 touchscreen display |
| **Tablet Kiosk Browsers** | iPadOS 14+, Android Chrome/WebView, Fire OS | Also works in any modern desktop browser |
| **Docker** | Docker 20.10+ with Compose v2 | Multi-arch (`linux/amd64` & `linux/arm64`) |

---

## 🤝 Ecosystem Links

* **StoryDeck Core:** [github.com/YotoStoryDeck/StoryDeck-Core](https://github.com/YotoStoryDeck/StoryDeck-Core)
* **StoryDeck Touch Firmware:** [github.com/YotoStoryDeck/StoryDeck-Touch](https://github.com/YotoStoryDeck/StoryDeck-Touch)
* **Touch Web Flasher:** [yotostorydeck.github.io/StoryDeck-Touch](https://yotostorydeck.github.io/StoryDeck-Touch/)
