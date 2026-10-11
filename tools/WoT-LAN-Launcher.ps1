# WoT 0.8.2 LAN launcher: pick host or guest, set nickname / host address /
# session key, start the game. Lives in the game folder next to
# WorldOfTanks.exe; started by "WoT LAN.cmd".
# Windows PowerShell 5.1 + WinForms. This file must be saved as UTF-8 with BOM.

param([string]$GameDir = $PSScriptRoot)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$exe      = Join-Path $GameDir 'WorldOfTanks.exe'
$userDir  = Join-Path $GameDir 'offhangar_user'
$cfgPath  = Join-Path $userDir 'lan.json'
$statPath = Join-Path $userDir 'lanlink.txt'
$statsDb  = Join-Path $userDir 'lan_stats.json'

if (-not (Test-Path $exe)) {
    [System.Windows.Forms.MessageBox]::Show("WorldOfTanks.exe не найден в`n$GameDir`n`nПоложите лаунчер в папку игры.", 'WoT LAN', 'OK', 'Error') | Out-Null
    exit 1
}
if (-not (Test-Path $userDir)) { New-Item $userDir -ItemType Directory -Force | Out-Null }

function Read-Cfg {
    $c = $null
    if (Test-Path $cfgPath) {
        try { $c = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $c = $null }
    }
    if ($null -eq $c) { $c = New-Object PSObject }
    foreach ($kv in @(@('role','host'), @('host_ip','127.0.0.1'), @('port',27015), @('name','Player'), @('key',''), @('lobby',''), @('room',''))) {
        if (-not ($c.PSObject.Properties.Name -contains $kv[0])) {
            $c | Add-Member -NotePropertyName $kv[0] -NotePropertyValue $kv[1] -Force
        }
    }
    return $c
}

function Save-Cfg($c) {
    # keeps every other key (uid!) as it was
    $json = $c | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$cfg = Read-Cfg

# ---------------------------------------------------------------- window
$form = New-Object System.Windows.Forms.Form
$form.Text = 'World of Tanks 0.8.2 - LAN'
$form.Size = New-Object System.Drawing.Size(520, 560)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$form.Controls.Add($tabs)

$tPlay  = New-Object System.Windows.Forms.TabPage; $tPlay.Text  = 'Игра'
$tStats = New-Object System.Windows.Forms.TabPage; $tStats.Text = 'Статистика'
$tabs.TabPages.Add($tPlay); $tabs.TabPages.Add($tStats)

function Add-Label($parent, $text, $x, $y, $w = 120) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object System.Drawing.Point($x, $y); $l.Size = New-Object System.Drawing.Size($w, 24)
    $parent.Controls.Add($l); return $l
}
function Add-Box($parent, $text, $x, $y, $w = 300) {
    $b = New-Object System.Windows.Forms.TextBox
    $b.Text = "$text"; $b.Location = New-Object System.Drawing.Point($x, $y); $b.Size = New-Object System.Drawing.Size($w, 26)
    $parent.Controls.Add($b); return $b
}

Add-Label $tPlay 'Ник' 20 22 | Out-Null
$bName = Add-Box $tPlay $cfg.name 150 20

$rbHost  = New-Object System.Windows.Forms.RadioButton
$rbHost.Text = 'Играть хостом (друзья подключаются ко мне)'
$rbHost.Location = New-Object System.Drawing.Point(20, 60); $rbHost.Size = New-Object System.Drawing.Size(460, 26)
$rbGuest = New-Object System.Windows.Forms.RadioButton
$rbGuest.Text = 'Подключиться к другу'
$rbGuest.Location = New-Object System.Drawing.Point(20, 88); $rbGuest.Size = New-Object System.Drawing.Size(460, 26)
$rbSolo  = New-Object System.Windows.Forms.RadioButton
$rbSolo.Text = 'Один, без сети'
$rbSolo.Location = New-Object System.Drawing.Point(20, 116); $rbSolo.Size = New-Object System.Drawing.Size(460, 26)
$tPlay.Controls.AddRange(@($rbHost, $rbGuest, $rbSolo))
switch ("$($cfg.role)") { 'guest' { $rbGuest.Checked = $true } 'off' { $rbSolo.Checked = $true } default { $rbHost.Checked = $true } }

Add-Label $tPlay 'IP хоста' 20 158 | Out-Null
$bIp = Add-Box $tPlay $cfg.host_ip 150 156
Add-Label $tPlay 'Порт' 20 194 | Out-Null
$bPort = Add-Box $tPlay $cfg.port 150 192 100
Add-Label $tPlay 'Пароль' 20 230 | Out-Null
$bKey = Add-Box $tPlay $cfg.key 150 228
Add-Label $tPlay 'Лобби' 20 266 | Out-Null
$bLobby = Add-Box $tPlay $cfg.lobby 150 264
Add-Label $tPlay 'Комната' 20 302 | Out-Null
$bRoom = Add-Box $tPlay $cfg.room 150 300
$hint = Add-Label $tPlay 'Лобби и комната необязательны: с ними друзья находят хоста без IP.' 20 334 460
$hint.ForeColor = [System.Drawing.Color]::Gray

