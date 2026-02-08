@echo off
:: =============================================================================
:: TikTok Controller - Windows Launcher
:: =============================================================================
:: This wrapper ensures the window NEVER closes automatically.
:: It calls the main script and keeps the window open no matter what.
:: =============================================================================

:: Keep window open by running the real script via cmd /k
:: If you double-click this, the window will ALWAYS stay open
cmd /k "%~dp0_start.bat"
