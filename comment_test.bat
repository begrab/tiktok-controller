@echo off
:: =============================================================================
:: Comment-flow test loop -- opens the comment sheet on several videos and
:: reports (via vision) whether it lands on a TOP-LEVEL comment or a REPLY.
:: It NEVER posts anything. Usage:  comment_test.bat        (6 rounds)
::                                  comment_test.bat 10      (10 rounds)
:: The iMouse console must be running and TikTok open on the For You feed.
:: =============================================================================
setlocal
set "HERE=%~dp0"
set "BACKEND=%HERE%backend"
set "VENV_PYTHON=%BACKEND%\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [ERROR] venv not found. Run run.bat once first.
    goto :end
)
cd /d "%BACKEND%"
echo [INFO] Comment-flow test (no comments are posted)...
"%VENV_PYTHON%" comment_test.py %1
:end
echo.
pause
