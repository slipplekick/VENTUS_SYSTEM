const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const AutoLaunch = require('auto-launch');
const path  = require('path');
const http  = require('http');
const fs    = require('fs');
const { spawn, exec } = require('child_process');

// ── CONFIG ────────────────────────────────────────────────────────────────────
// PYTHON_DEV: override with env var so other devs don't need to edit this file.
// e.g. set PYTHON_PATH=C:\Python311\python.exe in your shell before npm start
const PYTHON_DEV = process.env.PYTHON_PATH ||
    (process.platform === 'win32' ? 'python' : 'python3');

let mainWindow = null;
let tray       = null;
let flaskProc  = null;
let FLASK_PORT = null;   // determined dynamically at runtime
let FLASK_URL  = null;

// ── FLASK ─────────────────────────────────────────────────────────────────────
function startFlask() {
    return new Promise((resolve, reject) => {
        let exe, args, cwd;

        if (app.isPackaged) {
            exe  = path.join(process.resourcesPath, 'python-dist', 'ventus-flask.exe');
            args = [];
            cwd  = path.join(process.resourcesPath, 'python-dist');
        } else {
            exe  = PYTHON_DEV;
            args = [path.join(__dirname, 'app.py')];
            cwd  = __dirname;
        }

        console.log(`[Flask] Spawning: ${exe} ${args.join(' ')}`);

        flaskProc = spawn(exe, args, {
            cwd,
            // VENTUS_PORT=0 tells app.py to pick a free port and print it
            env: { ...process.env, VENTUS_ROOT: __dirname, VENTUS_PORT: '0' },
            stdio: ['ignore', 'pipe', 'pipe'],
            windowsHide: true,
        });

        let portFound = false;

        flaskProc.stdout.on('data', d => {
            const text = d.toString();
            process.stdout.write('[Flask] ' + text);

            // Parse the port from "VENTUS_PORT=<n>" printed by app.py on boot
            if (!portFound) {
                const match = text.match(/VENTUS_PORT=(\d+)/);
                if (match) {
                    FLASK_PORT = parseInt(match[1], 10);
                    FLASK_URL  = `http://127.0.0.1:${FLASK_PORT}`;
                    portFound  = true;
                    console.log(`[Flask] Port negotiated: ${FLASK_PORT}`);
                    resolve(FLASK_PORT);
                }
            }
        });

        flaskProc.stderr.on('data', d => process.stdout.write('[Flask ERR] ' + d));
        flaskProc.on('exit', code => {
            console.log(`[Flask] exited ${code}`);
            if (!portFound) reject(new Error(`Flask exited (${code}) before printing port`));
        });

        // Hard timeout: if port not found within 15s something went wrong
        setTimeout(() => {
            if (!portFound) reject(new Error('Flask never printed VENTUS_PORT — check app.py'));
        }, 15000);
    });
}

function waitForFlask(retries = 40, interval = 500) {
    return new Promise((resolve, reject) => {
        let n = 0;
        const check = () => {
            http.get(`${FLASK_URL}/api/vault_stats`, r => {
                if (r.statusCode === 200) { console.log('[Flask] Ready ✓'); resolve(); }
                else retry();
            }).on('error', retry);
        };
        const retry = () => (++n >= retries
            ? reject(new Error('Flask timed out — check app.py for errors'))
            : setTimeout(check, interval));
        check();
    });
}

// ── KILL FLASK (cross-platform) ───────────────────────────────────────────────
function killFlask() {
    if (!flaskProc) return;
    console.log('[Flask] Killing Flask process...');
    try { flaskProc.kill('SIGKILL'); } catch (_) {}

    if (FLASK_PORT) {
        if (process.platform === 'win32') {
            // Windows: taskkill by port via netstat
            exec(
                `for /f "tokens=5" %a in ('netstat -aon ^| find ":${FLASK_PORT}"') do taskkill /F /PID %a`,
                () => {}
            );
        } else {
            // Mac / Linux: lsof equivalent
            exec(`lsof -ti tcp:${FLASK_PORT} | xargs kill -9`, () => {});
        }
    }
}

