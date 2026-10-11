# Updates the WoT 0.8.2 LAN mod in one or more game folders from GitHub.
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/MondayDecember/WoT-0.8.2-LAN/lan/step3-shooting/tools/update_mod.ps1)))
#
# Another game folder: add  -GameDir 'D:\Games\WoT'  at the end.
#
# Copies scripts and gui into res_mods\0.8.2, deletes compiled .pyc files the
# mod no longer ships (the game would load those instead of the new .py), and
# puts the launcher next to WorldOfTanks.exe. offhangar_user (your settings,
# stats, accounts) is never touched. Close the game first.
param(
    [string[]]$GameDir = @('C:\World_of_Tanks_0.08.02.00.00_RU_0349_SD', 'C:\WoT082_guest'),
    [string]$Branch = 'lan/step3-shooting'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$dirs = @($GameDir | Where-Object { Test-Path (Join-Path $_ 'WorldOfTanks.exe') })
if (-not $dirs) { Write-Host 'No game folder found (WorldOfTanks.exe). Pass -GameDir.' -ForegroundColor Red; return }

$tmp = Join-Path $env:TEMP ('wotlan_' + [guid]::NewGuid().ToString('N'))
New-Item $tmp -ItemType Directory | Out-Null
try {
    $zip = Join-Path $tmp 'mod.zip'
    Write-Host "Downloading $Branch ..."
    Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/MondayDecember/WoT-0.8.2-LAN/zip/refs/heads/$Branch" -OutFile $zip
    Expand-Archive $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $src) { throw 'archive is empty' }
    $src = $src.FullName

    foreach ($d in $dirs) {
        $exe = Join-Path $d 'WorldOfTanks.exe'
        if (Get-Process WorldOfTanks -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
            Write-Host "$d : the game is running there - close it and run this again." -ForegroundColor Yellow
            continue
        }
        $rm = Join-Path $d 'res_mods\0.8.2'
        New-Item $rm -ItemType Directory -Force | Out-Null
        Copy-Item (Join-Path $src 'scripts') $rm -Recurse -Force
        if (Test-Path (Join-Path $src 'gui')) { Copy-Item (Join-Path $src 'gui') $rm -Recurse -Force }

        # A .pyc the mod no longer ships but whose .py it does ship is stale:
        # the client prefers the .pyc and would run the old code.
        $removed = 0
        $srcMods = Join-Path $src 'scripts\client\gui\mods'
        $dstMods = Join-Path $rm 'scripts\client\gui\mods'
        Get-ChildItem $dstMods -Recurse -Filter *.pyc -ErrorAction SilentlyContinue | ForEach-Object {
            $rel = $_.FullName.Substring($dstMods.Length).TrimStart('\')
            $relPy = $rel.Substring(0, $rel.Length - 1)
            if (-not (Test-Path (Join-Path $srcMods $rel)) -and (Test-Path (Join-Path $srcMods $relPy))) {
                Remove-Item $_.FullName -Force; $removed++
            }
        }

        Copy-Item (Join-Path $src 'tools\WoT-LAN-Launcher.ps1') $d -Force
        Copy-Item (Join-Path $src 'tools\WoT LAN.cmd') $d -Force
        Copy-Item (Join-Path $src 'tools\bot_brain.py') $d -Force
        Write-Host "$d : updated (old .pyc removed: $removed)" -ForegroundColor Green
    }
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'Done. Start the game with "WoT LAN.cmd" in the game folder.'
