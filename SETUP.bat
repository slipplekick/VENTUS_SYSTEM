@echo off
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
echo  // ONE-CLICK SETUP
echo  // This window will guide you through everything.
echo  // You do not need to know how to code.
echo.
echo  Press any key to start -- or close this window to cancel.
pause > nul
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 1 -- PYTHON
:: ─────────────────────────────────────────────────────────────────────────────
echo [1/7] Checking for Python...
echo ────────────────────────────────────────────────────────

python --version >nul 2>&1
if %errorlevel% equ 0 goto :python_ok

echo  Python was not found on your computer.
echo  Opening the Python download page in your browser now...
echo.
echo  IMPORTANT -- on the installer, tick the box that says:
echo    "Add Python to PATH"
echo  Then click Install Now.
echo.
start https://www.python.org/downloads/
echo  Press any key once Python is installed and you have restarted your PC.
pause > nul

python --version >nul 2>&1
if %errorlevel% equ 0 goto :python_ok

echo.
echo  Python still not found. Make sure you ticked "Add Python to PATH"
echo  and restarted your PC after installing.
echo  Close this window, restart, and run SETUP.bat again.
pause
exit /b 1

:python_ok
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo  [OK] %%i
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 2 -- NODE.JS
:: ─────────────────────────────────────────────────────────────────────────────
echo [2/7] Checking for Node.js...
echo ────────────────────────────────────────────────────────

node --version >nul 2>&1
if %errorlevel% equ 0 goto :node_ok

echo  Node.js was not found on your computer.
echo  Opening the Node.js download page in your browser now...
echo.
echo  Download the LTS version (the left button on the page).
echo  Run the installer and click Next through everything -- defaults are fine.
echo.
start https://nodejs.org/
echo  Press any key once Node.js is installed and you have restarted your PC.
pause > nul

node --version >nul 2>&1
if %errorlevel% equ 0 goto :node_ok

echo.
echo  Node.js still not found.
echo  Make sure you restarted your PC after installing.
echo  Close this window, restart, and run SETUP.bat again.
pause
exit /b 1

:node_ok
for /f "tokens=*" %%i in ('node --version 2^>^&1') do echo  [OK] Node %%i
for /f "tokens=*" %%i in ('npm --version  2^>^&1') do echo  [OK] npm  %%i
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 3 -- PYTHON PACKAGES
:: ─────────────────────────────────────────────────────────────────────────────
echo [3/7] Installing Python packages...
echo ────────────────────────────────────────────────────────
echo  This may take a few minutes. Do not close this window.
echo.

python -m pip install --upgrade pip setuptools wheel --quiet --no-warn-script-location
echo  [OK] pip ready.

python -m pip install flask requests pandas python-dotenv --quiet --no-warn-script-location
if %errorlevel% neq 0 goto :pip_core_fail
echo  [OK] Core packages installed.

echo  Installing audio analysis engine -- this one is bigger, may take 2-3 minutes...
python -m pip install librosa --no-warn-script-location
if %errorlevel% neq 0 goto :librosa_fail
echo  [OK] Audio engine installed.
goto :step4

:pip_core_fail
echo.
echo  Failed to install core packages.
echo  Check your internet connection and run SETUP.bat again.
pause
exit /b 1

:librosa_fail
echo.
echo  The audio analysis engine could not be installed.
echo  VENTUS will still work -- Spotify will cover most tracks.
echo  Ghost Signal local decryption will be unavailable.
echo  You can run  pip install librosa  manually later to enable it.
echo.

:step4
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 4 -- NODE MODULES
:: ─────────────────────────────────────────────────────────────────────────────
echo [4/7] Installing app components...
echo ────────────────────────────────────────────────────────

if not exist "package.json" goto :no_package_json
if exist "node_modules" goto :node_modules_ok

echo  Downloading app components -- this can take a few minutes.
echo  Do not close this window.
call npm install --loglevel=error
if %errorlevel% neq 0 goto :npm_fail
echo  [OK] App components installed.
goto :step5

:node_modules_ok
echo  [OK] App components already present -- skipping.
goto :step5

:no_package_json
echo.
echo  Something is wrong with your VENTUS folder.
echo  The file package.json is missing.
echo  Try re-downloading VENTUS and running SETUP.bat from inside that folder.
pause
exit /b 1

:npm_fail
echo.
echo  Failed to download app components.
echo  Check your internet connection and run SETUP.bat again.
pause
exit /b 1

:step5
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 5 -- FOLDER STRUCTURE
:: ─────────────────────────────────────────────────────────────────────────────
echo [5/7] Setting up folders...
echo ────────────────────────────────────────────────────────

if not exist "templates"    mkdir templates
if not exist "assets"       mkdir assets
if not exist "assets\icons" mkdir assets\icons

