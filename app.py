from flask import Flask, render_template, jsonify, request, Response
import requests
import pandas as pd
import sqlite3
import os
import json
import time
import sys
import queue
import threading
import tempfile
from datetime import datetime

# ── LIBROSA (Ghost Signal local engine) ───────────────────────────────────────
try:
    import librosa
    import numpy as np
    LIBROSA_OK = True
    print("[BOOT] librosa OK — Ghost Signal local engine ACTIVE")
except ImportError:
    LIBROSA_OK = False
    print("[BOOT] librosa NOT installed — Ghost Signal DISABLED (pip install librosa to enable)")

# ── ENCODING FIX ──────────────────────────────────────────────────────────────
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ── PYINSTALLER PATH FIX ──────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _HERE = sys._MEIPASS
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))

# ── LOAD .env SECRETS ─────────────────────────────────────────────────────────
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

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
LFM_KEY       = os.environ.get("LFM_KEY",               "")

def get_auth_url(): return "https://accounts.s" + "potify.com/api/token"
def get_api():      return "https://api.s"       + "potify.com/v1"

# ══════════════════════════════════════════════════════════════════════════════
# SQLITE VAULT  —  replaces vibe_vault.csv
# WAL journal mode allows concurrent reads from SSE thread + request threads
# without locking. INSERT OR REPLACE handles upserts safely.
# ══════════════════════════════════════════════════════════════════════════════
DB_FILE = os.path.join(_HERE, 'vibe_vault.db')

