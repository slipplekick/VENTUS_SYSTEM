# VENTUS//SYS

Spotify-connected dashboard that scores tracks against your own taste profile. This doesn't suggest tracks, it just tells you how well something fits *your* profile before you hear it.

It scores on energy, valence, danceability, BPM, key and a few other axes, all weighted against a profile built from your own playlists. If a track has no data anywhere, it downloads the 30s preview and runs local audio analysis on it (librosa). Everything gets cached in a local SQLite vault that gets faster the more you use it. There's also a Last.fm integration for genre fingerprinting if you want it.

Runs as an Electron app in the system tray on Windows. Flask backend, no cloud, no accounts beyond Spotify and optionally Last.fm.


## Requirements

Windows 10 or 11. A free Spotify account (Premium isn't needed for scoring, only if you want playback controls). A Spotify Developer app (free, takes about 5 minutes to set up). A Last.fm API key is optional — only needed for the genre fingerprint feature.

SETUP.bat handles Python and Node.js installation automatically if you don't have them.


## Setup

Download the repo as a ZIP, extract it somewhere, and run SETUP.bat. It walks you through everything — installs dependencies, asks for your credentials, opens a browser for Spotify auth, and writes the .env file. When it prints SETUP COMPLETE you're done. After that just double-click LAUNCH.bat whenever you want to open it.

The one thing that trips people up: when you create the Spotify Developer app, the redirect URI needs to be exactly `http://127.0.0.1:8888/callback` — not localhost, not with a trailing slash, exactly that string. Spotify's dashboard sometimes suggests localhost which won't work.

For the Spotify Developer app itself: go to developer.spotify.com/dashboard, create an app with any name, add that redirect URI under settings, check Web API, save it, and copy the Client ID and Secret into SETUP when it asks.

If you want the Last.fm genre features, grab a free API key at last.fm/api/account/create and paste it when SETUP asks. You can skip it and add it later.


## First run

Play something on Spotify before opening the dashboard — VENTUS needs an active session to show Now Playing data. The vault starts empty and fills up as you use the app. Go to the DNA panel and hit Rebuild after syncing a playlist to get the genre fingerprint working.


## Re-auth

If your token expires or you want to switch Spotify accounts, delete the .env file and run SETUP.bat again. It skips all the install steps and goes straight to the login flow.


## Troubleshooting

If nothing shows in Now Playing, make sure Spotify is actually playing (not paused) and that you're logged into the same account you used during setup.

If SETUP fails on the OAuth step, double-check your Client ID and Secret and make sure the redirect URI in your Spotify Developer app is exactly `http://127.0.0.1:8888/callback`. Also check that nothing else is using port 8888.

If Ghost Signal says LIBROSA NOT INSTALLED, run `pip install librosa` in a terminal. SETUP tries to install it but it fails on some systems. Everything else works fine without it — librosa is only used for the local audio fallback on tracks with no data anywhere.

If the app won't start or shows a blank screen, open a terminal in the app folder and run `npm start` to see what's actually failing.


## Files

SETUP.bat and LAUNCH.bat are the two you interact with directly. app.py is the Flask backend, main.js is the Electron wrapper, preload.js is the bridge between them. get_tokens.py handles OAuth and is called by SETUP. master_vibe_training_set.csv is your taste profile and grows as you sync playlists. The templates folder holds index.html.

Three files are created automatically and shouldn't be committed: .env holds your credentials, vibe_vault.db is the local track cache, and node_modules is dependencies.
