@echo off
:: Force the script to run in the folder it is located in
cd /d "%~dp0"

setlocal EnableDelayedExpansion
title VENTUS // SETUP
color 0B

echo.
echo  ██╗   ██╗███████╗███╗   ██╗████████╗██╗   ██╗███████╗
echo  ██║   ██║██╔════╝████╗  ██║╚══██╔══╝██║   ██║██╔════╝
echo  ██║   ██║█████╗  ██╔██╗ ██║   ██║   ██║   ██║███████╗
echo  ╚██╗ ██╔╝██╔══╝  ██║╚██╗██║   ██║   ██║   ██║╚════██║
echo   ╚████╔╝ ███████╗██║ ╚████║   ██║   ╚██████╔╝███████║
echo    ╚═══╝  ╚══════╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚══════╝
echo.
echo  // ONE-CLICK SETUP — installs all dependencies and
echo  // connects VENTUS to your Spotify account.
echo.
echo  Press any key to begin, or close this window to cancel.
pause > nul

:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [1/7] Checking Python...
echo ────────────────────────────────────────────────────────

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] Python not found. Attempting to install via winget...
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    if !errorlevel! neq 0 (
        echo.
        echo  [X] winget install failed. Please install Python 3.11+ manually:
        echo      https://www.python.org/downloads/
        echo      Check "Add Python to PATH" during install, then re-run SETUP.bat.
        pause
        exit /b 1
    )
    call refreshenv >nul 2>&1
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
    python --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo  [!] PATH not refreshed. Close this window, restart your terminal, run SETUP.bat again.
        pause
        exit /b 1
    )
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo  [OK] %%i


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [2/7] Checking Node.js...
echo ────────────────────────────────────────────────────────

node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] Node.js not found. Attempting to install via winget...
    winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
    if !errorlevel! neq 0 (
        echo  [X] winget install failed. Please install Node.js LTS from https://nodejs.org/ then re-run.
        pause
        exit /b 1
    )
    set "PATH=%PATH%;%ProgramFiles%\nodejs"
    node --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo  [!] Please restart your terminal and run SETUP.bat again after Node installs.
        pause
        exit /b 1
    )
)
for /f "tokens=*" %%i in ('node --version 2^>^&1') do echo  [OK] Node %%i
for /f "tokens=*" %%i in ('npm --version 2^>^&1') do echo  [OK] npm %%i


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo[3/7] Installing Python dependencies...
echo ────────────────────────────────────────────────────────

