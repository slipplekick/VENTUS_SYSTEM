# VENTUS//SYS

> A Spotify-connected music intelligence dashboard. Scores tracks against your personal vibe profile, decrypts Ghost Signals via local audio physics, and lives in your system tray.

---

## What is this?

VENTUS is a desktop app that connects to your Spotify account and scores every track you play against your personal taste profile in real time. It analyzes energy, valence, danceability, BPM, key, and more — and tells you how well a track fits your vibe.

**Features:**
- **Live Now Playing** — scores the current track as it plays
- **Queue Scanner** — scores your upcoming queue before you hear it
- **Playlist Auditor** — scores every track in any playlist at once
- **Vault** — local SQLite cache of every track ever analyzed, grows over time
- **Ghost Signal Decryption** — if a track has no data available, VENTUS downloads the 30s preview and analyzes it locally using audio physics
- **DNA Rebuild** — pulls genre tags from Last.fm to build your genre fingerprint
- **System Tray** — runs quietly in the background, optional boot on startup

---

## Requirements

- Windows 10 or 11
- A free [Spotify account](https://spotify.com) (Premium not required for scoring, required for playback control)
- A free [Spotify Developer app](https://developer.spotify.com/dashboard)
- A free [Last.fm API key](https://www.last.fm/api/account/create)

> SETUP.bat will walk you through all of this step by step. Python and Node.js will be installed automatically if you don't have them.

---

## Installation

### Step 1 — Download

Download this repo as a ZIP and extract it anywhere you want (e.g. `C:\Users\You\VENTUS_SYSTEM`).

### Step 2 — Create a Spotify Developer App

You need a free Spotify Developer app to get your Client ID and Secret.

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Log in and click **Create app**
3. Fill in any name and description
4. Under **Redirect URIs**, add exactly this — no trailing slash:
   ```
   http://127.0.0.1:8888/callback
   ```
5. Under **APIs used**, check **Web API**
6. Save the app, then copy your **Client ID** and **Client Secret**

> ⚠️ The redirect URI must be `127.0.0.1` not `localhost` — Spotify's dashboard may suggest localhost but VENTUS won't work with it.

### Step 3 — Get a Last.fm API Key

Last.fm is used for the DNA Rebuild feature (genre fingerprinting).

1. Go to [last.fm/api/account/create](https://www.last.fm/api/account/create)
2. Fill in any app name and description
3. Copy the **API key** it gives you

### Step 4 — Run SETUP.bat

Double-click **SETUP.bat** inside the folder. It will:

- Install Python 3.11 if you don't have it
- Install Node.js LTS if you don't have it
- Install all Python and Node dependencies automatically
- Ask for your Spotify Client ID, Client Secret, and Last.fm API key
- Open a browser window for Spotify login
- Write your credentials to a `.env` file automatically

When it says **SETUP COMPLETE**, you're done.

### Step 5 — Launch

Double-click **LAUNCH.bat** to start VENTUS any time.

The app will appear in your system tray. Double-click the tray icon to open the dashboard.

---

## First Run Tips

- **Play something on Spotify first** — VENTUS needs an active Spotify session to show Now Playing data
- **The Vault starts empty** — it fills up automatically as you use the app. The more you use it, the faster scoring gets
- **Rebuild DNA** — go to the DNA panel and hit Rebuild after you've synced a playlist. This builds your genre fingerprint from Last.fm tags
- **Sync a playlist** — use the Sync feature to bulk-add a playlist to your training set and Vault at once

---

## Re-authorising Spotify

If your token expires or you want to switch accounts:

1. Delete the `.env` file in the app folder
2. Run `SETUP.bat` again — it skips all install steps and goes straight to the login screen

---

## Troubleshooting

**Nothing shows in Now Playing**
- Make sure Spotify is actually playing something (not paused)
- Make sure you're logged into the same Spotify account you used during setup

**SETUP.bat fails on OAuth**
- Double-check your Client ID and Secret
- Make sure the Redirect URI in your Spotify Developer app is exactly `http://127.0.0.1:8888/callback`
- Make sure nothing else is using port 8888

**Ghost Signal says LIBROSA NOT INSTALLED**
- Run `pip install librosa` in a terminal. SETUP.bat tries to install it but it occasionally fails on some systems

**App won't start / blank screen**
- Open a terminal in the app folder and run `npm start` to see the error output

---

## Files overview

```
VENTUS_SYSTEM/
├── SETUP.bat                     ← run this first
├── LAUNCH.bat                    ← created by SETUP, run this to start the app
├── app.py                        ← Flask backend
├── main.js                       ← Electron wrapper
├── preload.js                    ← Electron bridge
├── package.json                  ← Node config
├── get_tokens.py                 ← OAuth helper, called by SETUP
├── env.example                   ← credential template
├── master_vibe_training_set.csv  ← your taste profile
├── templates/
│   └── index.html                ← the UI
└── assets/                       ← icons
```

These files are created automatically and should not be shared:
```
.env              ← your credentials
vibe_vault.db     ← your listening data cache
node_modules/     ← dependencies
```
