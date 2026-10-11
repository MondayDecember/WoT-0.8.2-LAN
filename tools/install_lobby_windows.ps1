# Installs the WoT LAN lobby/account server on this Windows PC:
#   C:\WoTLobby\lobby_server.py, data in C:\WoTLobby\wotlan_data,
#   firewall rule for UDP 27016, a scheduled task that starts it at boot.
# Run from an elevated PowerShell in the folder that has lobby_server.py.
param([string]$Dir = 'C:\WoTLobby', [int]$Port = 27016)

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw 'Python 3 not found. Install it first: winget install Python.Python.3.12' }

New-Item $Dir -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'lobby_server.py') $Dir -Force

if (-not (Get-NetFirewallRule -DisplayName 'WoT LAN lobby' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'WoT LAN lobby' -Direction Inbound -Protocol UDP -LocalPort $Port -Action Allow -Profile Any | Out-Null
}

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$Dir\lobby_server.py`" --port $Port --data `"$Dir\wotlan_data`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'WoT LAN lobby' -Action $action -Trigger $trigger -Settings $set -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName 'WoT LAN lobby'
Start-Sleep 2
"Lobby installed in $Dir and started (task 'WoT LAN lobby'). Forward UDP $Port on the router to this PC."
