from flask import Flask, render_template, jsonify, request, Response
import requests
import pandas as pd
import sqlite3
import os
import json
import time
import sys

# fix stdout encoding on windows - must be before any print()
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
import queue
import threading
import tempfile
import statistics
from datetime import datetime

# librosa init (ghost signal local engine)
try:
    import librosa
    import numpy as np
    LIBROSA_OK = True
    print("[BOOT] librosa OK — Ghost Signal local engine ACTIVE")
except ImportError:
    LIBROSA_OK = False
    print("[BOOT] librosa NOT installed — Ghost Signal DISABLED (pip install librosa to enable)")



# resolve base path when running as pyinstaller bundle
if getattr(sys, 'frozen', False):
    _HERE = sys._MEIPASS
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))

# load credentials from .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, '.env'))
    print("[BOOT] .env loaded")
except ImportError:
    print("[BOOT] python-dotenv not installed — using fallback credentials")

app = Flask(__name__,
            template_folder=os.path.join(_HERE, 'templates'),
            static_folder=os.path.join(_HERE, 'assets'),
            static_url_path='/assets')

# credentials
CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
LFM_KEY       = os.environ.get("LFM_KEY",               "")

def get_auth_url(): return "https://accounts.s" + "potify.com/api/token"
def get_api():      return "https://api.s"       + "potify.com/v1"

# sqlite vault - WAL mode for concurrent reads, upserts via INSERT OR REPLACE
DB_FILE        = os.path.join(_HERE, 'vibe_vault.db')
MASTER_DB_FILE = os.path.join(_HERE, 'vibe_vault_master.db')

def _db():
    """Per-call connection with WAL enabled. Use as context manager."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn

def _master_db():
    """Connection to the master vault (separate DB, same schema)."""
    conn = sqlite3.connect(MASTER_DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS vault (
                id               TEXT PRIMARY KEY,
                energy           REAL DEFAULT 0,
                valence          REAL DEFAULT 0,
                danceability     REAL DEFAULT 0,
                bpm              REAL DEFAULT 0,
                acousticness     REAL DEFAULT 0,
                instrumentalness REAL DEFAULT 0,
                loudness         REAL DEFAULT 0,
                key              INTEGER DEFAULT -1,
                mode             INTEGER DEFAULT 1,
                source           TEXT    DEFAULT 'reccobeats'
            )
        """)
        c.commit()
    # One-time CSV migration
    old_csv     = os.path.join(_HERE, 'vibe_vault.csv')
    done_marker = os.path.join(_HERE, 'vibe_vault.csv.migrated')
    if os.path.exists(old_csv) and not os.path.exists(done_marker):
        try:
            df   = pd.read_csv(old_csv).fillna(0)
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    str(r.get('id','')),
                    float(r.get('energy',0) or 0),
                    float(r.get('valence',0) or 0),
                    float(r.get('danceability',0) or 0),
                    float(r.get('bpm',0) or 0),
                    float(r.get('acousticness',0) or 0),
                    float(r.get('instrumentalness',0) or 0),
                    float(r.get('loudness',0) or 0),
                    int(r.get('key',-1) or -1),
                    int(r.get('mode',1) or 1),
                    'reccobeats',
                ))
            with _db() as c:
                c.executemany("""INSERT OR IGNORE INTO vault
                    (id,energy,valence,danceability,bpm,acousticness,
                     instrumentalness,loudness,key,mode,source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
                c.commit()
            os.rename(old_csv, done_marker)
            print(f"[VAULT] Migrated {len(rows)} rows CSV→SQLite")
        except Exception as e:
            print(f"[VAULT] CSV migration failed (non-fatal): {e}")

_init_db()

def _init_master_db():
    with _master_db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS master_vault (
                id               TEXT PRIMARY KEY,
                song             TEXT DEFAULT '',
                artist           TEXT DEFAULT '',
                energy           REAL DEFAULT 0,
                valence          REAL DEFAULT 0,
                danceability     REAL DEFAULT 0,
                bpm              REAL DEFAULT 0,
                acousticness     REAL DEFAULT 0,
                instrumentalness REAL DEFAULT 0,
                loudness         REAL DEFAULT 0,
                key              INTEGER DEFAULT -1,
                mode             INTEGER DEFAULT 1,
                source           TEXT DEFAULT 'reccobeats',
                added_at         TEXT DEFAULT ''
            )
        """)
        c.commit()

_init_master_db()

def _row_to_feat(row) -> dict:
    return {
        'energy':           float(row['energy']           or 0),
        'valence':          float(row['valence']          or 0),
        'danceability':     float(row['danceability']     or 0),
        'bpm':              float(row['bpm']              or 0),
        'acousticness':     float(row['acousticness']     or 0),
        'instrumentalness': float(row['instrumentalness'] or 0),
        'loudness':         float(row['loudness']         or 0),
        'key':              int(row['key']  if row['key']  is not None else -1),
        'mode':             int(row['mode'] if row['mode'] is not None else  1),
    }

def vault_get(tid: str) -> dict | None:
    try:
        with _db() as c:
            row = c.execute("SELECT * FROM vault WHERE id=?", (tid,)).fetchone()
        return _row_to_feat(row) if row else None
    except Exception as e:
        print(f"[VAULT] get: {e}"); return None

def vault_get_many(tids: list) -> dict:
    if not tids: return {}
    try:
        ph = ",".join("?"*len(tids))
        with _db() as c:
            rows = c.execute(f"SELECT * FROM vault WHERE id IN ({ph})", tids).fetchall()
        return {r['id']: _row_to_feat(r) for r in rows}
    except Exception as e:
        print(f"[VAULT] get_many: {e}"); return {}

