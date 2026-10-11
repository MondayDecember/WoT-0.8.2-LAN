@echo off
rem WoT 0.8.2 LAN lobby server - needs Python 3 (https://www.python.org/). Forward UDP 27016 to this PC.
cd /d "%~dp0"
python lobby_server.py --port 27016
pause