if exist "index.html" (
    if not exist "templates\index.html" (
        copy "index.html" "templates\index.html" >nul
    )
)

echo  [OK] Folders ready.
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 6 -- SPOTIFY CREDENTIALS
:: ─────────────────────────────────────────────────────────────────────────────
echo [6/7] Connecting to Spotify...
echo ────────────────────────────────────────────────────────

if not exist ".env" goto :run_oauth

findstr /C:"SPOTIFY_REFRESH_TOKEN=your_" ".env" >nul 2>&1
if %errorlevel% equ 0 goto :run_oauth

findstr /C:"SPOTIFY_REFRESH_TOKEN=" ".env" >nul 2>&1
if %errorlevel% equ 0 goto :oauth_done

:run_oauth
echo.
echo  To connect VENTUS to Spotify, you need a free developer key.
echo  This takes about 5 minutes and is completely free.
echo.
echo  -- PART A: Create a Spotify app --
echo.
echo  1. A browser window is opening now...
start https://developer.spotify.com/dashboard
echo  2. Log in with your Spotify account.
echo  3. Click "Create app".
echo  4. Fill in any name and description (e.g. VENTUS).
echo  5. In the Redirect URI box, type EXACTLY:
echo.
echo       http://127.0.0.1:8888/callback
echo.
echo     (copy-paste that -- no spaces, no slash at the end)
echo  6. Tick "Web API" under APIs used, then click Save.
echo  7. Open your new app and click Settings to see your Client ID and Secret.
echo.
echo  Press any key once your Spotify app is ready.
pause > nul
echo.

echo  -- PART B: Enter your details --
echo  (Copy-paste from the Spotify dashboard -- do not type from memory)
echo.

set SPOTIFY_CLIENT_ID=
set SPOTIFY_CLIENT_SECRET=
set LFM_KEY=

set /p SPOTIFY_CLIENT_ID=  Spotify Client ID: 
if "!SPOTIFY_CLIENT_ID!"=="" goto :missing_id

set /p SPOTIFY_CLIENT_SECRET=  Spotify Client Secret: 
if "!SPOTIFY_CLIENT_SECRET!"=="" goto :missing_secret

echo.
echo  Last.fm is used for genre and mood data. Get a free key at:
echo  https://www.last.fm/api/account/create
echo  (takes 2 minutes -- fill in any app name)
echo.
start https://www.last.fm/api/account/create
set /p LFM_KEY=  Last.fm API key: 
if "!LFM_KEY!"=="" goto :missing_lfm

echo.
echo  -- PART C: Log in to Spotify --
echo.
echo  A browser window will open. Log in and click Agree.
echo  When the page says "SIGNAL ACQUIRED", come back to this window.
echo.
echo  Press any key to open the browser...
pause > nul

python get_tokens.py "!SPOTIFY_CLIENT_ID!" "!SPOTIFY_CLIENT_SECRET!" "!LFM_KEY!" ".env"
if %errorlevel% neq 0 goto :oauth_fail

echo  [OK] Spotify connected successfully.
goto :oauth_done

:missing_id
echo  No Client ID entered. Run SETUP.bat again.
pause
exit /b 1

:missing_secret
echo  No Client Secret entered. Run SETUP.bat again.
pause
exit /b 1

:missing_lfm
echo  No Last.fm key entered. Run SETUP.bat again.
pause
exit /b 1

:oauth_fail
echo.
echo  Spotify login failed. Most common reasons:
echo.
echo    - Wrong Client ID or Secret (copy-paste directly, no extra spaces)
echo    - Redirect URI was not set to exactly:
echo        http://127.0.0.1:8888/callback
echo    - Another program is using port 8888 (close other apps and try again)
echo.
echo  Run SETUP.bat again -- it will skip steps 1-5 and go straight to this step.
pause
exit /b 1

:oauth_done
echo  [OK] Spotify credentials found.
echo.



:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 7 -- FINISH
:: ─────────────────────────────────────────────────────────────────────────────
echo [7/7] Finishing up...
echo ────────────────────────────────────────────────────────

echo @echo off                > LAUNCH.bat
echo title VENTUS // SYS     >> LAUNCH.bat
echo cd /d "%%~dp0"          >> LAUNCH.bat
echo npm start               >> LAUNCH.bat

echo  [OK] LAUNCH.bat created.
echo.



echo ════════════════════════════════════════════════════════════════════
echo  SETUP COMPLETE
echo ════════════════════════════════════════════════════════════════════
echo.
echo  VENTUS is ready. Double-click LAUNCH.bat any time to start it.
echo.
echo  If Spotify stops working later (token expired):
echo    1. Delete the file named  .env  in this folder
echo    2. Run SETUP.bat again -- it will skip straight to the login step
echo.
echo  Press any key to close this window.
pause > nul
