@echo off
rem WoT 0.8.2 LAN launcher - keep this file and WoT-LAN-Launcher.ps1 in the game folder
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -STA -File "%~dp0WoT-LAN-Launcher.ps1"