python -m pip install --upgrade pip --quiet
echo  Installing core packages: flask requests pandas python-dotenv...
python -m pip install flask requests pandas python-dotenv --quiet
if %errorlevel% neq 0 (
    echo  [X] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo  [OK] Core packages installed.

echo  Installing librosa (Ghost Signal Decryption engine)...
echo  [i] This pulls numpy/scipy/soundfile — may take a minute.
python -m pip install librosa --quiet
if %errorlevel% neq 0 (
    echo  [!] librosa failed — Ghost Signal Decryption will be unavailable.
    echo      Run  pip install librosa  later to enable it. VENTUS runs fine without it.
) else (
    echo  [OK] librosa installed.
)


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [4/7] Installing Node/Electron dependencies...
echo ────────────────────────────────────────────────────────

if not exist "package.json" (
    echo  [X] package.json not found.
    echo      Make sure you are running SETUP.bat from inside the VENTUS project folder.
    echo      Current folder: %CD%
    pause
    exit /b 1
)
if exist "node_modules" (
    echo  [OK] node_modules\ already present ^— skipping npm install.
    echo  [i] Delete node_modules\ and re-run SETUP.bat to force a clean reinstall.
) else (
    echo  Running npm install...
    call npm install --loglevel=error
    if !errorlevel! neq 0 (
        echo  [X] npm install failed. Check your internet connection.
        pause
        exit /b 1
    )
    echo  [OK] Node modules installed.
)


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [5/7] Creating folder structure...
echo ────────────────────────────────────────────────────────

if not exist "templates"    ( mkdir templates    && echo  [OK] Created templates\    ) else ( echo  [OK] templates\ exists )
if not exist "assets"       ( mkdir assets       && echo  [OK] Created assets\       ) else ( echo  [OK] assets\ exists    )
if not exist "assets\icons" ( mkdir assets\icons && echo  [OK] Created assets\icons\ ) else ( echo  [OK] assets\icons\ exists )

:: Move index.html → templates if sitting in root
if exist "index.html" (
    if not exist "templates\index.html" (
        copy "index.html" "templates\index.html" >nul
        echo  [OK] Copied index.html to templates\index.html
    )
)


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [6/7] Spotify OAuth setup...
echo ────────────────────────────────────────────────────────

:: Skip if .env already has a real refresh token
if exist ".env" (
    findstr /C:"SPOTIFY_REFRESH_TOKEN=your_" ".env" >nul 2>&1
    if !errorlevel! neq 0 (
        findstr /C:"SPOTIFY_REFRESH_TOKEN=" ".env" >nul 2>&1
        if !errorlevel! equ 0 (
            echo  [OK] .env already has credentials — skipping OAuth.
            goto :skip_oauth
        )
    )
)

echo.
echo  ┌─────────────────────────────────────────────────────────────────┐
echo  │  SPOTIFY DEVELOPER SETUP                                        │
echo  │                                                                 │
echo  │  You need a free Spotify Developer account to continue.         │
echo  │  If you haven't already:                                        │
echo  │                                                                 │
echo  │  1. Go to: https://developer.spotify.com/dashboard             │
echo  │  2. Click "Create app"                                          │
echo  │  3. Fill in any name/description                                │
echo  │  4. Set Redirect URI to:                                        │
echo  │       http://127.0.0.1:8888/callback                           │
echo  │     (EXACTLY this — no trailing slash)                          │
echo  │  5. Check "Web API" under APIs used                             │
echo  │  6. Save the app and copy your Client ID + Client Secret        │
echo  │                                                                 │
echo  │  Press any key once your app is created and you have the IDs.  │
echo  └─────────────────────────────────────────────────────────────────┘
pause > nul

echo.
set /p SPOTIFY_CLIENT_ID=  Enter your Spotify Client ID: 
if "!SPOTIFY_CLIENT_ID!"=="" (
    echo  [X] No Client ID entered. Run SETUP.bat again.
    pause
    exit /b 1
)

set /p SPOTIFY_CLIENT_SECRET=  Enter your Spotify Client Secret: 
if "!SPOTIFY_CLIENT_SECRET!"=="" (
    echo  [X] No Client Secret entered. Run SETUP.bat again.
    pause
    exit /b 1
)

echo.
echo  Last.fm is required for DNA Rebuild and genre features.
echo  Get a free key at: https://www.last.fm/api/account/create
echo  (takes 2 minutes - just fill in any app name)
echo.
set /p LFM_KEY=  Enter your Last.fm API key: 
if "!LFM_KEY!"=="" (
    echo  [X] Last.fm key is required. Run SETUP.bat again.
    pause
    exit /b 1
)

echo.
echo  ┌─────────────────────────────────────────────────────────────────┐
echo  │  CONNECTING TO SPOTIFY                                          │
echo  │                                                                 │
echo  │  A browser window will open to the Spotify login page.         │
echo  │  Log in and click Agree.                                        │
echo  │                                                                 │
echo  │  The page will show "// SIGNAL ACQUIRED" when done.            │
echo  │  Then come back here.                                           │
echo  └─────────────────────────────────────────────────────────────────┘
echo.
echo  Press any key to open the browser...
pause > nul

python get_tokens.py "!SPOTIFY_CLIENT_ID!" "!SPOTIFY_CLIENT_SECRET!" "!LFM_KEY!" ".env"
if %errorlevel% neq 0 (
    echo.
    echo  [X] OAuth failed — see error above.
    echo.
    echo  Common causes:
    echo    - Wrong Client ID or Secret
    echo    - Redirect URI not set to  http://127.0.0.1:8888/callback  in your Spotify app
    echo    - Port 8888 already in use (close other apps and try again)
    echo.
    echo  You can also fill .env manually — copy env.example to .env and edit it.
    pause
    exit /b 1
)
echo  [OK] Spotify tokens saved to .env

:skip_oauth


:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [7/7] Final checks...
echo ────────────────────────────────────────────────────────

:: Check .env is present and non-empty
if not exist ".env" (
    echo  [!] .env still missing — copy env.example to .env and fill it in manually.
) else (
    echo  [OK] .env present
)

:: Create LAUNCH.bat
(
    echo @echo off
    echo title VENTUS // SYS
    echo cd /d "%%~dp0"
    echo npm start
) > LAUNCH.bat
echo  [OK] Created LAUNCH.bat


echo.
echo ════════════════════════════════════════════════════════════════════
echo  SETUP COMPLETE
echo ════════════════════════════════════════════════════════════════════
echo.
echo  VENTUS is ready. Double-click LAUNCH.bat to start it any time.
echo.
echo  If you ever need to re-authorise Spotify (token expired or revoked):
echo    1. Delete .env
echo    2. Run SETUP.bat again — it will skip all the install steps
echo       and go straight to the OAuth screen.
echo.
echo  Press any key to close.
pause > nul