// ── TRAY ICON ─────────────────────────────────────────────────────────────────
function getTrayIcon() {
    const p = path.join(__dirname, 'assets', 'tray-icon.png');
    if (fs.existsSync(p)) {
        const img = nativeImage.createFromPath(p);
        if (!img.isEmpty()) return img;
    }
    const fallback = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAI0lEQVQ4T2NkYGD4z8BAgFFSgFHSAKOkAUZJA4ySBkY8AABG7AABkc3ingAAAABJRU5ErkJggg==';
    return nativeImage.createFromDataURL(fallback);
}

// ── WINDOW ────────────────────────────────────────────────────────────────────
function createWindow() {
    mainWindow = new BrowserWindow({
        width:           1920,
        height:          1080,
        minWidth:        1280,
        minHeight:       720,
        frame:           false,
        transparent:     false,
        backgroundColor: '#080d14',
        icon:            path.join(__dirname, 'assets', 'icon.png'),
        webPreferences: {
            preload:          path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration:  false,
            devTools:         true,
        },
        show: false,
    });

    mainWindow.loadURL(FLASK_URL);
    mainWindow.once('ready-to-show', () => mainWindow.show());

    // F12 — toggle DevTools
    mainWindow.webContents.on('before-input-event', (e, input) => {
        if (input.key === 'F12' && input.type === 'keyDown') {
            mainWindow.webContents.isDevToolsOpened()
                ? mainWindow.webContents.closeDevTools()
                : mainWindow.webContents.openDevTools({ mode: 'detach' });
        }
    });

    mainWindow.on('close', e => {
        if (!app.isQuiting) { e.preventDefault(); mainWindow.hide(); }
    });
    mainWindow.on('closed', () => { mainWindow = null; });

    mainWindow.webContents.on('will-navigate', (e, url) => {
        if (!url.startsWith(FLASK_URL)) { e.preventDefault(); shell.openExternal(url); }
    });
}

// ── TRAY ──────────────────────────────────────────────────────────────────────
function createTray() {
    tray = new Tray(getTrayIcon());
    tray.setToolTip('VENTUS//SYS');

    const buildMenu = (startupOn) => Menu.buildFromTemplate([
        { label: 'VENTUS//SYS', enabled: false },
        { type:  'separator' },
        { label: 'Open', click: () => mainWindow ? mainWindow.show() : createWindow() },
        { label: 'Hide', click: () => mainWindow && mainWindow.hide() },
        { type: 'separator' },
        {
            label: `Boot on startup: ${startupOn ? 'ON ✓' : 'OFF'}`,
            click: async () => {
                const al = getAutoLaunch();
                if (startupOn) { await al.disable(); tray.setContextMenu(buildMenu(false)); }
                else           { await al.enable();  tray.setContextMenu(buildMenu(true));  }
            }
        },
        { type: 'separator' },
        { label: 'Quit VENTUS', click: () => { app.isQuiting = true; app.quit(); } },
    ]);

    getAutoLaunch().isEnabled()
        .then(on => tray.setContextMenu(buildMenu(on)))
        .catch(()  => tray.setContextMenu(buildMenu(false)));

    tray.on('double-click', () => {
        if (mainWindow) mainWindow.show(); else createWindow();
    });
}

// ── AUTO-LAUNCH ───────────────────────────────────────────────────────────────
function getAutoLaunch() {
    return new AutoLaunch({ name: 'VENTUS-SYS', path: app.getPath('exe') });
}

// ── IPC — window controls ─────────────────────────────────────────────────────
ipcMain.on('window-minimize', () => mainWindow && mainWindow.minimize());
ipcMain.on('window-maximize', () => {
    if (!mainWindow) return;
    mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
});
ipcMain.on('window-close', () => mainWindow && mainWindow.hide());

ipcMain.handle('startup-get',     ()      => getAutoLaunch().isEnabled());
ipcMain.handle('startup-enable',  async () => { await getAutoLaunch().enable();  return true;  });
ipcMain.handle('startup-disable', async () => { await getAutoLaunch().disable(); return false; });

// ── BOOT ──────────────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
    createTray();

    try {
        await startFlask();          // resolves once VENTUS_PORT is printed
        await waitForFlask();        // polls /api/vault_stats until 200
    } catch (e) {
        console.error('[Boot] Flask error:', e.message);
        // Show window anyway so user sees something
    }

    createWindow();
});

app.on('window-all-closed', e => e.preventDefault());  // stay in tray
app.on('activate', () => { if (!mainWindow) createWindow(); });
app.on('before-quit', () => { app.isQuiting = true; killFlask(); });