def vault_insert(rows: list, source: str = 'reccobeats'):
    """
    Upsert a list of feature dicts. 'id' key required.
    _source key is stripped automatically — never leaks into DB.
    """
    if not rows: return
    clean = []
    # Valid source labels — anything else defaults to 'reccobeats'
    VALID_SOURCES = {'reccobeats', 'local_engine', 'spotify_af', 'audio_analysis'}
    for r in rows:
        raw_src = r.get('_source', source)
        src = raw_src if raw_src in VALID_SOURCES else source
        clean.append((
            str(r['id']),
            float(r.get('energy',0) or 0),
            float(r.get('valence',0) or 0),
            float(r.get('danceability',0) or 0),
            float(r.get('bpm',0) or 0),
            float(r.get('acousticness',0) or 0),
            float(r.get('instrumentalness',0) or 0),
            float(r.get('loudness',0) or 0),
            int(r.get('key',-1) or -1),
            int(r.get('mode',1) or 1),
            src,
        ))
    try:
        with _db() as c:
            # Protect Ghost Signal / Spotify-AF / Audio-Analysis rows — never let reccobeats overwrite them.
            PROTECTED = {'local_engine', 'spotify_af', 'audio_analysis'}
            ghost_rows = [r for r in clean if r[10] in PROTECTED]
            rb_rows    = [r for r in clean if r[10] not in PROTECTED]

            if ghost_rows:
                c.executemany("""INSERT OR REPLACE INTO vault
                    (id,energy,valence,danceability,bpm,acousticness,
                     instrumentalness,loudness,key,mode,source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", ghost_rows)

            if rb_rows:
                # Fetch all protected IDs in a single query — not once per row
                existing_protected = {r[0] for r in c.execute(
                    f"SELECT id FROM vault WHERE source IN ({','.join(['?']*len(PROTECTED))})",
                    list(PROTECTED)).fetchall()}
                safe_rb = [r for r in rb_rows if r[0] not in existing_protected]
                if safe_rb:
                    c.executemany("""INSERT OR REPLACE INTO vault
                        (id,energy,valence,danceability,bpm,acousticness,
                         instrumentalness,loudness,key,mode,source)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", safe_rb)

            c.commit()
    except Exception as e:
        print(f"[VAULT] insert: {e}")

def vault_count() -> int:
    try:
        with _db() as c:
            return c.execute("SELECT COUNT(*) FROM vault").fetchone()[0]
    except: return 0

def vault_all_as_df() -> pd.DataFrame:
    try:
        with _db() as c:
            return pd.read_sql("SELECT * FROM vault", c)
    except: return pd.DataFrame()

# helpers
def find_col(df, keys, exclude=None):
    """
    Return the first column whose lowercased name contains any of `keys`.
    `exclude` is an optional list/set of column names to skip — prevents
    the ID column from being re-matched as a name/song/track column.
    Keys are checked in order so more-specific keys win.
    """
    excl = set(exclude or [])
    for key in keys:
        for c in df.columns:
            if c in excl:
                continue
            if key in c.lower():
                return c
    return None

def get_camelot(key, mode):
    try: k, m = int(key), int(mode)
    except: return "--"
    if k < 0 or k > 11 or m not in [0,1]: return "--"
    majors = ["8B","3B","10B","5B","12B","7B","2B","9B","4B","11B","6B","1B"]
    minors = ["5A","12A","7A","2A","9A","4A","11A","6A","1A","8A","3A","10A"]
    return majors[k] if m == 1 else minors[k]

# taste profile
def get_taste_profile():
    csv_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
    # Fresh install — no CSV yet. Return {} so the frontend detects this as
    # an empty profile and shows the first-run playlist picker overlay.
    # Returning hardcoded fallback defaults here would make vibeProfile look
    # populated and suppress the onboarding flow.
    if not os.path.exists(csv_path):
        return {}
    try:
        df = pd.read_csv(csv_path)
        # CSV exists but has no data rows (e.g. after wipe_profile) — also return {}
        # so the frontend can show first-run picker again if needed.
        if len(df) == 0:
            return {}
        date_col = find_col(df, ['added at', 'date'])
        df['dt'] = pd.to_datetime(df[date_col], errors='coerce').fillna(
            pd.Timestamp(datetime(2020,1,1))) if date_col else pd.Timestamp(datetime(2020,1,1))
        now = datetime.now()
        def w(dt):
            if hasattr(dt,'tzinfo') and dt.tzinfo: dt = dt.replace(tzinfo=None)
            d = (now - dt).days
            return 3.0 if d<=30 else 1.5 if d<=180 else 1.0 if d<=365 else 0.5
        df['weight'] = df['dt'].apply(w)
        def wm(keys, scale=False, fb=0.0):
            c = find_col(df, keys)
            if not c: return fb
            v = pd.to_numeric(df[c], errors='coerce')
            mask = v.notna()
            if not mask.any(): return fb
            tw = df.loc[mask,'weight'].sum()
            if tw == 0: return fb
            m = (v[mask]*df.loc[mask,'weight']).sum() / tw
            return round(m if not scale else (m if m>1 else m*100), 2)
        return {
            'energy':           wm(['energy'],      scale=True, fb=69.0),
            'valence':          wm(['valence'],     scale=True, fb=67.0),
            'dance':            wm(['dance'],       scale=True, fb=60.0),
            'bpm':              wm(['bpm','tempo'],            fb=120.0),
            'acousticness':     wm(['acoustic'],   scale=True, fb=10.0),
            'instrumentalness': wm(['instrument'], scale=True, fb=5.0),
            'loudness':         wm(['loud'],                   fb=-8.0),
        }
    except Exception as ex:
        print(f"[WARN] get_taste_profile: {ex}")
        # Only return fallback defaults if the CSV exists but is malformed —
        # not on FileNotFoundError (that case is handled above).
        return {'energy':69.0,'valence':67.0,'dance':60.0,'bpm':120.0,
                'acousticness':10.0,'instrumentalness':5.0,'loudness':-8.0}

# token cache - refresh at 50min to avoid hammering auth on every request
# Spotify access tokens last 3600s. We refresh at 50 min (3000s) to stay safe.
_token_cache: str | None = None
_token_expires_at: float = 0.0
_token_lock = threading.Lock()

def get_spotify_token() -> str | None:
    global _token_cache, _token_expires_at
    with _token_lock:
        if _token_cache and time.time() < _token_expires_at:
            return _token_cache
        try:
            r = requests.post(get_auth_url(), data={
                "grant_type":    "refresh_token",
                "refresh_token": REFRESH_TOKEN,
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }, timeout=10)
            token = r.json().get('access_token')
            if not token:
                print(f"[ERROR] Token failed: {r.text}")
                return None
            _token_cache      = token
            _token_expires_at = time.time() + 3000  # refresh at 50 min
            print("[TOKEN] Refreshed Spotify access token", flush=True)
            return token
        except Exception as e:
            print(f"[ERROR] Token exception: {e}")
            return None

TASTE_PROFILE = get_taste_profile()

# scoring
def score_features(data):
    p = TASTE_PROFILE
    if not p:
        return 0, "NO SIGNAL", {}
    def nl(db):      return max(0, min(100, ((db + 30) / 30) * 100))
    def nb(bpm, ref): return min(100, abs(bpm - ref) / 40.0 * 100)  # tightened from /80
    use_ac  = (data.get('acousticness', 0) or 0) >= 2.0
    use_ins = (data.get('instrumentalness', 0) or 0) >= 2.0
    de = abs((data.get('energy', 0) or 0) - (p.get('energy', 0) or 0))
    dv = abs((data.get('valence', 0) or 0) - (p.get('valence', 0) or 0))
    dd = abs((data.get('danceability', 0) or 0) - (p.get('dance', 0) or 0))
    dl = abs(nl(data.get('loudness', 0) or 0) - nl(p.get('loudness', 0) or 0))
    db = nb(data.get('bpm', 0) or 0, p.get('bpm', 0) or 0)
    da = abs((data.get('acousticness', 0) or 0) - (p.get('acousticness', 0) or 0)) if use_ac else 0
    di = abs((data.get('instrumentalness', 0) or 0) - (p.get('instrumentalness', 0) or 0)) if use_ins else 0
    axes = [(de, 1.00), (dv, 1.00), (dd, 0.85), (dl, 0.60), (db, 0.45)]
    if use_ac:  axes.append((da, 0.70))
    if use_ins: axes.append((di, 0.70))
    tw  = sum(w for _, w in axes)
    wv  = sum(d * w for d, w in axes) / tw
    scr = max(0, min(100, round(100 - (wv * 2.0))))
    if   scr >= 85: verdict = "CORE"
    elif scr >= 65: verdict = "ALIGNED"
    elif scr >= 45: verdict = "FRINGE"
    elif scr >= 25: verdict = "OUTLIER"
    else:           verdict = "NO MATCH"
    return scr, verdict, {
        "Energy": round(de, 1), "Valence": round(dv, 1), "Dance": round(dd, 1),
        "Acoustic": round(da, 1), "Instrumental": round(di, 1),
        "Loudness": round(dl, 1), "BPM": round(db, 1),
    }

# SSE - defined early so reccobeats fetch can broadcast warnings
_sse_clients = []
_sse_lock    = threading.Lock()

# failed tracks - persisted so restarts don't retry known-dead IDs
# Tracks that returned no data from all 5 tiers.
# Persisted to disk so app restarts don't re-hammer the same dead tracks.
_FAILED_TRACKS_FILE = os.path.join(_HERE, 'failed_tracks.json')
FAILED_TRACKS: set = set()

def _load_failed_tracks():
    global FAILED_TRACKS
    try:
        if os.path.exists(_FAILED_TRACKS_FILE):
            with open(_FAILED_TRACKS_FILE, 'r') as f:
                FAILED_TRACKS = set(json.load(f))
            print(f"[BOOT] Loaded {len(FAILED_TRACKS)} known-bad tracks from cache", flush=True)
    except Exception as e:
        print(f"[BOOT] failed_tracks load error (non-fatal): {e}")

def _save_failed_tracks():
    try:
        with open(_FAILED_TRACKS_FILE, 'w') as f:
            json.dump(list(FAILED_TRACKS), f)
    except Exception as e:
        print(f"[WARN] failed_tracks save: {e}")

_load_failed_tracks()

def _sse_broadcast(event_type, data):
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:    q.put_nowait(payload)
            except: dead.append(q)
        for q in dead: _sse_clients.remove(q)

# reccobeats
# ReccoBeats returns audio features for up to 40 Spotify track IDs per call.
# The response items do NOT echo back the Spotify ID directly — each item
# contains an 'href' field whose last path segment IS the Spotify track ID.
# Example href: "https://api.reccobeats.com/v1/audio-features/4uLU6hMCjMI75M1A2tKUQC"
#                                                                              ^^^^^^^^^^^^^^^^^^^^^^
# We build a lookup dict from that extracted ID back to the original tid so
# the caller always receives a {spotify_track_id -> features} mapping.
def fetch_reccobeats_batch(tids):
    if not tids: return {}
    # Hard-limit batches to 40 — API silently drops extras beyond this
    tids = tids[:40]
    try:
        res = requests.get(
            f"https://api.reccobeats.com/v1/audio-features?ids={','.join(tids)}",
            timeout=15)
        if res.status_code != 200:
            if res.status_code >= 500:
                _sse_broadcast("system_warning", {"type": "RECCOBEATS_OFFLINE",
                    "message": f"Reccobeats {res.status_code} — Deep Scans elevated. Local engine active."})
            return {}
        batch = {}
        for raw in res.json().get('content', []):
            # Primary strategy: extract Spotify ID from the href path tail
            href = raw.get('href', '')
            tid_from_href = href.rstrip('/').split('/')[-1] if href else ''
            # Fallback: some response versions include 'id' or 'spotifyId' directly
            tid_direct = raw.get('id') or raw.get('spotifyId') or raw.get('trackId') or ''
            # Pick whichever candidate actually matches one of our requested IDs
            tid = None
            if tid_from_href and tid_from_href in tids:
                tid = tid_from_href
            elif tid_direct and tid_direct in tids:
                tid = tid_direct
            if not tid:
                continue
            # All percentage-scale fields (energy, valence, etc.) come back as
            # 0.0–1.0 floats; multiply by 100 to normalise to our 0–100 scale.
            # BPM / tempo, loudness, key, and mode are already in native units.
            e   = raw.get('energy',           0) or 0
            v   = raw.get('valence',          0) or 0
            d   = raw.get('danceability',     0) or 0
            ac  = raw.get('acousticness',     0) or 0
            ins = raw.get('instrumentalness', 0) or 0
            batch[tid] = {
                'energy':           round(e   * 100 if e   <= 1.0 else e,   4),
                'valence':          round(v   * 100 if v   <= 1.0 else v,   4),
                'danceability':     round(d   * 100 if d   <= 1.0 else d,   4),
                'bpm':              round(raw.get('tempo', 0) or 0,         3),
                'acousticness':     round(ac  * 100 if ac  <= 1.0 else ac,  4),
                'instrumentalness': round(ins * 100 if ins <= 1.0 else ins, 4),
                'loudness':         round(raw.get('loudness', 0) or 0,      3),
                'key':              int(raw.get('key',  -1) if raw.get('key')  is not None else -1),
                'mode':             int(raw.get('mode',  1) if raw.get('mode') is not None else  1),
            }
        return batch
    except requests.exceptions.ConnectionError:
        _sse_broadcast("system_warning", {"type": "RECCOBEATS_OFFLINE",
            "message": "Reccobeats unreachable — local physics engine handling Deep Scans."})
        return {}
    except Exception as e:
        print(f"[WARN] Reccobeats batch: {e}"); return {}

def fetch_reccobeats(tid):
    return fetch_reccobeats_batch([tid]).get(tid)

# ghost signal - local librosa fallback
def _gs(msg:str):
    _sse_broadcast("ghost_status",{"message":msg})
    print(f"[GHOST] {msg}",flush=True)

def decrypt_ghost_signal(track_id:str, token:str) -> dict | None:
    """
    Fallback: Spotify preview URL → 30s MP3 → librosa physics analysis.
    _source key is included so vault_insert() can tag the row correctly.
    """
    _gs("METADATA MISSING — INITIATING DEEP SCAN")
    try:
        # Try market=US first — regional licensing often reveals preview_url hidden in local market
        tr = requests.get(f"{get_api()}/tracks/{track_id}?market=US",
                          headers={"Authorization":f"Bearer {token}"}, timeout=10)
        if tr.status_code != 200:
            _gs(f"TRACK LOOKUP FAILED ({tr.status_code}) — CANNOT ANALYZE"); return None
        preview_url = tr.json().get('preview_url')
        # If US market had no preview, try without market param (local market)
        if not preview_url:
            tr2 = requests.get(f"{get_api()}/tracks/{track_id}",
                               headers={"Authorization":f"Bearer {token}"}, timeout=10)
            if tr2.status_code == 200:
                preview_url = tr2.json().get('preview_url')
    except Exception as e:
        _gs(f"TRACK FETCH ERROR: {e}"); return None

    if not preview_url:
        _gs("NO PREVIEW AUDIO — AUDIO RESTRICTED — CANNOT ANALYZE"); return None

    _gs("EXTRACTING PREVIEW AUDIO STREAM...")
    audio_path = None
    try:
        ar  = requests.get(preview_url, timeout=20)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
        tmp.write(ar.content); tmp.close()
        audio_path = tmp.name
    except Exception as e:
        _gs(f"AUDIO DOWNLOAD FAILED: {e}"); return None

    try:
        if not LIBROSA_OK:
            _gs("LIBROSA NOT INSTALLED — pip install librosa — CANNOT ANALYZE"); return None
        _gs("AUDIO ACQUIRED — RUNNING LOCAL ACOUSTIC ANALYSIS...")

        y, sr = librosa.load(audio_path, sr=22050, duration=30)

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm      = float(tempo[0]) if hasattr(tempo,"__len__") else float(tempo)
        # half-tempo correction — librosa often locks onto the half-beat on energetic tracks
        onset_strength = float(np.mean(librosa.onset.onset_strength(y=y, sr=sr)))
        if bpm < 100 and onset_strength > 3.0:
            bpm *= 2
        elif bpm < 110 and onset_strength > 4.5:
            bpm *= 2

        rms    = librosa.feature.rms(y=y)
        rms_mean = float(np.mean(rms))
        rms_peak = float(np.percentile(rms, 95)) + 1e-6
        # normalize against track's own dynamic range instead of hardcoded multiplier
        energy = min(100.0, max(0.0, (rms_mean / rms_peak) * 100.0 * 1.8))

        S        = np.abs(librosa.stft(y))
        loudness = float(np.mean(librosa.amplitude_to_db(S, ref=np.max)))

        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
        valence  = min(100.0, max(0.0, (float(np.mean(centroid))-500)/35.0))

        # reuse already-computed onset_strength array; also reuse stft
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        dance     = min(100.0, max(0.0, 100-float(np.std(onset_env))*4))

        # reuse S from loudness calc above
        low          = float(np.mean(S[:S.shape[0]//4,:]))
        high         = float(np.mean(S[S.shape[0]//4:,:]))
        acousticness = min(100.0, max(0.0, (low/(high+1e-6))*25))

        chroma    = librosa.feature.chroma_cqt(y=y, sr=sr)
        key_idx   = int(np.argmax(np.mean(chroma, axis=1)))
        y_harm, y_perc = librosa.effects.hpss(y)
        harm_energy = float(np.mean(y_harm ** 2))
        perc_energy = float(np.mean(y_perc ** 2))
        # major if harmonic energy dominates, minor if percussive/tense
        mode      = 1 if harm_energy >= perc_energy * 0.8 else 0

        _gs(f"DECRYPTION COMPLETE — BPM:{bpm:.0f} NRG:{energy:.0f}% KEY:{get_camelot(key_idx,mode)}")
        return {
            'energy':           round(energy,4),
            'valence':          round(valence,4),
            'danceability':     round(dance,4),
            'bpm':              round(bpm,3),
            'acousticness':     round(acousticness,4),
            'instrumentalness': 0.0,
            'loudness':         round(loudness,3),
            'key':              key_idx,
            'mode':             mode,
            '_source':          'local_engine',  # stripped by vault_insert
        }
    except Exception as e:
        _gs(f"LOCAL ENGINE FAILED: {e}"); return None
    finally:
        if audio_path and os.path.exists(audio_path):
            try: os.remove(audio_path)
            except: pass

# spotify audio-features fallback (tier 2)
def fetch_spotify_audio_features(track_id: str, token: str) -> dict | None:
    """
    Call Spotify /audio-features/{id} directly — bypasses ReccoBeats entirely.
    Returns normalised feature dict (same schema as fetch_reccobeats) or None.
    Works for almost every track in Spotify's catalog, including niche/indie.
    """
    if not token: return None
    try:
        res = requests.get(f"{get_api()}/audio-features/{track_id}",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if res.status_code != 200:
            print(f"[SPOTIFY-AF] {track_id}: {res.status_code}"); return None
        d = res.json()
        if not d or d.get('error'): return None
        e   = d.get('energy',       0) or 0
        v   = d.get('valence',      0) or 0
        dn  = d.get('danceability', 0) or 0
        ac  = d.get('acousticness', 0) or 0
        ins = d.get('instrumentalness', 0) or 0
        return {
            'energy':           round(e   * 100 if e   <= 1.0 else e,   4),
            'valence':          round(v   * 100 if v   <= 1.0 else v,   4),
            'danceability':     round(dn  * 100 if dn  <= 1.0 else dn,  4),
            'bpm':              round(d.get('tempo', 0) or 0,           3),
            'acousticness':     round(ac  * 100 if ac  <= 1.0 else ac,  4),
            'instrumentalness': round(ins * 100 if ins <= 1.0 else ins, 4),
            'loudness':         round(d.get('loudness', 0) or 0,        3),
            'key':              int(d.get('key',  -1) if d.get('key')  is not None else -1),
            'mode':             int(d.get('mode',  1) if d.get('mode') is not None else  1),
        }
    except Exception as e:
        print(f"[SPOTIFY-AF] exception: {e}"); return None


# spotify audio-analysis derive (tier 3, last resort before librosa)
def derive_from_audio_analysis(track_id: str, token: str) -> dict | None:
    """
    Use Spotify /audio-analysis/{id} (full low-level acoustic data) to
    compute energy, BPM, loudness, valence, danceability, acousticness.
    No preview URL needed — Spotify performs this on their own masters.
    Covers virtually every track in the catalog including niche/indie/new.
    """
    if not token: return None
    try:
        res = requests.get(f"{get_api()}/audio-analysis/{track_id}",
                           headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if res.status_code != 200:
            print(f"[AUDIO-ANALYSIS] {track_id}: {res.status_code}"); return None
        d = res.json()
        if not d: return None

        # Top-level track summary
        track_summary = d.get('track', {})
        bpm      = float(track_summary.get('tempo', 0) or 0)
        loudness = float(track_summary.get('loudness', -10) or -10)
        key      = int(track_summary.get('key', -1) if track_summary.get('key') is not None else -1)
        mode     = int(track_summary.get('mode', 1) if track_summary.get('mode') is not None else 1)

        # Segments contain timbre and pitch arrays — derive energy/valence/dance from them
        segments = d.get('segments', [])
        if segments:
            # Energy: mean loudness normalised to 0-100
            seg_lounds = [s.get('loudness_max', s.get('loudness_start', -30)) for s in segments]
            mean_loud  = sum(seg_lounds) / len(seg_lounds)
            energy     = min(100.0, max(0.0, ((mean_loud + 60) / 60) * 100))

            # Valence: timbre[1] (brightness) correlates with perceived positivity
            timbres = [s.get('timbre', []) for s in segments if s.get('timbre')]
            if timbres:
                brightness = [t[1] for t in timbres if len(t) > 1]
                mean_bright = sum(brightness) / len(brightness) if brightness else 0
                valence = min(100.0, max(0.0, (mean_bright + 100) / 2.0))
            else:
                valence = 50.0

            # Danceability: derived from tempo confidence and beat regularity
            beats = d.get('beats', [])
            if len(beats) > 1:
                beat_durs = [beats[i+1]['start'] - beats[i]['start'] for i in range(len(beats)-1)]
                beat_var  = statistics.stdev(beat_durs) if len(beat_durs) > 1 else 1.0
                dance     = min(100.0, max(0.0, 100 - beat_var * 200))
            else:
                dance = 60.0

            # Acousticness: tracks with many segments at low loudness tend to be acoustic
            acousticness = min(100.0, max(0.0, max(0, -mean_loud - 5) * 3))

        else:
            # No segments — use summary-level fallbacks
            energy = min(100.0, max(0.0, ((loudness + 60) / 60) * 100))
            valence = 50.0; dance = 60.0; acousticness = 15.0

        print(f"[AUDIO-ANALYSIS] {track_id}: BPM:{bpm:.0f} NRG:{energy:.0f}% LOUD:{loudness:.1f}")
        return {
            'energy':           round(energy, 4),
            'valence':          round(valence, 4),
            'danceability':     round(dance, 4),
            'bpm':              round(bpm, 3),
            'acousticness':     round(acousticness, 4),
            'instrumentalness': 0.0,
            'loudness':         round(loudness, 3),
            'key':              key,
            'mode':             mode,
        }
    except Exception as e:
        print(f"[AUDIO-ANALYSIS] exception: {e}"); return None


# 5-tier resolver
def resolve_features(track_id: str, token: str | None = None,
                     allow_ghost: bool = True) -> tuple:
    """
    Returns (feat_dict | None, method_label, source_label).

    Resolution order:
      1. SQLite vault            — instant, no network
      2. ReccoBeats              — batch API
      3. Spotify audio-features  — direct Spotify endpoint (covers niche tracks RB misses)
      4. Ghost Signal            — librosa local analysis from preview MP3
      5. Audio-analysis derive   — compute metrics from Spotify's full track analysis
      6. void                    — nothing worked; track added to FAILED_TRACKS
    """
    # 1. Vault hit — fastest path
    feat = vault_get(track_id)
    if feat:
        return feat, "VAULT CACHE", "local"

    # 2. Skip known-bad tracks immediately — prevents infinite retry loops
    if track_id in FAILED_TRACKS:
        return None, "NO SIGNAL (CACHED FAIL)", "void"

    # 3. ReccoBeats single-track lookup
    feat = fetch_reccobeats(track_id)
    if feat:
        vault_insert([{'id': track_id, **feat}])
        return feat, "RECCOBEATS API", "api"

    # 4. Spotify audio-features direct (most complete coverage, incl. niche tracks)
    if token:
        feat = fetch_spotify_audio_features(track_id, token)
        if feat:
            vault_insert([{'id': track_id, '_source': 'spotify_af', **feat}])
            return feat, "SPOTIFY AUDIO-FEATURES", "spotify_af"

    # 5. Ghost Signal Decryption (librosa local analysis from preview audio)
    if allow_ghost and token:
        feat = decrypt_ghost_signal(track_id, token)
        if feat:
            vault_insert([{'id': track_id, **feat}])  # feat already has '_source': 'local_engine'
            return {k: v for k, v in feat.items() if k != '_source'}, "LOCAL ACOUSTIC ENGINE", "ghost_decrypted"

    # 6. Audio-analysis derive — nuclear fallback, no preview needed
    if token:
        feat = derive_from_audio_analysis(track_id, token)
        if feat:
            vault_insert([{'id': track_id, '_source': 'audio_analysis', **feat}])
            return feat, "AUDIO ANALYSIS DERIVED", "audio_analysis"

    # 7. All tiers exhausted — cache the failure to avoid future retries
    FAILED_TRACKS.add(track_id)
    _save_failed_tracks()
    return None, "NO SIGNAL", "void"

# playlist pagination
def fetch_playlist_tracks(playlist_id, token, fields=None):
    default_fields = "items(item(id,name,artists,album(images)),track(id,name,artists,album(images))),next"
    url     = f"{get_api()}/playlists/{playlist_id}/items?fields={fields or default_fields}&limit=100"
    headers = {"Authorization":f"Bearer {token}"}
    items   = []
    while url:
        last_err = None
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, timeout=12)
                if res.status_code == 200:
                    break
                last_err = f"Spotify API error: {res.status_code}"
            except requests.RequestException as e:
                last_err = str(e)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        else:
            raise ValueError(last_err or "Playlist fetch failed after 3 attempts")
        body = res.json()
        items.extend(body.get('items',[]))
        url = body.get('next')
    return items

# background vault thread
# Silently resolves features for the currently-playing track every time it
# changes. Vaults the result so it's available instantly for scoring.
_last_auto_vaulted: str | None = None

def _auto_vault_track(track_id: str, token: str):
    """Called in a daemon thread — silently vaults a track without blocking SSE."""
    global _last_auto_vaulted
    if track_id == _last_auto_vaulted:
        return
    # Check vault first — if already there, nothing to do
    if vault_get(track_id):
        _last_auto_vaulted = track_id
        return
    _last_auto_vaulted = track_id
    feat, method, _ = resolve_features(track_id, token=token, allow_ghost=True)
    if feat:
        print(f"[AUTO-VAULT] {track_id[:18]} → {method}", flush=True)
        _sse_broadcast("vault_updated", {"track_id": track_id, "method": method})


# now-playing poller
# Polls every 3 seconds — correct for a live player.
# Broadcasts a lightweight "progress" event when the same track is still playing
# so the seek bar updates smoothly without triggering a full now_playing re-eval.
def _now_playing_poller():
    last_id = None
    while True:
        try:
            token = get_spotify_token()   # uses cache — no auth hit every 3s
            if token:
                res = requests.get(f"{get_api()}/me/player/currently-playing",
                                   headers={"Authorization":f"Bearer {token}"}, timeout=6)
                if res.status_code == 200 and res.content:
                    body = res.json()
                    if body and body.get('item') and body.get('is_playing'):
                        item   = body['item']
                        cid    = item['id']
                        images = item.get('album',{}).get('images',[])
                        track_changed = (cid != last_id)

                        if track_changed:
                            # Full broadcast — new track, frontend needs all metadata
                            _sse_broadcast("now_playing", {
                                "is_playing":    True,
                                "id":            cid,
                                "name":          item['name'],
                                "artist":        item['artists'][0]['name'] if item.get('artists') else "Unknown",
                                "album":         item.get('album',{}).get('name',''),
                                "album_art":     images[0]['url'] if images else None,
                                "progress_ms":   body.get('progress_ms', 0),
                                "duration_ms":   item.get('duration_ms', 0),
                                "track_changed": True,
                            })
                            last_id = cid
                            # Silently vault every new track using all 5 resolver tiers
                            threading.Thread(target=_auto_vault_track,
                                             args=(cid, token), daemon=True).start()
                        else:
                            # Same track — send lightweight progress-only event
                            # so the seek bar stays accurate without full re-eval
                            _sse_broadcast("progress", {
                                "progress_ms": body.get('progress_ms', 0),
                                "duration_ms": item.get('duration_ms', 0),
                            })
                    else:
                        if last_id is not None:
                            _sse_broadcast("now_playing", {"is_playing": False})
                            last_id = None
        except Exception as e:
            print(f"[SSE-POLL] {e}")
        time.sleep(3)

threading.Thread(target=_now_playing_poller, daemon=True).start()

# background playlist auto-sync
_autosync_playlist_id: str | None = None
_autosync_interval:    int        = 300  # seconds (5 min default)

def _playlist_autosync_worker():
    """Background thread — re-syncs taste profile playlist periodically."""
    global _autosync_playlist_id
    while True:
        time.sleep(_autosync_interval)
        pid = _autosync_playlist_id
        if not pid:
            continue
        try:
            token = get_spotify_token()
            if not token:
                continue
            all_items = fetch_playlist_tracks(pid, token,
                fields="items(item(id,name,artists),track(id,name,artists)),next")
            master_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
            try:
                df = pd.read_csv(master_path)
            except Exception:
                continue
            id_col = find_col(df, ['spotify track id', 'track id', 'id'])
            if not id_col:
                continue
            existing = set(df[id_col].dropna().astype(str))
            # deletion sync
            playlist_ids_auto = set()
            for pl in all_items:
                t = pl.get('item') or pl.get('track')
                if t and t.get('id'):
                    playlist_ids_auto.add(t['id'])
            removed_auto = existing - playlist_ids_auto
            if removed_auto:
                df = df[~df[id_col].isin(removed_auto)]
                df.to_csv(master_path, index=False)
                with _db() as c:
                    c.executemany("DELETE FROM vault WHERE id=?", [(rid,) for rid in removed_auto])
                    c.commit()
                print(f"[AUTO-SYNC] Removed {len(removed_auto)} tracks no longer in playlist", flush=True)
                existing -= removed_auto

            new_ids = []
            track_meta = {}
            for pl in all_items:
                t = pl.get('item') or pl.get('track')
                if t and t.get('id') and t['id'] not in existing:
                    new_ids.append(t['id']); track_meta[t['id']] = t
            if new_ids:
                print(f"[AUTO-SYNC] {len(new_ids)} new tracks detected — syncing…", flush=True)
                n_col  = find_col(df, ['song','name','track'], exclude=[id_col])
                a_col  = find_col(df, ['artist'])
                dt_col = find_col(df, ['added at','date']) or 'Added At'
                e_col  = find_col(df, ['energy'])   or 'Energy'
                v_col  = find_col(df, ['valence'])  or 'Valence'
                d_col  = find_col(df, ['dance'])    or 'Danceability'
                b_col  = find_col(df, ['bpm','tempo']) or 'BPM'
                ac_col = find_col(df, ['acoustic']) or 'Acousticness'
                in_col = find_col(df, ['instrument']) or 'Instrumentalness'
                lo_col = find_col(df, ['loud'])     or 'Loudness'
                k_col  = find_col(df, ['camelot','key']) or 'Camelot'
                new_rows = []
                for i in range(0, len(new_ids), 40):
                    chunk = new_ids[i:i+40]
                    bd    = fetch_reccobeats_batch(chunk)
                    for tid in chunk:
                        meta = track_meta[tid]; feat = bd.get(tid, {})
                        rd = {id_col: tid, dt_col: datetime.now().strftime('%Y-%m-%d')}
                        if n_col: rd[n_col] = meta.get('name', '')
                        if a_col: rd[a_col] = meta['artists'][0]['name'] if meta.get('artists') else ''
                        if feat:
                            rd[e_col]  = feat.get('energy', 0)
                            rd[v_col]  = feat.get('valence', 0)
                            rd[d_col]  = feat.get('danceability', 0)
                            rd[b_col]  = feat.get('bpm', 0)
                            rd[ac_col] = feat.get('acousticness', 0)
                            rd[in_col] = feat.get('instrumentalness', 0)
                            rd[lo_col] = feat.get('loudness', 0)
                            rd[k_col]  = get_camelot(feat.get('key', -1), feat.get('mode', 1))
                        new_rows.append(rd)
                if new_rows:
                    pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True).to_csv(master_path, index=False)
                    vault_rows = [{'id': rd[id_col], **{k: v for k, v in rd.items()
                        if k not in (id_col, dt_col, n_col, a_col, k_col)}}
                        for rd in new_rows if rd.get(e_col) or rd.get(b_col)]
                    if vault_rows:
                        vault_insert(vault_rows)
                    _sse_broadcast("system_warning", {
                        "type": "PLAYLIST_UPDATED",
                        "message": f"Playlist auto-synced — {len(new_rows)} new tracks added."
                    })
                    global TASTE_PROFILE
                    TASTE_PROFILE = get_taste_profile()
        except Exception as e:
            print(f"[AUTO-SYNC] error: {e}", flush=True)

threading.Thread(target=_playlist_autosync_worker, daemon=True).start()

# routes

@app.route('/')
def home():
    p = os.path.join(_HERE,'templates','index.html')
    if os.path.exists(p):
        with open(p,'r',encoding='utf-8') as f: return f.read()
    return f"CRITICAL ERROR: index.html not found at {p}"

@app.route('/api/stream')
def sse_stream():
    cq = queue.Queue(maxsize=50)
    with _sse_lock: _sse_clients.append(cq)
    def gen():
        yield ": connected\n\n"
        try:
            while True:
                try:    yield cq.get(timeout=20)
                except queue.Empty: yield ": ping\n\n"
        except GeneratorExit: pass
        finally:
            with _sse_lock:
                try: _sse_clients.remove(cq)
                except: pass
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/vibe_profile')
def vibe_profile():
    global TASTE_PROFILE
    TASTE_PROFILE = get_taste_profile()
    return jsonify(TASTE_PROFILE)

@app.route('/api/wipe_profile', methods=['POST'])
def wipe_profile():
    """
    Clears the master_vibe_training_set.csv back to headers-only,
    resets the in-memory TASTE_PROFILE to defaults, and clears
    FAILED_TRACKS so every track gets a fresh resolution attempt.
    """
    global TASTE_PROFILE
    master_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
    try:
        # Preserve the header row; wipe all data rows
        if os.path.exists(master_path):
            try:
                df = pd.read_csv(master_path, nrows=0)   # headers only
                df.to_csv(master_path, index=False)
            except Exception:
                # If the file is malformed just write a clean skeleton
                pd.DataFrame(columns=[
                    'Spotify Track ID','Song','Artist','Added At',
                    'Energy','Valence','Danceability','BPM',
                    'Acousticness','Instrumentalness','Loudness','Camelot'
                ]).to_csv(master_path, index=False)
        else:
            pd.DataFrame(columns=[
                'Spotify Track ID','Song','Artist','Added At',
                'Energy','Valence','Danceability','BPM',
                'Acousticness','Instrumentalness','Loudness','Camelot'
            ]).to_csv(master_path, index=False)
        # Reset live profile to fallback defaults
        TASTE_PROFILE = get_taste_profile()
        # Clear failed-tracks cache — fresh slate for next sync
        FAILED_TRACKS.clear()
        _save_failed_tracks()
        return jsonify({"success": True, "message": "TASTE PROFILE WIPED — ready for new sync."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/vibe_dna')
def vibe_dna():
    try:
        with open(os.path.join(_HERE,'vibe_dna.json'),'r',encoding='utf-8') as f:
            return jsonify(json.load(f))
    except: return jsonify([])

@app.route('/api/vault_stats')
def vault_stats(): return jsonify({"cached": vault_count()})

@app.route('/api/vault_data')
def get_vault_data():
    df  = vault_all_as_df()
    out = []
    for _,r in df.iterrows():
        out.append({
            "id":str(r.get('id','')),
            # Preserve float precision — rounding kills librosa's values like 63.4 energy
            "energy":round(float(r.get('energy',0)),2),
            "valence":round(float(r.get('valence',0)),2),
            "danceability":round(float(r.get('danceability',0)),2),
            "bpm":round(float(r.get('bpm',0)),2),
            "acousticness":round(float(r.get('acousticness',0)),2),
            "instrumentalness":round(float(r.get('instrumentalness',0)),2),
            "loudness":round(float(r.get('loudness',0)),2),
            "key":int(r.get('key',-1) or -1),
            "mode":int(r.get('mode',1) or 1),
            # source field — 'local_engine' for Ghost Signal (librosa), 'reccobeats' otherwise
            "source":str(r.get('source','reccobeats') or 'reccobeats'),
        })
    return jsonify(out)

@app.route('/api/tracks')
def get_tracks():
    csv_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
    try:
        df = pd.read_csv(csv_path).fillna("")
    except FileNotFoundError:
        # First-run: no master CSV yet — return empty list, not an error
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    id_c=find_col(df,['spotify track id','track id','id'])
    n_c =find_col(df,['song','name','track'], exclude=[id_c]); a_c=find_col(df,['artist'])
    k_c =find_col(df,['camelot','key']);       e_c=find_col(df,['energy'])
    v_c =find_col(df,['valence']);             d_c=find_col(df,['dance'])
    p_c =find_col(df,['pop']);                 b_c=find_col(df,['bpm','tempo'])
    ac_c=find_col(df,['acoustic']);           in_c=find_col(df,['instrument'])
    lo_c=find_col(df,['loud'])
    def fv(r,col,scale=False):
        if not col: return 0
        try:
            v=float(r.get(col,0) or 0)
            return round(v if not scale else (v if v>1 else v*100))
        except: return 0
    out=[]
    for i,r in df.iterrows():
        out.append({
            "num":i+1,"id":str(r.get(id_c,"")) if id_c else "",
            "song":str(r.get(n_c,"")) if n_c else "","artist":str(r.get(a_c,"")) if a_c else "",
            "key":str(r.get(k_c,"--")) if k_c else "--",
            "energy":fv(r,e_c,True),"valence":fv(r,v_c,True),"dance":fv(r,d_c,True),
            "bpm":fv(r,b_c),"acousticness":fv(r,ac_c,True),"instrumentalness":fv(r,in_c,True),
            "loudness":round(float(r.get(lo_c,0) or 0)) if lo_c else 0,
            "popularity":round(float(r.get(p_c,0) or 0)) if p_c else 0,
        })
    return jsonify(out)

# score track
@app.route('/api/score_track', methods=['POST'])
def score_track():
    t_id = (request.json or {}).get('track_id')
    if not t_id: return jsonify({"error":"No track_id provided"}),400
    token = get_spotify_token()
    data, method, source = resolve_features(t_id, token=token, allow_ghost=True)
    if data is None:
        return jsonify({"error":"Deep Scan — no audio features found",
                        "verdict":"NO MATCH","score":None}),404
    score,verdict,deltas = score_features(data)
    return jsonify({
        "score":score,"verdict":verdict,"method":method,"source":source,
        "profile":TASTE_PROFILE,"camelot":get_camelot(data.get('key',-1),data.get('mode',1)),
        "features":{
            "Energy":round(data['energy']),"Valence":round(data['valence']),
            "Dance":round(data['danceability']),"BPM":round(data['bpm']),
            "Acoustic":round(data['acousticness']),"Instrumental":round(data['instrumentalness']),
            "Loudness":round(data['loudness'],1),"Key":data['key'],"Mode":data['mode'],
        },
        "deltas":deltas,
    })

# audit playlist
@app.route('/api/audit_playlist', methods=['POST'])
def audit_playlist():
    playlist_id = (request.json or {}).get('playlist_id','').strip()
    if not playlist_id: return jsonify({"error":"No playlist_id provided"}),400
    token = get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:    all_items = fetch_playlist_tracks(playlist_id, token)
    except ValueError as e: return jsonify({"error":str(e)}),400

    all_ids    = []
    track_meta = {}
    for pl in all_items:
        t = pl.get('item') or pl.get('track')
        if t and t.get('id'):
            all_ids.append(t['id']); track_meta[t['id']] = t

    # Bulk vault lookup, then batch Reccobeats for uncached
    cached = vault_get_many(all_ids)
    uncached = [tid for tid in all_ids if tid not in cached]
    if uncached:
        for i in range(0, len(uncached), 40):
            bd = fetch_reccobeats_batch(uncached[i:i+40])
            if bd:
                vault_insert([{'id':tid,**feat} for tid,feat in bd.items()])
        cached = vault_get_many(all_ids)

    results = []
    for tid in all_ids:
        t         = track_meta[tid]
        images    = t.get('album',{}).get('images',[])
        album_art = images[-1]['url'] if images else None
        name      = t['name']
        artist    = t['artists'][0]['name'] if t.get('artists') else "Unknown"
        data      = cached.get(tid)
        if data:
            sc,vd,dl = score_features(data)
            results.append({"id":tid,"name":name,"artist":artist,"album_art":album_art,
                "score":sc,"verdict":vd,"source":"vault",
                "camelot":get_camelot(data['key'],data['mode']),
                "features":{k:round(v,1) for k,v in data.items()}})
        else:
            # Deep scan fallback for tracks Reccobeats couldn't resolve
            feat = decrypt_ghost_signal(tid, token)
            if feat:
                vault_insert([{'id':tid,**feat}])
                clean = {k:v for k,v in feat.items() if k!='_source'}
                sc,vd,dl = score_features(clean)
                results.append({"id":tid,"name":name,"artist":artist,"album_art":album_art,
                    "score":sc,"verdict":vd,"source":"ghost_decrypted",
                    "camelot":get_camelot(clean['key'],clean['mode']),
                    "features":{k:round(v,1) for k,v in clean.items()}})
            else:
                results.append({"id":tid,"name":name,"artist":artist,"album_art":album_art,
                    "score":None,"verdict":"NO MATCH","source":"ghost"})
    return jsonify(results)

# sync playlist to master csv
@app.route('/api/sync_playlist', methods=['POST'])
def sync_playlist():
    playlist_id = (request.json or {}).get('playlist_id','').strip()
    if not playlist_id: return jsonify({"error":"No playlist_id provided"}),400
    token = get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:
        all_items = fetch_playlist_tracks(playlist_id,token,
            fields="items(item(id,name,artists),track(id,name,artists)),next")
    except ValueError as e: return jsonify({"error":str(e)}),400

    master_path = os.path.join(_HERE,'master_vibe_training_set.csv')
    # Gracefully handle missing or empty CSV (first-run scenario)
    try:
        df = pd.read_csv(master_path)
    except FileNotFoundError:
        # Create a minimal skeleton so the sync can proceed
        df = pd.DataFrame(columns=[
            'Spotify Track ID','Song','Artist','Added At',
            'Energy','Valence','Danceability','BPM',
            'Acousticness','Instrumentalness','Loudness','Camelot'
        ])
        df.to_csv(master_path, index=False)
    except Exception as e:
        return jsonify({"error": f"Could not read master CSV: {e}"}), 500
    id_col = find_col(df,['spotify track id','track id','id'])
    if not id_col: return jsonify({"error":"Could not find ID column"}),500

    n_col =find_col(df,['song','name','track'], exclude=[id_col]); a_col =find_col(df,['artist'])
    dt_col=find_col(df,['added at','date']) or 'Added At'
    e_col =find_col(df,['energy'])  or 'Energy';    v_col =find_col(df,['valence'])  or 'Valence'
    d_col =find_col(df,['dance'])   or 'Danceability'; b_col=find_col(df,['bpm','tempo']) or 'BPM'
    ac_col=find_col(df,['acoustic']) or 'Acousticness'; in_col=find_col(df,['instrument']) or 'Instrumentalness'
    lo_col=find_col(df,['loud'])    or 'Loudness';  k_col=find_col(df,['camelot','key']) or 'Camelot'

    existing = set(df[id_col].dropna().astype(str))
    # deletion sync — remove tracks from CSV and vault that are no longer in the playlist
    playlist_ids = set()
    for pl in all_items:
        t = pl.get('item') or pl.get('track')
        if t and t.get('id'):
            playlist_ids.add(t['id'])
    removed_ids = existing - playlist_ids
    if removed_ids:
        df = df[~df[id_col].isin(removed_ids)]
        df.to_csv(master_path, index=False)
        with _db() as c:
            c.executemany("DELETE FROM vault WHERE id=?", [(rid,) for rid in removed_ids])
            c.commit()
        print(f"[SYNC] Removed {len(removed_ids)} tracks no longer in playlist", flush=True)
        existing -= removed_ids

    to_fetch=[]; track_meta={}
    for pl in all_items:
        t = pl.get('item') or pl.get('track')
        if not t or not t.get('id') or t['id'] in existing: continue
        to_fetch.append(t['id']); track_meta[t['id']]=t; existing.add(t['id'])

    new_rows=[]
    for i in range(0,len(to_fetch),40):
        chunk = to_fetch[i:i+40]
        bd    = fetch_reccobeats_batch(chunk)
        for tid in chunk:
            meta = track_meta[tid]; feat = bd.get(tid,{})
            rd = {id_col:tid, dt_col:datetime.now().strftime('%Y-%m-%d')}
            if n_col: rd[n_col]=meta.get('name','')
            if a_col: rd[a_col]=meta['artists'][0]['name'] if meta.get('artists') else ''
            if feat:
                rd[e_col]=feat.get('energy',0);      rd[v_col]=feat.get('valence',0)
                rd[d_col]=feat.get('danceability',0); rd[b_col]=feat.get('bpm',0)
                rd[ac_col]=feat.get('acousticness',0);rd[in_col]=feat.get('instrumentalness',0)
                rd[lo_col]=feat.get('loudness',0)
                rd[k_col]=get_camelot(feat.get('key',-1),feat.get('mode',1))
            new_rows.append(rd)

    if new_rows:
        pd.concat([df,pd.DataFrame(new_rows)],ignore_index=True).to_csv(master_path,index=False)
        global TASTE_PROFILE; TASTE_PROFILE=get_taste_profile()
        # push all tracks that have metrics into the SQLite vault
        vault_rows = []
        for rd in new_rows:
            # Only insert rows where ReccoBeats actually returned features
            if rd.get(e_col) or rd.get(b_col):
                tid = rd.get(id_col,'')
                if tid:
                    vault_rows.append({
                        'id':               tid,
                        'energy':           float(rd.get(e_col,  0) or 0),
                        'valence':          float(rd.get(v_col,  0) or 0),
                        'danceability':     float(rd.get(d_col,  0) or 0),
                        'bpm':              float(rd.get(b_col,  0) or 0),
                        'acousticness':     float(rd.get(ac_col, 0) or 0),
                        'instrumentalness': float(rd.get(in_col, 0) or 0),
                        'loudness':         float(rd.get(lo_col, 0) or 0),
                    })
        if vault_rows:
            vault_insert(vault_rows)
            print(f"[SYNC] Pushed {len(vault_rows)}/{len(new_rows)} rows to vault")

        # Background-resolve any tracks ReccoBeats missed (zeros) via all fallback tiers
        ghost_ids = [rd.get(id_col,'') for rd in new_rows
                     if not (rd.get(e_col) or rd.get(b_col)) and rd.get(id_col,'')]
        if ghost_ids:
            def _bg_resolve_ghosts(ids, tok):
                global TASTE_PROFILE
                resolved = 0
                for tid in ids:
                    if vault_get(tid): continue
                    feat, method, _ = resolve_features(tid, token=tok, allow_ghost=True)
                    if feat:
                        resolved += 1
                        print(f"[SYNC-BG] {tid[:18]} resolved via {method}", flush=True)
                        sc, vd, _ = score_features(feat) if TASTE_PROFILE else (None, "NO SIGNAL", {})
                        _sse_broadcast("ghost_resolved", {
                            "id": tid,
                            "method": method,
                            "score": sc,
                            "verdict": vd,
                            "energy": round(feat.get("energy", 0), 1),
                            "valence": round(feat.get("valence", 0), 1),
                            "danceability": round(feat.get("danceability", 0), 1),
                            "bpm": round(feat.get("bpm", 0), 1),
                            "acousticness": round(feat.get("acousticness", 0), 1),
                            "instrumentalness": round(feat.get("instrumentalness", 0), 1),
                            "loudness": round(feat.get("loudness", 0), 1),
                            "key": feat.get("key", -1),
                            "mode": feat.get("mode", 1),
                            "camelot": get_camelot(feat.get("key", -1), feat.get("mode", 1)),
                        })
                print(f"[SYNC-BG] Done — {resolved}/{len(ids)} ghost tracks resolved", flush=True)
                TASTE_PROFILE = get_taste_profile()
                _sse_broadcast("vault_updated", {"msg": f"Background resolved {resolved}/{len(ids)} ghost tracks", "recalc": True})
            _bg_tok = get_spotify_token()
            threading.Thread(target=_bg_resolve_ghosts, args=(ghost_ids, _bg_tok), daemon=True).start()
            print(f"[SYNC] {len(ghost_ids)} ghost tracks queued for background resolution", flush=True)

        return jsonify({"success":True,"added":len(new_rows),
            "message":f"SYNC COMPLETE — {len(new_rows)} new track(s) added. {len(ghost_ids)} ghost tracks resolving in background."})
    return jsonify({"success":True,"added":0,"message":"SIGNAL ALIGNED — no new tracks detected."})

# sync vault from playlist
@app.route('/api/sync_vault_from_playlist', methods=['POST'])
def sync_vault_from_playlist():
    playlist_id=(request.json or {}).get('playlist_id','').strip()
    if not playlist_id: return jsonify({"error":"No playlist_id provided"}),400
    token=get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:
        all_items=fetch_playlist_tracks(playlist_id,token,
            fields="items(item(id,name,artists),track(id,name,artists)),next")
    except ValueError as e: return jsonify({"error":str(e)}),400

    all_ids=[]; 
    for pl in all_items:
        t=pl.get('item') or pl.get('track')
        if t and t.get('id'): all_ids.append(t['id'])

    cached_map = vault_get_many(all_ids)
    already    = len([tid for tid in all_ids if tid in cached_map])
    to_fetch   = [tid for tid in all_ids if tid not in cached_map]
    added,ghosted = 0,0

    for i in range(0,len(to_fetch),40):
        chunk=to_fetch[i:i+40]; bd=fetch_reccobeats_batch(chunk)
        vault_insert([{'id':tid,**feat} for tid,feat in bd.items()])
        added+=len(bd); ghosted+=len(chunk)-len(bd)

    return jsonify({"success":True,"added":added,"already_cached":already,"ghost":ghosted,
        "total_scanned":len(all_items),
        "message":f"VAULT SYNC COMPLETE — {added} new, {ghosted} deep scans, {already} already indexed."})

# now playing http fallback
@app.route('/api/now_playing')
def now_playing():
    token=get_spotify_token()
    if not token: return jsonify({"is_playing":False,"error":"auth_failed"})
    try:
        res=requests.get(f"{get_api()}/me/player/currently-playing",
                         headers={"Authorization":f"Bearer {token}"},timeout=8)
        if res.status_code==200 and res.content:
            body=res.json()
            if body and body.get('item') and body.get('is_playing'):
                item=body['item']; images=item.get('album',{}).get('images',[])
                return jsonify({"is_playing":True,"id":item['id'],"name":item['name'],
                    "artist":item['artists'][0]['name'] if item.get('artists') else "Unknown",
                    "album":item.get('album',{}).get('name',''),
                    "album_art":images[0]['url'] if images else None,
                    "progress_ms":body.get('progress_ms',0),"duration_ms":item.get('duration_ms',0)})
    except Exception as e: print(f"[WARN] now_playing: {e}")
    return jsonify({"is_playing":False})

# search
@app.route('/api/search_spotify', methods=['POST'])
def search_spotify():
    query=(request.json or {}).get('query','').strip()
    if not query: return jsonify({"error":"No query provided"}),400
    token=get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:
        res=requests.get(f"{get_api()}/search",
            params={"q":query,"type":"track","limit":8},
            headers={"Authorization":f"Bearer {token}"},timeout=10)
        if res.status_code!=200: return jsonify({"error":f"Search failed: {res.status_code}"}),400
        results=[]
        for item in res.json().get('tracks',{}).get('items',[]):
            images=item.get('album',{}).get('images',[])
            results.append({"id":item['id'],"name":item['name'],
                "artist":item['artists'][0]['name'] if item.get('artists') else "Unknown",
                "album":item.get('album',{}).get('name',''),
                "album_art":images[-1]['url'] if images else None})
        return jsonify(results)
    except Exception as e: return jsonify({"error":str(e)}),500

# queue
@app.route('/api/queue')
def get_queue():
    token=get_spotify_token()
    if not token: return jsonify({"error":"Auth failed"}),401
    res=requests.get(f"{get_api()}/me/player/queue",
                     headers={"Authorization":f"Bearer {token}"})
    if res.status_code!=200: return jsonify({"error":"No active player"}),404

    queue_data = res.json().get('queue',[])[:15]
    all_ids    = [t['id'] for t in queue_data if t.get('id')]
    cached     = vault_get_many(all_ids)

    uncached = [tid for tid in all_ids if tid not in cached]
    if uncached:
        batch = fetch_reccobeats_batch(uncached)
        if batch:
            vault_insert([{'id':tid,**feat} for tid,feat in batch.items()])
            cached.update(batch)
        # Individual retries + Ghost decryption for anything still missing
        still = [tid for tid in uncached if tid not in cached]
        for tid in still:
            feat = fetch_reccobeats(tid)
            if feat:
                vault_insert([{'id':tid,**feat}]); cached[tid]=feat
            else:
                feat = decrypt_ghost_signal(tid, token)
                if feat:
                    vault_insert([{'id':tid,**feat}])
                    cached[tid] = {k:v for k,v in feat.items() if k!='_source'}

    results=[]
    for t in queue_data:
        tid=t.get('id')
        if not tid: continue
        feat=cached.get(tid)
        score,verdict,camelot=None,"NO SIGNAL","--"
        if feat:
            score,verdict,_=score_features(feat)
            camelot=get_camelot(feat.get('key',-1),feat.get('mode',1))
        results.append({
            "id":tid,"name":t['name'],
            "artist":t['artists'][0]['name'] if t.get('artists') else "Unknown",
            "score":score,"verdict":verdict,"camelot":camelot,
            "energy": round(feat['energy'])  if (feat and feat.get('energy')  is not None) else None,
            "valence":round(feat['valence']) if (feat and feat.get('valence') is not None) else None,
        })
    return jsonify(results)

# add to playlist
SESSION_ADDED_TRACKS=set()

@app.route('/api/add_to_playlist', methods=['POST'])
def add_to_playlist():
    data=request.json or {}
    playlist_id=data.get('playlist_id'); raw=data.get('track_id')
    if not playlist_id or not raw: return jsonify({"error":"Need both playlist_id and track_id"}),400
    track_id=raw.replace('spotify:track:','').strip()
    cache_key=f"{playlist_id}:{track_id}"
    if cache_key in SESSION_ADDED_TRACKS:
        return jsonify({"error":"Track already added in this session."}),400
    token=get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:
        tr=requests.get(f"{get_api()}/tracks/{track_id}",
                        headers={"Authorization":f"Bearer {token}"},timeout=10)
        if tr.status_code!=200: return jsonify({"error":f"Could not verify track: {tr.status_code}"}),400
        td=tr.json()
        t_isrc=td.get('external_ids',{}).get('isrc')
        t_name=td.get('name','').split(' - ')[0].split(' (')[0].lower().strip()
        t_arts=td.get('artists') or []
        t_art =t_arts[0].get('name','').lower().strip() if t_arts else ''
        fields="items(item(id,name,artists,external_ids),track(id,name,artists,external_ids)),next"
        url=f"{get_api()}/playlists/{playlist_id}/items?fields={fields}&limit=100"
        is_dup=False
        while url and not is_dup:
            r=requests.get(url,headers={"Authorization":f"Bearer {token}"},timeout=10)
            if r.status_code!=200: return jsonify({"error":f"Failed to scan: {r.status_code}"}),400
            body=r.json()
            for pl in body.get('items',[]):
                t=pl.get('item') or pl.get('track')
                if not t or not t.get('id'): continue
                if t['id']==track_id: is_dup=True; break
                pi=t.get('external_ids',{}).get('isrc')
                if t_isrc and pi and t_isrc==pi: is_dup=True; break
                pn=t.get('name','').split(' - ')[0].split(' (')[0].lower().strip()
                pa=(t.get('artists') or [{}])[0].get('name','').lower().strip()
                if pn==t_name and pa==t_art: is_dup=True; break
            url=body.get('next')
        if is_dup:
            SESSION_ADDED_TRACKS.add(cache_key)
            return jsonify({"error":"Track is already in the playlist!"}),400
        ar=requests.post(f"{get_api()}/playlists/{playlist_id}/items",
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
            json={"uris":[f"spotify:track:{track_id}"]},timeout=10)
        if ar.status_code==201:
            SESSION_ADDED_TRACKS.add(cache_key)
            return jsonify({"success":True,"snapshot_id":ar.json().get('snapshot_id')})
        return jsonify({"error":f"Spotify returned {ar.status_code}"}),400
    except Exception as e: return jsonify({"error":str(e)}),500

# track info - single lookup for vault display
@app.route('/api/track_info')
def track_info():
    tid = request.args.get('id', '').strip()
    if not tid: return jsonify({"error": "No id"}), 400
    token = get_spotify_token()
    if not token: return jsonify({"error": "Auth failed"}), 401
    try:
        res = requests.get(f"{get_api()}/tracks/{tid}",
                           headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if res.status_code != 200:
            return jsonify({"error": f"Spotify {res.status_code}"}), 404
        item   = res.json()
        images = item.get('album', {}).get('images', [])
        feat   = vault_get(tid)
        # Also try market=US for preview_url (regional unlock)
        preview = item.get('preview_url')
        if not preview:
            try:
                r2 = requests.get(f"{get_api()}/tracks/{tid}?market=US",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=8)
                if r2.status_code == 200:
                    preview = r2.json().get('preview_url')
            except: pass
        return jsonify({
            "id":          tid,
            "name":        item.get('name', ''),
            "artist":      item['artists'][0]['name'] if item.get('artists') else 'Unknown',
            "album_art":   images[0]['url'] if images else None,
            "camelot":     get_camelot(feat['key'], feat['mode']) if feat else '--',
            "preview_url": preview,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/backfill_vault', methods=['POST'])
def backfill_vault():
    """
    One-shot: reads master_vibe_training_set.csv and inserts every row that
    has real audio metrics into the SQLite vault (skipping rows with all-zero
    features, i.e. the null / Ghost-blocked tracks).
    Safe to run multiple times — INSERT OR REPLACE is idempotent.
    """
    master_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
    try:
        df = pd.read_csv(master_path).fillna(0)
    except FileNotFoundError:
        return jsonify({"error": "No master CSV found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    id_col  = find_col(df, ['spotify track id','track id','id'])
    e_col   = find_col(df, ['energy'])
    v_col   = find_col(df, ['valence'])
    d_col   = find_col(df, ['dance'])
    b_col   = find_col(df, ['bpm','tempo'])
    ac_col  = find_col(df, ['acoustic'])
    in_col  = find_col(df, ['instrument'])
    lo_col  = find_col(df, ['loud'])
    k_col   = find_col(df, ['key'])
    m_col   = find_col(df, ['mode'])

    if not id_col:
        return jsonify({"error": "Cannot find ID column in CSV"}), 500

    existing = set()
    ghost_protected = set()
    try:
        with _db() as c:
            rows_db = c.execute("SELECT id, source FROM vault").fetchall()
            existing        = {r['id'] for r in rows_db}
            _PROTECTED_SOURCES = {'local_engine', 'spotify_af', 'audio_analysis'}
            ghost_protected = {r['id'] for r in rows_db if r['source'] in _PROTECTED_SOURCES}
    except: pass

    vault_rows = []
    skipped_null = 0
    already_in   = 0

    for _, r in df.iterrows():
        tid = str(r.get(id_col, '')).strip()
        if not tid or tid == 'nan': continue
        if tid in ghost_protected:
            already_in += 1; continue  # Ghost Signal data is precious — never overwrite
        if tid in existing:
            already_in += 1; continue

        e  = float(r.get(e_col,  0) or 0) if e_col  else 0
        b  = float(r.get(b_col,  0) or 0) if b_col  else 0

        # Skip rows where ReccoBeats never returned anything (all zeros = null track)
        if e == 0 and b == 0:
            skipped_null += 1; continue

        def fv(col, fb=0.0):
            if not col: return fb
            try: return float(r.get(col, fb) or fb)
            except: return fb

        vault_rows.append({
            'id':               tid,
            'energy':           e,
            'valence':          fv(v_col),
            'danceability':     fv(d_col),
            'bpm':              b,
            'acousticness':     fv(ac_col),
            'instrumentalness': fv(in_col),
            'loudness':         fv(lo_col),
            'key':              int(r.get(k_col, -1) or -1) if k_col else -1,
            'mode':             int(r.get(m_col,  1) or  1) if m_col else  1,
        })

    if vault_rows:
        vault_insert(vault_rows)

    return jsonify({
        "success":        True,
        "inserted":       len(vault_rows),
        "already_had":    already_in,
        "skipped_null":   skipped_null,
        "ghost_protected":len(ghost_protected),
        "vault_total":    vault_count(),
        "message": (f"BACKFILL COMPLETE — {len(vault_rows)} tracks pushed to vault. "
                    f"{len(ghost_protected)} Ghost Signal rows protected. "
                    f"{skipped_null} null-metric tracks skipped. "
                    f"{already_in} already cached.")
    })


@app.route('/api/rescan_ghosts', methods=['POST'])
def rescan_ghosts():
    """
    Streaming SSE endpoint.
    Reads master CSV, finds tracks with energy=0 AND bpm=0 (GHOST / no ReccoBeats data),
    checks if they have a Spotify preview URL, runs Ghost Signal (librosa) on each,
    and stores the result as local_engine in the vault.
    Streams progress events so the frontend can show a live log.
    """
    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Spotify auth failed"}), 401

    master_path = os.path.join(_HERE, 'master_vibe_training_set.csv')
    try:
        df = pd.read_csv(master_path).fillna("")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    id_col = find_col(df, ['spotify track id', 'track id', 'id'])
    e_col  = find_col(df, ['energy'])
    b_col  = find_col(df, ['bpm', 'tempo'])
    if not id_col:
        return jsonify({"error": "Cannot find ID column in CSV"}), 500

    ghost_ids = []
    for _, r in df.iterrows():
        tid = str(r.get(id_col, '')).strip()
        if not tid: continue
        try:
            e = float(r.get(e_col, 0) or 0) if e_col else 0.0
            b = float(r.get(b_col, 0) or 0) if b_col else 0.0
        except: e, b = 0.0, 0.0
        if e == 0.0 and b == 0.0:
            ghost_ids.append(tid)

    if ghost_ids:
        ph = ",".join("?" * len(ghost_ids))
        with _db() as c:
            already_gs = {r[0] for r in c.execute(
                f"SELECT id FROM vault WHERE id IN ({ph}) AND source='local_engine'",
                ghost_ids).fetchall()}
        ghost_ids = [tid for tid in ghost_ids if tid not in already_gs]

    def stream():
        total      = len(ghost_ids)
        success    = 0
        no_preview = 0
        failed     = 0

        yield f"event: progress\ndata: {json.dumps({'msg': f'GHOST RESCAN INITIATED — {total} tracks queued for analysis', 'done': 0, 'total': total})}\n\n"

        if total == 0:
            yield f"event: done\ndata: {json.dumps({'msg': 'NO GHOST TRACKS — all tracks already have data.', 'success': 0, 'no_preview': 0, 'failed': 0, 'vault_total': vault_count()})}\n\n"
            return

        for i, tid in enumerate(ghost_ids):
            feat = decrypt_ghost_signal(tid, token)
            if feat is None:
                # Tier 2: try Spotify audio-features direct
                feat = fetch_spotify_audio_features(tid, token)
                if feat:
                    feat['_source'] = 'spotify_af'  # direct Spotify AF data
                    vault_insert([{'id': tid, **feat}])
                    success += 1
                    msg = (f"[{i+1}/{total}] SPOTIFY-AF — "
                           f"BPM:{feat.get('bpm',0):.0f} NRG:{feat.get('energy',0):.0f}%")
                else:
                    # Tier 3: audio-analysis nuclear fallback
                    feat = derive_from_audio_analysis(tid, token)
                    if feat:
                        vault_insert([{'id': tid, **feat}])
                        success += 1
                        msg = (f"[{i+1}/{total}] ANALYSIS-DERIVED — "
                               f"BPM:{feat.get('bpm',0):.0f} NRG:{feat.get('energy',0):.0f}%")
                    else:
                        failed += 1
                        msg = f"[{i+1}/{total}] ALL TIERS FAILED — {tid[:18]}..."
            else:
                vault_insert([{'id': tid, **feat}])
                success += 1
                msg = (f"[{i+1}/{total}] DECRYPTED — "
                       f"BPM:{feat.get('bpm',0):.0f} NRG:{feat.get('energy',0):.0f}% "
                       f"KEY:{get_camelot(feat.get('key',-1), feat.get('mode',1))}")

            yield f"event: progress\ndata: {json.dumps({'msg': msg, 'done': i+1, 'total': total})}\n\n"

        summary = (f"RESCAN COMPLETE — {success} tracks resolved (Ghost Signal / Spotify-AF / Analysis). "
                   f"{failed} tracks truly unresolvable.")
        yield f"event: done\ndata: {json.dumps({'msg': summary, 'success': success, 'no_preview': 0, 'failed': failed, 'vault_total': vault_count()})}\n\n"

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/rebuild_dna', methods=['POST'])
def rebuild_dna():
    master_path=os.path.join(_HERE,'master_vibe_training_set.csv')
    try:    df=pd.read_csv(master_path,encoding='utf-8-sig').fillna("")
    except: df=pd.read_csv(master_path).fillna("")
    id_col=find_col(df,['spotify track id','track id','id'])
    n_col=find_col(df,['song','name','track'], exclude=[id_col]); a_col=find_col(df,['artist'])
    if not n_col or not a_col: return jsonify({"error":"Cannot find track/artist columns"}),500
    SKIP={
        'seen live','favorites','favourite','spotify','track','music','albums i own',
        'check out','awesome','good','cool','nice','best','beautiful','love',
        'heard on tv','heard on radio','sexy','hot','amazing','epic','perfect',
        'furry','fandom','meme','viral','youtube','tiktok','netflix',
        'under 2000 listeners','under 5000 listeners','2000s','1990s','1980s',
        '00s','90s','80s','70s','60s','50s',
    }
    # load existing tags so rebuild merges rather than overwrites
    existing_dna_path = os.path.join(_HERE, 'vibe_dna.json')
    try:
        with open(existing_dna_path, 'r', encoding='utf-8') as _f:
            existing_tags = json.load(_f)
        # prime the counts so existing tags start with a head-start
        tag_counts = {t: 2 for t in existing_tags if t not in SKIP}
    except Exception:
        tag_counts = {}
    for _,row in df.iterrows():
        url=(f"http://ws.audioscrobbler.com/2.0/?method=track.getTopTags"
             f"&artist={requests.utils.quote(str(row[a_col]))}"
             f"&track={requests.utils.quote(str(row[n_col]))}"
             f"&api_key={LFM_KEY}&format=json&autocorrect=1")
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=6)
                if r.status_code == 429:
                    # Last.fm rate-limited — back off and retry
                    time.sleep(3 * (attempt + 1))
                    continue
                for t in r.json().get('toptags',{}).get('tag',[])[:10]:
                    n=t['name'].lower()
                    if n not in SKIP: tag_counts[n]=tag_counts.get(n,0)+1
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(0.08)   # polite baseline — well under Last.fm's 5 req/s limit
    dna=[tag for tag,c in tag_counts.items() if c>=2]
    with open(os.path.join(_HERE,'vibe_dna.json'),'w',encoding='utf-8') as f: json.dump(dna,f)
    return jsonify({"success":True,"tags":len(dna),"dna":dna,
                    "message":f"GENRE PROFILE REBUILT — {len(dna)} genre triggers extracted."})

# export session
@app.route('/api/export_session', methods=['POST'])
def export_session():
    log=request.json or []
    if not log: return jsonify({"error":"Empty session log"}),400
    try:
        from datetime import datetime
        rows=[]
        for h in log:
            ts=h.get('time')
            try:
                readable=datetime.fromtimestamp(int(ts)/1000).strftime('%Y-%m-%d %H:%M:%S') if ts else ''
            except Exception:
                readable=str(ts) if ts else ''
            rows.append({
                'time':    readable,
                'name':    h.get('name',''),
                'artist':  h.get('artist',''),
                'score':   h.get('score',''),
                'verdict': h.get('verdict',''),
                'method':  h.get('method',''),
                'source':  h.get('source','manual'),   # 'auto' for silent-scored, 'manual' otherwise
                'id':      h.get('id',''),
            })
        return Response(pd.DataFrame(rows).to_csv(index=False),mimetype='text/csv',
                        headers={"Content-Disposition":"attachment; filename=ventus_session.csv"})
    except Exception as e: return jsonify({"error":str(e)}),500

# playback controls
@app.route('/api/playback/<action>', methods=['POST'])
def playback_control(action):
    token=get_spotify_token()
    if not token: return jsonify({"error":"Auth failed"}),401
    headers={"Authorization":f"Bearer {token}"}
    base=f"{get_api()}/me/player"
    if action=="seek":
        res=requests.put(f"{base}/seek?position_ms={(request.json or {}).get('position_ms',0)}",headers=headers)
    elif action=="volume":
        res=requests.put(f"{base}/volume?volume_percent={(request.json or {}).get('volume_percent',50)}",headers=headers)
    elif action=="shuffle":
        res=requests.put(f"{base}/shuffle?state={str((request.json or {}).get('state',False)).lower()}",headers=headers)
    elif action=="repeat":
        st=(request.json or {}).get('state','off')
        if st not in('off','context','track'): st='off'
        res=requests.put(f"{base}/repeat?state={st}",headers=headers)
    elif action=="transfer":
        res=requests.put(base,headers={**headers,"Content-Type":"application/json"},
                         json={"device_ids":[(request.json or {}).get('device_id','')],"play":True})
    elif action in("play","pause"):
        res=requests.put(f"{base}/{action}",headers=headers)
    elif action=="next":
        res=requests.post(f"{base}/next",headers=headers)
    elif action=="prev":
        res=requests.post(f"{base}/previous",headers=headers)
    else:
        return jsonify({"error":"Invalid action"}),400
    return jsonify({"success":res.status_code in[204,200]})

# devices
@app.route('/api/devices')
def get_devices():
    token=get_spotify_token()
    if not token: return jsonify([])
    try:
        res=requests.get(f"{get_api()}/me/player/devices",
                         headers={"Authorization":f"Bearer {token}"},timeout=8)
        if res.status_code!=200: return jsonify([])
        return jsonify([{"id":d['id'],"name":d['name'],"type":d['type'],
            "is_active":d['is_active'],"volume_percent":d.get('volume_percent',50)}
            for d in res.json().get('devices',[])])
    except Exception as e: print(f"[WARN] get_devices: {e}"); return jsonify([])

@app.route('/api/transfer_playback', methods=['POST'])
def transfer_playback():
    did=(request.json or {}).get('device_id','')
    if not did: return jsonify({"error":"No device_id provided"}),400
    token=get_spotify_token()
    if not token: return jsonify({"error":"Auth failed"}),401
    try:
        res=requests.put(f"{get_api()}/me/player",
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
            json={"device_ids":[did],"play":True},timeout=8)
        return jsonify({"success":res.status_code in[204,200]})
    except Exception as e: return jsonify({"error":str(e)}),500

# player state
@app.route('/api/player_state')
def player_state():
    token = get_spotify_token()
    if not token: return jsonify({'error': 'auth_failed'}), 401
    try:
        res = requests.get(f"{get_api()}/me/player",
                           headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if res.status_code == 200 and res.content:
            body = res.json()
            return jsonify({
                'shuffle_state': body.get('shuffle_state', False),
                'repeat_state':  body.get('repeat_state', 'off'),
                'volume_percent': body.get('device', {}).get('volume_percent', 50),
                'is_playing':    body.get('is_playing', False),
            })
    except Exception as e:
        print(f"[WARN] player_state: {e}")
    return jsonify({'shuffle_state': False, 'repeat_state': 'off', 'volume_percent': 50})


@app.route('/api/top')
def get_top():
    t_type=request.args.get('type','tracks')
    time_range=request.args.get('time_range','long_term')
    limit=min(int(request.args.get('limit',50)),50)
    if t_type not in('tracks','artists'): return jsonify({"error":"type must be 'tracks' or 'artists'"}),400
    if time_range not in('short_term','medium_term','long_term'): return jsonify({"error":"Invalid time_range"}),400
    token=get_spotify_token()
    if not token: return jsonify({"error":"Spotify auth failed"}),401
    try:
        res=requests.get(f"{get_api()}/me/top/{t_type}",
            params={"time_range":time_range,"limit":limit},
            headers={"Authorization":f"Bearer {token}"},timeout=10)
        if res.status_code!=200: return jsonify({"error":f"Spotify returned {res.status_code}"}),400
        items=res.json().get('items',[])
        if t_type=='tracks':
            out=[]
            for item in items:
                images=item.get('album',{}).get('images',[])
                out.append({"id":item['id'],"name":item['name'],
                    "artists":[{"name":a['name']} for a in item.get('artists',[])],
                    "album":item.get('album',{}).get('name',''),
                    "album_art":images[0]['url'] if images else None,
                    "popularity":item.get('popularity',0),"duration_ms":item.get('duration_ms',0)})
        else:
            out=[]
            for item in items:
                images=item.get('images',[])
                out.append({"id":item['id'],"name":item['name'],
                    "genres":item.get('genres',[]),"popularity":item.get('popularity',0),
                    "image":images[0]['url'] if images else None,
                    "followers": (item.get("followers") or {}).get("total", 0)})
        return jsonify(out)
    except Exception as e: return jsonify({"error":str(e)}),500

# playlist auto-sync
@app.route('/api/set_autosync', methods=['POST'])
def set_autosync():
    global _autosync_playlist_id, _autosync_interval
    data = request.json or {}
    pid = data.get('playlist_id', '').strip()
    interval = int(data.get('interval_seconds', 300))
    _autosync_playlist_id = pid if pid else None
    _autosync_interval    = max(60, interval)
    return jsonify({
        "success": True,
        "playlist_id": _autosync_playlist_id,
        "interval_seconds": _autosync_interval,
        "message": f"Auto-sync {'ENABLED for playlist ' + pid if pid else 'DISABLED'}."
    })

@app.route('/api/autosync_status')
def autosync_status():
    return jsonify({
        "enabled":          bool(_autosync_playlist_id),
        "playlist_id":      _autosync_playlist_id or '',
        "interval_seconds": _autosync_interval,
    })

# user playlists
@app.route('/api/user_playlists')
def user_playlists():
    """
    Returns the current user's Spotify playlists (owned + followed).
    Uses GET /me/playlists with pagination to fetch up to 200 playlists.
    Each item: { id, name, track_count, image_url, owner }
    """
    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Spotify auth failed"}), 401
    try:
        playlists = []
        url = f"{get_api()}/me/playlists?limit=50"
        while url and len(playlists) < 200:
            res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if res.status_code != 200:
                return jsonify({"error": f"Spotify error {res.status_code}"}), res.status_code
            body = res.json()
            for item in body.get('items', []):
                if not item:
                    continue
                images = item.get('images') or []
                playlists.append({
                    'id':          item['id'],
                    'name':        item.get('name', 'Untitled'),
                    'track_count': item.get('tracks', {}).get('total', 0),
                    'image_url':   images[0]['url'] if images else None,
                    'owner':       item.get('owner', {}).get('display_name', ''),
                })
            url = body.get('next')
        return jsonify(playlists)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# master vault routes
@app.route('/api/master_vault/list')
def master_vault_list():
    try:
        with _master_db() as c:
            rows = c.execute("SELECT id, song, artist, energy, valence, danceability, bpm, key, mode, source, added_at FROM master_vault ORDER BY rowid DESC").fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "song": r["song"], "artist": r["artist"],
                "energy": r["energy"], "valence": r["valence"],
                "danceability": r["danceability"], "bpm": r["bpm"],
                "camelot": get_camelot(r["key"], r["mode"]),
                "source": r["source"], "added_at": r["added_at"],
            })
        return jsonify({"tracks": out, "count": len(out)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/master_vault/sync', methods=['POST'])
def master_vault_sync():
    playlist_id = (request.json or {}).get('playlist_id', '').strip()
    if not playlist_id:
        return jsonify({"error": "No playlist_id provided"}), 400
    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Spotify auth failed"}), 401
    try:
        all_items = fetch_playlist_tracks(playlist_id, token,
            fields="items(item(id,name,artists),track(id,name,artists)),next")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with _master_db() as c:
        existing = set(r[0] for r in c.execute("SELECT id FROM master_vault").fetchall())

    to_fetch = []
    track_meta = {}
    for pl in all_items:
        t = pl.get('item') or pl.get('track')
        if t and t.get('id') and t['id'] not in existing:
            to_fetch.append(t['id'])
            track_meta[t['id']] = t

    added, ghosted = 0, 0
    for i in range(0, len(to_fetch), 40):
        chunk = to_fetch[i:i+40]
        bd = fetch_reccobeats_batch(chunk)
        rows = []
        for tid in chunk:
            meta = track_meta[tid]
            feat = bd.get(tid, {})
            song   = meta.get('name', '')
            artist = meta['artists'][0]['name'] if meta.get('artists') else ''
            if feat:
                rows.append((tid, song, artist,
                    float(feat.get('energy', 0)), float(feat.get('valence', 0)),
                    float(feat.get('danceability', 0)), float(feat.get('bpm', 0)),
                    float(feat.get('acousticness', 0)), float(feat.get('instrumentalness', 0)),
                    float(feat.get('loudness', 0)),
                    int(feat.get('key', -1)), int(feat.get('mode', 1)),
                    'reccobeats', datetime.now().strftime('%Y-%m-%d')))
                added += 1
            else:
                rows.append((tid, song, artist, 0, 0, 0, 0, 0, 0, 0, -1, 1,
                    'ghost', datetime.now().strftime('%Y-%m-%d')))
                ghosted += 1
        if rows:
            with _master_db() as c:
                c.executemany("""INSERT OR IGNORE INTO master_vault
                    (id,song,artist,energy,valence,danceability,bpm,
                     acousticness,instrumentalness,loudness,key,mode,source,added_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
                c.commit()

    return jsonify({"success": True, "added": added, "ghost": ghosted,
        "already": len(all_items) - len(to_fetch),
        "message": f"MASTER VAULT — {added} indexed, {ghosted} ghost, {len(all_items)-len(to_fetch)} already cached."})

@app.route('/api/master_vault/clear', methods=['POST'])
def master_vault_clear():
    try:
        with _master_db() as c:
            c.execute("DELETE FROM master_vault")
            c.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/master_vault/stats')
def master_vault_stats():
    try:
        with _master_db() as c:
            total = c.execute("SELECT COUNT(*) FROM master_vault").fetchone()[0]
            ghost = c.execute("SELECT COUNT(*) FROM master_vault WHERE source='ghost'").fetchone()[0]
        return jsonify({"total": total, "ghost": ghost, "indexed": total - ghost})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# boot
if __name__ == '__main__':
    import socket
    # Electron passes VENTUS_PORT=0 to get a free port, or a specific port.
    # Flask prints "VENTUS_PORT=<n>" to stdout so Electron can read it.
    port = int(os.environ.get("VENTUS_PORT", 0))
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
    print(f"VENTUS_PORT={port}", flush=True)
    print(f"[BOOT] VENTUS//SYS ONLINE v6 — port {port}", flush=True)
    print(f"[BOOT] TASTE PROFILE: {TASTE_PROFILE}", flush=True)
    print(f"[BOOT] Vault: {vault_count()} tracks in SQLite", flush=True)
    app.run(debug=False, port=port, threaded=True)