function Update-Enabled {
    $g = $rbGuest.Checked
    $net = -not $rbSolo.Checked
    $bIp.Enabled = $g
    $bPort.Enabled = $net; $bKey.Enabled = $net; $bLobby.Enabled = $net; $bRoom.Enabled = $net
}
$rbHost.Add_CheckedChanged({ Update-Enabled }); $rbGuest.Add_CheckedChanged({ Update-Enabled }); $rbSolo.Add_CheckedChanged({ Update-Enabled })
Update-Enabled

$btnPlay = New-Object System.Windows.Forms.Button
$btnPlay.Text = 'Играть'
$btnPlay.Location = New-Object System.Drawing.Point(20, 372); $btnPlay.Size = New-Object System.Drawing.Size(150, 40)
$btnPlay.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$tPlay.Controls.Add($btnPlay)

$status = New-Object System.Windows.Forms.TextBox
$status.Multiline = $true; $status.ReadOnly = $true; $status.ScrollBars = 'Vertical'
$status.Font = New-Object System.Drawing.Font('Consolas', 8.5)
$status.Location = New-Object System.Drawing.Point(20, 420); $status.Size = New-Object System.Drawing.Size(460, 80)
$tPlay.Controls.Add($status)

$btnPlay.Add_Click({
    $port = 0
    if (-not [int]::TryParse($bPort.Text.Trim(), [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        [System.Windows.Forms.MessageBox]::Show('Порт должен быть числом от 1 до 65535.', 'WoT LAN') | Out-Null; return
    }
    if ($rbGuest.Checked -and -not $bIp.Text.Trim() -and -not ($bLobby.Text.Trim() -and $bRoom.Text.Trim())) {
        [System.Windows.Forms.MessageBox]::Show('Укажите IP хоста или лобби с комнатой.', 'WoT LAN') | Out-Null; return
    }
    $role = 'host'; if ($rbGuest.Checked) { $role = 'guest' }; if ($rbSolo.Checked) { $role = 'off' }
    $cfg.role = $role
    $cfg.name = $bName.Text.Trim()
    $cfg.host_ip = $bIp.Text.Trim()
    $cfg.port = $port
    $cfg.key = $bKey.Text
    $cfg.lobby = $bLobby.Text.Trim()
    $cfg.room = $bRoom.Text.Trim()
    Save-Cfg $cfg
    if (Get-Process WorldOfTanks -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
        [System.Windows.Forms.MessageBox]::Show('Игра уже запущена. Настройки сохранены, они применятся при следующем запуске.', 'WoT LAN') | Out-Null; return
    }
    Start-Process $exe -WorkingDirectory $GameDir
    $status.Text = "Запуск... ($role)"
})

# live link status while the launcher stays open
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({
    if ($tabs.SelectedTab -ne $tPlay) { return }
    if (Test-Path $statPath) {
        try {
            $age = ((Get-Date) - (Get-Item $statPath).LastWriteTime).TotalSeconds
            $txt = Get-Content $statPath -Raw -Encoding UTF8
            if ($age -gt 10) { $txt = "(игра не запущена или не отвечает)`r`n" + $txt }
            $status.Text = $txt -replace "`r?`n", "`r`n"
        } catch {}
    }
})
$timer.Start()

# ---------------------------------------------------------------- stats tab
$grid = New-Object System.Windows.Forms.DataGridView
$grid.Location = New-Object System.Drawing.Point(10, 10); $grid.Size = New-Object System.Drawing.Size(480, 440)
$grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false; $grid.RowHeadersVisible = $false
$grid.AutoSizeColumnsMode = 'Fill'
foreach ($col in @('Игрок', 'Бои', 'Победы', '% побед', 'Фраги', 'Урон', 'Ср. урон')) { [void]$grid.Columns.Add($col, $col) }
$tStats.Controls.Add($grid)
$statsNote = Add-Label $tStats 'Статистика копится у хоста (lan_stats.json).' 10 458 480
$statsNote.ForeColor = [System.Drawing.Color]::Gray

function Load-Stats {
    $grid.Rows.Clear()
    if (-not (Test-Path $statsDb)) { return }
    try { $db = Get-Content $statsDb -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return }
    if ($null -eq $db.players) { return }
    $rows = @()
    foreach ($p in $db.players.PSObject.Properties) {
        $s = $p.Value
        $b = [int]$s.battles; $w = [int]$s.wins; $d = [int]$s.damage
        $rows += [pscustomobject]@{ n = "$($s.name)"; b = $b; w = $w; f = [int]$s.frags; d = $d }
    }
    foreach ($r in ($rows | Sort-Object -Property d -Descending)) {
        $pct = if ($r.b) { [math]::Round(100.0 * $r.w / $r.b) } else { 0 }
        $avg = if ($r.b) { [math]::Round($r.d / $r.b) } else { 0 }
        [void]$grid.Rows.Add($r.n, $r.b, $r.w, "$pct%", $r.f, $r.d, $avg)
    }
}
$tabs.Add_SelectedIndexChanged({ if ($tabs.SelectedTab -eq $tStats) { Load-Stats } })

[void]$form.ShowDialog()
$timer.Stop()
