@echo off
setlocal enabledelayedexpansion

:: Ensure System32 is in PATH for standard Windows utilities
set "PATH=C:\Windows\System32;C:\Windows;%PATH%"

title Stop CBSE Question Generator Services

cd /d "%~dp0"

echo ========================================================================
echo    Stopping CBSE Question Generator ^& Ollama / Nimble Services
echo ========================================================================
echo.

echo [*] Terminating processes on port 3000 and 11434...
python -c "import subprocess; [subprocess.run(['C:\\Windows\\System32\\taskkill.exe', '/F', '/T', '/PID', l.strip().split()[-1]], capture_output=True) for p in [3000, 11434] for l in subprocess.check_output(r'C:\Windows\System32\netstat.exe -ano', text=True).splitlines() if f':{p}' in l and 'LISTENING' in l]" >nul 2>&1

if exist "cbse-generator\.ollama_service.pid" (
    del /f /q "cbse-generator\.ollama_service.pid" >nul 2>&1
    echo [OK] Removed stale PID lock file.
)

echo [OK] All services on ports 3000 and 11434 have been stopped.
ping 127.0.0.1 -n 2 >nul 2>&1
exit /b 0
