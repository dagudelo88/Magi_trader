@echo off
cd /d "%~dp0"

echo Stopping any processes on ports 5000 and 8000...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":5000"') do taskkill /F /PID %%a 2>nul
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":8000"') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul

if exist "frontend\node_modules\.vite" (
  echo Clearing Vite cache...
  rd /s /q "frontend\node_modules\.vite" 2>nul
)

if not exist "node_modules" (
  echo Installing dependencies...
  call npm install
)

echo Starting MagiTrader (backend + frontend in one terminal)...
echo Press Ctrl+C to stop both.
echo.
call npm run dev
