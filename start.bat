@echo off
cd /d "%~dp0"

echo Stopping any processes on ports 5000 and 8000 (including child processes)...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":5000"') do taskkill /F /T /PID %%a 2>nul
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":8000"') do taskkill /F /T /PID %%a 2>nul

echo Killing any orphaned bot_runner, uvicorn, or multiprocessing-spawn python processes...
for /f "tokens=2 delims==" %%a in ('wmic process where "name like 'python%%' and commandline like '%%bot_runner%%'" get processid /value 2^>nul ^| find "="') do taskkill /F /T /PID %%a 2>nul
for /f "tokens=2 delims==" %%a in ('wmic process where "name like 'python%%' and commandline like '%%uvicorn%%'" get processid /value 2^>nul ^| find "="') do taskkill /F /T /PID %%a 2>nul
for /f "tokens=2 delims==" %%a in ('wmic process where "name like 'python%%' and commandline like '%%multiprocessing%%'" get processid /value 2^>nul ^| find "="') do taskkill /F /T /PID %%a 2>nul
if exist "backend\.bot_runner.pid" del /f /q "backend\.bot_runner.pid" 2>nul
timeout /t 2 /nobreak >nul

if exist "frontend\node_modules\.vite" (
  echo Clearing Vite cache...
  rd /s /q "frontend\node_modules\.vite" 2>nul
)

if not exist "node_modules" (
  echo Installing dependencies...
  call npm install
)

:: Create logs directory if it doesn't exist
if not exist "logs" mkdir logs

:: Generate a timestamped log filename via PowerShell
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set SESSION_TS=%%i
set LOGFILE=logs\session_%SESSION_TS%.txt

echo Starting MagiTrader (backend + frontend in one terminal)...
echo Session log: %LOGFILE%
echo Press Ctrl+C to stop both.
echo.

:: Run dev server and tee all output (stdout + stderr) to the session log file.
:: ANSI colour codes are stripped so the log is plain text and easy to grep.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$log = '%LOGFILE%';" ^
  "& { npm run dev } 2>&1 | ForEach-Object {" ^
  "    Write-Host $_;" ^
  "    ($_ -replace '\x1b\[[0-9;]*[mGKHF]', '') | Out-File -FilePath $log -Encoding UTF8 -Append" ^
  "}"
