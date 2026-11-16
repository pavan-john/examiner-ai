@echo off
setlocal enabledelayedexpansion

:: Ensure System32 is in PATH for standard Windows utilities
set "PATH=C:\Windows\System32;C:\Windows;%PATH%"

title CBSE Question Generator ^& Whole-Chapter Orchestration System

:: Navigate to project root directory
cd /d "%~dp0"

if "%1"=="--stop" goto do_stop
if "%1"=="stop" goto do_stop

cls
echo ========================================================================
echo    CBSE Question Generator ^& Whole-Chapter Topic Weight Orchestrator
echo ========================================================================
echo.

:: 1. Check Python
echo [*] Checking Python environment...
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python was not found in PATH!
    echo Please install Python 3.10 or newer from https://python.org
    echo and ensure "Add Python to PATH" is checked during installation.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
echo     Found: %PY_VER%

:: 2. Check Node.js
echo [*] Checking Node.js environment...
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Node.js was not found in PATH!
    echo Please install Node.js 18 or newer from https://nodejs.org
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do set "NODE_VER=%%v"
echo     Found Node.js: %NODE_VER%

:: 3. Check npm dependencies in cbse-generator
if not exist "cbse-generator\node_modules" (
    echo [*] Installing Node.js dependencies in cbse-generator...
    cd cbse-generator
    call npm install
    cd ..
    echo     Dependencies installed.
)

:: 4. Start Ollama / Nimble Mock Service (Port 11434)
echo [*] Checking Ollama / Nimble Service on port 11434...
python -c "import socket, sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', 11434)) == 0 else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo     [ALREADY RUNNING] Ollama / Nimble Service on port 11434 is active.
) else (
    echo     Starting Ollama / Nimble Service on port 11434...
    start "Ollama & Nimble Service [11434]" cmd /c "set PATH=C:\Windows\System32;C:\Windows;%PATH% && title Ollama & Nimble Service (Port 11434) && python ollama_mock_service.py run"
    python -c "import time; time.sleep(1.2)" >nul 2>&1
)

:: 5. Start CBSE Question Generator Node.js Server (Port 3000)
echo [*] Checking CBSE Question Generator Server on port 3000...
python -c "import socket, sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', 3000)) == 0 else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo     [ALREADY RUNNING] CBSE Generator Server on port 3000 is active.
) else (
    echo     Starting CBSE Generator Web Server on port 3000...
    start "CBSE Generator Web Server [3000]" cmd /c "set PATH=C:\Windows\System32;C:\Windows;%PATH% && title CBSE Generator Server (Port 3000) && cd cbse-generator && node server.js"
    python -c "import time; time.sleep(1.5)" >nul 2>&1
)

:: 6. Verify Service Availability
echo [*] Verifying services...
python -c "import socket, sys; ok1 = (socket.socket().connect_ex(('127.0.0.1', 11434)) == 0); ok2 = (socket.socket().connect_ex(('127.0.0.1', 3000)) == 0); sys.exit(0 if (ok1 and ok2) else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo     [SUCCESS] All services are up and responding!
) else (
    echo     [NOTICE] Services are initializing in the background.
)

:: 7. Launch Browser (unless --no-browser is passed)
if not "%1"=="--no-browser" (
    echo [*] Opening Web Interface in your default browser...
    start "" "http://localhost:3000"
)

:menu_loop
echo.
echo ========================================================================
echo    ACTIVE ENDPOINTS ^& DASHBOARD
echo ========================================================================
echo    [1] Web Interface:           http://localhost:3000
echo    [2] Ollama / Nimble API:     http://localhost:11434
echo    [3] Systemone Decision API:  http://localhost:11434/v1/systemone
echo    [4] API Status / Model Check http://localhost:3000/api/status
echo ========================================================================
echo.
echo    QUICK ACTIONS:
echo    [1] Open Web Interface in Browser (http://localhost:3000)
echo    [2] Run Weight Orchestrator CLI (Fully Dynamic Auto Mode)
echo    [3] Run Weight Orchestrator CLI (Manual 25 Questions)
echo    [4] Run Full Test Suite (pytest + Node integration tests)
echo    [5] Generate PDF Chapter Report (Solutions Chapter)
echo    [6] Stop All Services ^& Exit
echo.
set /p "CHOICE=Enter choice [1-6] (or Q to quit): "

if "%CHOICE%"=="1" (
    start "" "http://localhost:3000"
    goto menu_loop
)
if "%CHOICE%"=="2" (
    echo.
    echo [*] Running Chapter Orchestration in Fully Dynamic Auto Mode...
    python orchestrator.py
    echo.
    pause
    goto menu_loop
)
if "%CHOICE%"=="3" (
    echo.
    echo [*] Running Chapter Orchestration with Manual 25 Questions...
    python orchestrator.py --target-questions 25
    echo.
    pause
    goto menu_loop
)
if "%CHOICE%"=="4" (
    echo.
    echo [*] Running Pytest Unit ^& Integration Tests...
    python -m pytest cbse-generator/tests/test_orchestrator.py -v
    echo.
    echo [*] Running Node.js API Integration Tests...
    node cbse-generator/test_api.js
    echo.
    pause
    goto menu_loop
)
if "%CHOICE%"=="5" (
    echo.
    echo [*] Generating 16-page PDF Chapter Report...
    python generate_pdf_report.py
    echo.
    pause
    goto menu_loop
)
if "%CHOICE%"=="6" goto do_stop
if /i "%CHOICE%"=="q" goto do_stop
if /i "%CHOICE%"=="exit" goto do_stop

goto menu_loop

:do_stop
echo.
echo [*] Stopping background services on port 3000 and 11434...
python -c "import subprocess; [subprocess.run(['C:\\Windows\\System32\\taskkill.exe', '/F', '/T', '/PID', l.strip().split()[-1]], capture_output=True) for p in [3000, 11434] for l in subprocess.check_output(r'C:\Windows\System32\netstat.exe -ano', text=True).splitlines() if f':{p}' in l and 'LISTENING' in l]" >nul 2>&1
if exist "cbse-generator\.ollama_service.pid" del /f /q "cbse-generator\.ollama_service.pid" >nul 2>&1
echo [OK] All services have been stopped.
exit /b 0
