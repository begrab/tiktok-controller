@echo off
:: =============================================================================
:: TikTok Controller - Self-Verifying Button Test
:: =============================================================================
:: Runs the automated button test loop against the connected phone. It taps
:: each button on many feed videos and grades itself (heart/bookmark colour
:: flips, comment/share sheet opens), then writes backend\selftest_report.json.
::
:: Usage:  selftest.bat            (15 rounds, all buttons)
::         selftest.bat 25         (25 rounds)
::         selftest.bat 20 like,comment
::
:: The iMouse console must be running and the phone on the TikTok For You feed.
:: Leave TikTok open; the test swipes through videos on its own.
:: =============================================================================
setlocal
set "HERE=%~dp0"
set "BACKEND=%HERE%backend"
set "VENV_PYTHON=%BACKEND%\venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [ERROR] venv not found. Start the app once with run.bat first so
    echo         dependencies ^(including pillow^) get installed, then retry.
    goto :end
)

:: Make sure pillow is present (added after older installs).
"%VENV_PYTHON%" -c "import PIL" 2>nul || "%BACKEND%\venv\Scripts\pip.exe" install -q pillow

cd /d "%BACKEND%"
echo [INFO] Starting self-test on the connected phone...
"%VENV_PYTHON%" selftest.py %1 %2

:end
echo.
pause
