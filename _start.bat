@echo off
setlocal EnableDelayedExpansion
title TikTok Controller
color 0A

:: =============================================================================
:: TikTok Controller - Main Startup Script
:: =============================================================================

echo.
echo ==============================================================
echo   TikTok Controller
echo ==============================================================
echo.

:: -----------------------------------------------------------------------------
:: Step 1: Determine paths
:: -----------------------------------------------------------------------------
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash for cleaner paths
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "BACKEND_DIR=%SCRIPT_DIR%\backend"
set "VENV_DIR=%BACKEND_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"
set "REQUIREMENTS=%BACKEND_DIR%\requirements.txt"

echo [INFO] Script directory: %SCRIPT_DIR%
echo [INFO] Backend directory: %BACKEND_DIR%
echo [INFO] Venv Python: %VENV_PYTHON%
echo.

:: -----------------------------------------------------------------------------
:: Step 2: Verify backend directory exists
:: -----------------------------------------------------------------------------
if not exist "%BACKEND_DIR%" (
    echo [ERROR] Backend directory not found!
    echo         Expected: %BACKEND_DIR%
    echo.
    echo         Make sure you extracted the zip file correctly.
    goto :end
)

echo [OK] Backend directory exists
echo.

:: -----------------------------------------------------------------------------
:: Step 3: Check if venv exists, create if needed
:: -----------------------------------------------------------------------------
if exist "%VENV_PYTHON%" (
    echo [OK] Virtual environment found
) else (
    echo [INFO] Virtual environment not found. Creating...
    echo.

    :: Find Python to create venv
    set "PYTHON_CMD="

    :: Method 1: py launcher (most reliable)
    py --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=py -3"
        echo [OK] Found: py launcher
        goto :create_venv
    )

    :: Method 2: python in PATH
    python --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
        echo [OK] Found: python in PATH
        goto :create_venv
    )

    :: Method 3: Common install locations
    for %%P in (
        "C:\Python310\python.exe"
        "C:\Python311\python.exe"
        "C:\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) do (
        if exist %%P (
            set "PYTHON_CMD=%%P"
            echo [OK] Found: %%P
            goto :create_venv
        )
    )

    echo [ERROR] Python not found!
    echo.
    echo         Please install Python 3.10 or newer from:
    echo         https://www.python.org/downloads/
    echo.
    echo         IMPORTANT: Check "Add Python to PATH" during installation!
    echo.
    goto :end

:create_venv
    echo [INFO] Creating virtual environment with: !PYTHON_CMD!
    echo.
    !PYTHON_CMD! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment!
        goto :end
    )
    echo [OK] Virtual environment created
    echo.
)

:: -----------------------------------------------------------------------------
:: Step 4: Verify venv Python exists
:: -----------------------------------------------------------------------------
if not exist "%VENV_PYTHON%" (
    echo [ERROR] Virtual environment Python not found!
    echo         Expected: %VENV_PYTHON%
    echo.
    echo         Try deleting the 'backend\venv' folder and run again.
    goto :end
)

:: Show Python version
echo [INFO] Python version:
"%VENV_PYTHON%" --version
echo.

:: -----------------------------------------------------------------------------
:: Step 5: Install dependencies
:: -----------------------------------------------------------------------------
if not exist "%REQUIREMENTS%" (
    echo [ERROR] requirements.txt not found!
    echo         Expected: %REQUIREMENTS%
    goto :end
)

echo [INFO] Installing dependencies (this may take a minute)...
"%VENV_PIP%" install --disable-pip-version-check -q -r "%REQUIREMENTS%"
if errorlevel 1 (
    echo [WARNING] Some dependencies may have failed. Trying verbose install...
    "%VENV_PIP%" install -r "%REQUIREMENTS%"
)
echo [OK] Dependencies installed
echo.

:: -----------------------------------------------------------------------------
:: Step 6: Verify uvicorn is available
:: -----------------------------------------------------------------------------
echo [INFO] Verifying uvicorn...
"%VENV_PYTHON%" -c "import uvicorn; print(f'uvicorn {uvicorn.__version__}')"
if errorlevel 1 (
    echo [ERROR] uvicorn not installed correctly!
    echo         Attempting manual install...
    "%VENV_PIP%" install uvicorn[standard]
    if errorlevel 1 (
        echo [ERROR] Could not install uvicorn!
        goto :end
    )
)
echo.

:: -----------------------------------------------------------------------------
:: Step 7: Verify FastAPI app can be imported
:: -----------------------------------------------------------------------------
echo [INFO] Verifying FastAPI app...
cd /d "%BACKEND_DIR%"
"%VENV_PYTHON%" -c "from main import app; print('FastAPI app loaded successfully')"
if errorlevel 1 (
    echo [ERROR] Failed to load FastAPI app!
    echo         Check backend\main.py for errors.
    echo.
    echo [DEBUG] Showing import error:
    "%VENV_PYTHON%" -c "from main import app"
    goto :end
)
echo.

:: -----------------------------------------------------------------------------
:: Step 8: Show configuration
:: -----------------------------------------------------------------------------
echo [INFO] Configuration:
"%VENV_PYTHON%" -c "from config import IMOUSE_HOST, IMOUSE_PORT; print(f'  iMouse: {IMOUSE_HOST}:{IMOUSE_PORT}')"
echo.

:: -----------------------------------------------------------------------------
:: Step 9: Start the server
:: -----------------------------------------------------------------------------
echo ==============================================================
echo   STARTING SERVER
echo ==============================================================
echo.
echo   Web Interface:  http://localhost:8000
echo   API Docs:       http://localhost:8000/docs
echo.
echo   Press Ctrl+C to stop the server.
echo.
echo ==============================================================
echo.

:: Change to backend directory and start uvicorn
cd /d "%BACKEND_DIR%"
"%VENV_PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000

:: If uvicorn exits (error or Ctrl+C), we get here
echo.
echo [INFO] Server stopped.

:end
echo.
echo ==============================================================
echo   Press any key to close this window...
echo ==============================================================
pause >nul