def _db():
    """Per-call connection with WAL enabled. Use as context manager."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    for r in rows:
        src = r.get('_source', source)
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
            c.executemany("""INSERT OR REPLACE INTO vault
                (id,energy,valence,danceability,bpm,acousticness,
                 instrumentalness,loudness,key,mode,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", clean)
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

# ── HELPERS ───────────────────────────────────────────────────────────────────
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

# ── TASTE PROFILE WEIGHTING ────────────────────────────────────────────────────
def get_taste_profile():
    try:
        df = pd.read_csv(os.path.join(_HERE, 'master_vibe_training_set.csv'))
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
        return {'energy':69.0,'valence':67.0,'dance':60.0,'bpm':120.0,
                'acousticness':10.0,'instrumentalness':5.0,'loudness':-8.0}

def get_spotify_token():
    try:
        r = requests.post(get_auth_url(), data={
            "grant_type":    "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }, timeout=10)
        token = r.json().get('access_token')
        if not token: print(f"[ERROR] Token failed: {r.text}")
        return token
    except Exception as e:
        print(f"[ERROR] Token exception: {e}"); return None

TASTE_PROFILE = get_taste_profile()

# ── SCORING ───────────────────────────────────────────────────────────────────
def score_features(data):
    p = TASTE_PROFILE
    def nl(db):      return max(0,min(100,((db+30)/30)*100))
    def nb(bpm,ref): return min(100,abs(bpm-ref)/80.0*100)
    use_ac  = data['acousticness']     >= 2.0
    use_ins = data['instrumentalness'] >= 2.0
    de = abs(data['energy']-p['energy'])
    dv = abs(data['valence']-p['valence'])
    dd = abs(data['danceability']-p['dance'])
    dl = abs(nl(data['loudness'])-nl(p['loudness']))
    db = nb(data['bpm'],p['bpm'])
    da = abs(data['acousticness']-p['acousticness'])     if use_ac  else 0
    di = abs(data['instrumentalness']-p['instrumentalness']) if use_ins else 0
    axes = [(de,1.00),(dv,1.00),(dd,0.85),(dl,0.60),(db,0.45)]
    if use_ac:  axes.append((da,0.70))
    if use_ins: axes.append((di,0.70))
    tw  = sum(w for _,w in axes)
    wv  = sum(d*w for d,w in axes)/tw
    scr = max(0,min(100,round(100-(wv*2.0))))
    if   scr>=90: verdict="PERFECT MATCH"
    elif scr>=75: verdict="STRONG MATCH"
    elif scr>=58: verdict="ALIGNED"
    elif scr>=40: verdict="PERIPHERAL"
    elif scr>=20: verdict="DISSONANT"
    else:         verdict="NO MATCH"
    return scr, verdict, {
        "Energy":round(de,1),"Valence":round(dv,1),"Dance":round(dd,1),
        "Acoustic":round(da,1),"Instrumental":round(di,1),
        "Loudness":round(dl,1),"BPM":round(db,1),
    }

# ── SSE  (defined before Reccobeats so it can broadcast warnings) ─────────────
_sse_clients = []
_sse_lock    = threading.Lock()

# ── FAILED TRACKS CACHE ───────────────────────────────────────────────────────
# Tracks that returned no data from ReccoBeats AND failed Ghost Signal.
# Prevents infinite retry loops — once a track is known-bad, skip all API calls.
FAILED_TRACKS: set = set()

def _sse_broadcast(event_type, data):
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:    q.put_nowait(payload)
            except: dead.append(q)
        for q in dead: _sse_clients.remove(q)

# ── RECCOBEATS ────────────────────────────────────────────────────────────────
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

# ── GHOST SIGNAL DECRYPTION ───────────────────────────────────────────────────
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
        tr = requests.get(f"{get_api()}/tracks/{track_id}",
                          headers={"Authorization":f"Bearer {token}"}, timeout=10)
        if tr.status_code != 200:
            _gs(f"TRACK LOOKUP FAILED ({tr.status_code}) — CANNOT ANALYZE"); return None
        preview_url = tr.json().get('preview_url')
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

        rms    = librosa.feature.rms(y=y)
        energy = min(100.0, max(0.0, float(np.mean(rms))*350.0))

        S        = np.abs(librosa.stft(y))
        loudness = float(np.mean(librosa.amplitude_to_db(S, ref=np.max)))

        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
        valence  = min(100.0, max(0.0, (float(np.mean(centroid))-500)/35.0))

        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        dance     = min(100.0, max(0.0, 100-float(np.std(onset_env))*4))

        spec         = np.abs(librosa.stft(y))
        low          = float(np.mean(spec[:spec.shape[0]//4,:]))
        high         = float(np.mean(spec[spec.shape[0]//4:,:]))
        acousticness = min(100.0, max(0.0, (low/(high+1e-6))*25))

        chroma    = librosa.feature.chroma_cqt(y=y, sr=sr)
        key_idx   = int(np.argmax(np.mean(chroma, axis=1)))
        y_harm, _ = librosa.effects.hpss(y)
        mode      = 1 if float(np.mean(np.abs(y_harm))) > 0.005 else 0

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

# ── SHARED 4-TIER RESOLVER ────────────────────────────────────────────────────
def resolve_features(track_id: str, token: str | None = None,
                     allow_ghost: bool = True) -> tuple:
    """
    Returns (feat_dict | None, method_label, source_label).

    Resolution order:
      1. SQLite vault  — instant, no network
      2. ReccoBeats    — batch API (skipped if track is in FAILED_TRACKS)
      3. Ghost Signal  — librosa local analysis (skipped if allow_ghost=False)
      4. void          — nothing worked; track added to FAILED_TRACKS
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

    # 4. Ghost Signal Decryption (librosa local analysis from preview audio)
    if allow_ghost and token:
        feat = decrypt_ghost_signal(track_id, token)
        if feat:
            vault_insert([{'id': track_id, **feat}])
            return {k: v for k, v in feat.items() if k != '_source'}, "LOCAL ACOUSTIC ENGINE", "ghost_decrypted"

    # 5. All tiers exhausted — cache the failure to avoid future retries
    FAILED_TRACKS.add(track_id)
    return None, "NO SIGNAL", "void"

# ── PLAYLIST PAGINATION ───────────────────────────────────────────────────────
def fetch_playlist_tracks(playlist_id, token, fields=None):
    default_fields = "items(item(id,name,artists,album(images)),track(id,name,artists,album(images))),next"
    url     = f"{get_api()}/playlists/{playlist_id}/items?fields={fields or default_fields}&limit=100"
    headers = {"Authorization":f"Bearer {token}"}
    items   = []
    while url:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200: raise ValueError(f"Spotify API error: {res.status_code}")
        body = res.json()
        items.extend(body.get('items',[]))
        url = body.get('next')
    return items

# ── NOW-PLAYING POLLER ────────────────────────────────────────────────────────
def _now_playing_poller():
    last_id = None
    while True:
        try:
            token = get_spotify_token()
            if token:
                res = requests.get(f"{get_api()}/me/player/currently-playing",
                                   headers={"Authorization":f"Bearer {token}"}, timeout=6)
                if res.status_code == 200 and res.content:
                    body = res.json()
                    if body and body.get('item') and body.get('is_playing'):
                        item   = body['item']
                        cid    = item['id']
                        images = item.get('album',{}).get('images',[])
                        _sse_broadcast("now_playing",{
                            "is_playing":True,"id":cid,"name":item['name'],
                            "artist":item['artists'][0]['name'] if item.get('artists') else "Unknown",
                            "album":item.get('album',{}).get('name',''),
                            "album_art":images[0]['url'] if images else None,
                            "progress_ms":body.get('progress_ms',0),
                            "duration_ms":item.get('duration_ms',0),
                            "track_changed":(cid!=last_id),
                        })
                        last_id = cid
                    else:
                        if last_id is not None:
                            _sse_broadcast("now_playing",{"is_playing":False})
                            last_id = None
        except Exception as e: print(f"[SSE-POLL] {e}")
        time.sleep(3)

threading.Thread(target=_now_playing_poller, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

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
            "energy":round(float(r.get('energy',0))),
            "valence":round(float(r.get('valence',0))),
            "danceability":round(float(r.get('danceability',0))),
            "bpm":round(float(r.get('bpm',0))),
            "acousticness":round(float(r.get('acousticness',0))),
            "instrumentalness":round(float(r.get('instrumentalness',0))),
            "loudness":round(float(r.get('loudness',0)),1),
            "key":int(r.get('key',-1) or -1),
            "mode":int(r.get('mode',1) or 1),
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

# ── SCORE TRACK ───────────────────────────────────────────────────────────────
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

# ── AUDIT PLAYLIST — all 3 tiers per track ───────────────────────────────────
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

# ── SYNC PLAYLIST → MASTER CSV ────────────────────────────────────────────────
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
        # ── Also push all tracks that have metrics into the SQLite vault ──────
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
        return jsonify({"success":True,"added":len(new_rows),
            "message":f"SYNC COMPLETE — {len(new_rows)} new track(s) added with audio features."})
    return jsonify({"success":True,"added":0,"message":"SIGNAL ALIGNED — no new tracks detected."})

# ── SYNC VAULT FROM PLAYLIST ──────────────────────────────────────────────────
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

# ── NOW PLAYING HTTP FALLBACK ─────────────────────────────────────────────────
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

# ── SEARCH ────────────────────────────────────────────────────────────────────
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

# ── QUEUE — full 4-tier resolution ───────────────────────────────────────────
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

# ── ADD TO PLAYLIST ───────────────────────────────────────────────────────────
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

# ── TRACK INFO (single track name/artist lookup for vault display) ────────────
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
        return jsonify({
            "id":        tid,
            "name":      item.get('name', ''),
            "artist":    item['artists'][0]['name'] if item.get('artists') else 'Unknown',
            "album_art": images[0]['url'] if images else None,
            "camelot":   get_camelot(feat['key'], feat['mode']) if feat else '--',
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
    try:
        with _db() as c:
            rows_db = c.execute("SELECT id FROM vault").fetchall()
            existing = {r['id'] for r in rows_db}
    except: pass

    vault_rows = []
    skipped_null = 0
    already_in   = 0

    for _, r in df.iterrows():
        tid = str(r.get(id_col, '')).strip()
        if not tid or tid == 'nan': continue
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
        "success":      True,
        "inserted":     len(vault_rows),
        "already_had":  already_in,
        "skipped_null": skipped_null,
        "vault_total":  vault_count(),
        "message": (f"BACKFILL COMPLETE — {len(vault_rows)} tracks pushed to vault. "
                    f"{skipped_null} null-metric tracks skipped. "
                    f"{already_in} already cached.")
    })


@app.route('/api/rebuild_dna', methods=['POST'])
def rebuild_dna():
    master_path=os.path.join(_HERE,'master_vibe_training_set.csv')
    try:    df=pd.read_csv(master_path,encoding='utf-8-sig').fillna("")
    except: df=pd.read_csv(master_path).fillna("")
    id_col=find_col(df,['spotify track id','track id','id'])
    n_col=find_col(df,['song','name','track'], exclude=[id_col]); a_col=find_col(df,['artist'])
    if not n_col or not a_col: return jsonify({"error":"Cannot find track/artist columns"}),500
    SKIP={'seen live','favorites','favourite','spotify','track'}
    tag_counts={}
    for _,row in df.iterrows():
        url=(f"http://ws.audioscrobbler.com/2.0/?method=track.getTopTags"
             f"&artist={requests.utils.quote(str(row[a_col]))}"
             f"&track={requests.utils.quote(str(row[n_col]))}"
             f"&api_key={LFM_KEY}&format=json&autocorrect=1")
        try:
            for t in requests.get(url,timeout=5).json().get('toptags',{}).get('tag',[])[:10]:
                n=t['name'].lower()
                if n not in SKIP: tag_counts[n]=tag_counts.get(n,0)+1
        except: pass
        time.sleep(0.05)
    dna=[tag for tag,c in tag_counts.items() if c>=2]
    with open(os.path.join(_HERE,'vibe_dna.json'),'w',encoding='utf-8') as f: json.dump(dna,f)
    return jsonify({"success":True,"tags":len(dna),"dna":dna,
                    "message":f"GENRE PROFILE REBUILT — {len(dna)} genre triggers extracted."})

# ── EXPORT SESSION ────────────────────────────────────────────────────────────
@app.route('/api/export_session', methods=['POST'])
def export_session():
    log=request.json or []
    if not log: return jsonify({"error":"Empty session log"}),400
    try:
        return Response(pd.DataFrame(log).to_csv(index=False),mimetype='text/csv',
                        headers={"Content-Disposition":"attachment; filename=ventus_session.csv"})
    except Exception as e: return jsonify({"error":str(e)}),500

# ── PLAYBACK ──────────────────────────────────────────────────────────────────
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

# ── DEVICES ───────────────────────────────────────────────────────────────────
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

# ── PLAYER STATE (shuffle, repeat, volume) ───────────────────────────────────
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
                    "followers":item.get('followers',{}).get('total',0)})
        return jsonify(out)
    except Exception as e: return jsonify({"error":str(e)}),500

# ── BOOT ──────────────────────────────────────────────────────────────────────
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
