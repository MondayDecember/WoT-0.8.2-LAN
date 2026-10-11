# Starts two WoT 0.8.2 clients on one PC for LAN testing.
# Run from an elevated PowerShell (closing another process's handle needs admin).
param(
    [string]$HostDir  = "C:\World_of_Tanks_0.08.02.00.00_RU_0349_SD",
    [string]$GuestDir = "C:\WoT082_guest",
    [string]$Handle   = "C:\Games\handle\handle64.exe"
)

if (-not (Test-Path $Handle)) { throw "handle64.exe not found: $Handle" }

Get-Process WorldOfTanks -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2

Write-Host "Starting host..."
$p = Start-Process "$HostDir\WorldOfTanks.exe" -WorkingDirectory $HostDir -PassThru

# wait until the client has created its single-instance mutex
$line = $null
for ($i = 0; $i -lt 60 -and -not $line; $i++) {
    Start-Sleep 1
    $line = & $Handle -accepteula -nobanner -a -p $p.Id 2>$null | Select-String "wot_client_mutex"
}
if (-not $line) { throw "wot_client_mutex not found in host process" }

$hex = ($line.Line.Trim() -split ':')[0].Trim()
& $Handle -nobanner -c $hex -p $p.Id -y | Out-Null
Write-Host "Mutex $hex closed, starting guest..."

Start-Process "$GuestDir\WorldOfTanks.exe" -WorkingDirectory $GuestDir | Out-Null
Write-Host "Done. Status files:"
Write-Host "  $HostDir\offhangar_user\lanlink.txt"
Write-Host "  $GuestDir\offhangar_user\lanlink.txt"
