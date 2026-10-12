# WoT 0.8.2 LAN launcher: account login, friends, platoon, then start the game.
# Lives in the game folder next to WorldOfTanks.exe; started by "WoT LAN.cmd".
# Windows PowerShell 5.1 + WinForms. This file must be saved as UTF-8 with BOM.
#
#   -SelfTest  runs the lobby calls without any window (used for testing)

param([string]$GameDir = $PSScriptRoot, [switch]$SelfTest)

$ErrorActionPreference = 'Continue'

$exe      = Join-Path $GameDir 'WorldOfTanks.exe'
$userDir  = Join-Path $GameDir 'offhangar_user'
$cfgPath  = Join-Path $userDir 'lan.json'
$statPath = Join-Path $userDir 'lanlink.txt'
$statsDb  = Join-Path $userDir 'lan_stats.json'

# ------------------------------------------------------------------ config
function Read-Cfg {
    $c = $null
    if (Test-Path $cfgPath) {
        try { $c = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $c = $null }
    }
    if ($null -eq $c) { $c = New-Object PSObject }
    foreach ($kv in @(@('role','host'), @('host_ip','127.0.0.1'), @('port',27015), @('name','Player'), @('key',''),
                      @('lobby',''), @('room',''), @('token',''), @('account',''))) {
        if (-not ($c.PSObject.Properties.Name -contains $kv[0])) {
            $c | Add-Member -NotePropertyName $kv[0] -NotePropertyValue $kv[1] -Force
        }
    }
    return $c
}
function Set-CfgValue($c, $k, $v) {
    if ($c.PSObject.Properties.Name -contains $k) { $c.$k = $v } else { $c | Add-Member -NotePropertyName $k -NotePropertyValue $v -Force }
}
function Save-Cfg($c) {
    if (-not (Test-Path $userDir)) { New-Item $userDir -ItemType Directory -Force | Out-Null }
    $json = $c | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ------------------------------------------------------------------ mod options (offhangar_user/config.json)
$modCfgPath = Join-Path $userDir 'config.json'
$modDefPath = Join-Path $GameDir 'res_mods\0.8.2\scripts\client\gui\mods\offhangar\config_defaults.json'
function Get-BotsPerTeam {
    foreach ($p in @($modCfgPath, $modDefPath)) {
        if (Test-Path $p) {
            $t = [System.IO.File]::ReadAllText($p)
            if ($t -match '"bots_per_team"\s*:\s*(\d+)') { return [Math]::Min(15, [int]$Matches[1]) }
        }
    }
    return 15
}
function Set-BotsPerTeam([int]$n) {
    # Edits only that one number, so the rest of the file (and its comments) stays as it is.
    try {
        if (-not (Test-Path $userDir)) { New-Item $userDir -ItemType Directory -Force | Out-Null }
        if (-not (Test-Path $modCfgPath)) {
            if (Test-Path $modDefPath) { Copy-Item $modDefPath $modCfgPath } else { [System.IO.File]::WriteAllText($modCfgPath, '{}') }
        }
        $t = [System.IO.File]::ReadAllText($modCfgPath)
        if ($t -match '"bots_per_team"\s*:\s*\d+') {
            $t = [regex]::Replace($t, '"bots_per_team"\s*:\s*\d+', ('"bots_per_team": ' + $n))
        } else {
            $t = [regex]::Replace($t, '^\s*\{', ('{' + "`r`n" + '    "bots_per_team": ' + $n + ','))
            $t = [regex]::Replace($t, ',\s*\}\s*$', "`r`n}")
        }
        [System.IO.File]::WriteAllText($modCfgPath, $t, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

# ------------------------------------------------------------------ bot brain (bot_brain.py, Python 3)
$brainScript = Join-Path $GameDir 'bot_brain.py'
$script:brainNote = ''
function Find-Python3 {
    # Returns @(exe, extraArgs) of a working Python 3, or $null. The Windows
    # Store alias 'python.exe' exists even without Python, so ask for real.
    foreach ($c in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
        $cmd = Get-Command $c[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { continue }
        try { $v = & $cmd.Source @($c[1] + @('-c', 'import sys;print(sys.version_info[0])')) 2>$null } catch { $v = $null }
        if ("$v".Trim() -eq '3') { return ,@($cmd.Source, $c[1]) }
    }
    return $null
}
function Start-Brain {
    if (-not (Test-Path $brainScript)) { $script:brainNote = 'Мозг ботов: нет bot_brain.py рядом с игрой - боты считаются в игре.'; return }
    $py = Find-Python3
    if (-not $py) { $script:brainNote = 'Мозг ботов: не найден Python 3 - боты считаются в игре (как раньше).'; return }
    # A brain left over from an older version would hold the port: restart it.
    try {
        Get-CimInstance Win32_Process -Filter "Name like 'py%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'bot_brain\.py' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch {}
    $args2 = @($py[1]) + @("`"$brainScript`"", '--dir', "`"$GameDir`"")
    try {
        Start-Process -FilePath $py[0] -ArgumentList $args2 -WindowStyle Hidden | Out-Null
        $script:brainNote = 'Мозг ботов: запущен (' + $py[0] + ')'
    } catch { $script:brainNote = 'Мозг ботов: не запустился - ' + $_.Exception.Message }
}

# ------------------------------------------------------------------ lobby client
$script:udp = $null
$script:lobbyEp = $null
$script:q = 0

function Set-Lobby([string]$spec) {
    $script:lobbyEp = $null
    $spec = "$spec".Trim()
    if (-not $spec) { return $false }
    $hostPart = $spec; $portPart = 27016
    if ($spec -match '^(.+):(\d+)$') { $hostPart = $Matches[1]; $portPart = [int]$Matches[2] }
    try {
        $ip = [System.Net.Dns]::GetHostAddresses($hostPart) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
        if ($null -eq $ip) { return $false }
        $script:lobbyEp = New-Object System.Net.IPEndPoint($ip, $portPart)
        if ($null -eq $script:udp) {
            $script:udp = New-Object System.Net.Sockets.UdpClient(0)
            $script:udp.Client.ReceiveTimeout = 1500
        }
        return $true
    } catch { return $false }
}

function Lobby-Call([hashtable]$msg) {
    if ($null -eq $script:lobbyEp) { return $null }
    $script:q += 1
    $msg['p'] = 'WOTLOBBY1'; $msg['q'] = $script:q
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($msg | ConvertTo-Json -Compress -Depth 6))
    for ($try = 0; $try -lt 2; $try++) {
        try { [void]$script:udp.Send($bytes, $bytes.Length, $script:lobbyEp) } catch { return $null }
        $deadline = (Get-Date).AddMilliseconds(1500)
        while ((Get-Date) -lt $deadline) {
            $from = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
            try { $data = $script:udp.Receive([ref]$from) } catch { break }
            try { $r = [System.Text.Encoding]::UTF8.GetString($data) | ConvertFrom-Json } catch { continue }
            if ($r.q -eq $script:q) { return $r }
        }
    }
    return $null
}

function Pw-Hash([string]$login, [string]$password) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $b = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes('wotlan:' + $login.ToLower() + ':' + $password))
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Status-Ru([string]$s) {
    switch ($s) { 'battle' { 'в бою' } 'hangar' { 'в ангаре' } 'launcher' { 'в лаунчере' } default { 'не в сети' } }
}

# ------------------------------------------------------------------ self test (no window)
if ($SelfTest) {
    $cfg = Read-Cfg
    if (-not (Set-Lobby $cfg.lobby)) { 'lobby: cannot resolve'; exit 1 }
    $login = "$env:WOTLAN_TEST_LOGIN"; $pw = "$env:WOTLAN_TEST_PW"
    $r = Lobby-Call @{ t = 'register'; login = $login; pw = (Pw-Hash $login $pw); name = $login }
    "register: " + ($r | ConvertTo-Json -Compress)
    $r = Lobby-Call @{ t = 'login'; login = $login; pw = (Pw-Hash $login $pw) }
    "login: ok=$($r.ok) name=$($r.name)"
    if ($r.ok) {
        Set-CfgValue $cfg 'token' $r.token; Set-CfgValue $cfg 'uid' $r.uid; Set-CfgValue $cfg 'name' $r.name; Set-CfgValue $cfg 'account' $login
        Save-Cfg $cfg
        $f = Lobby-Call @{ t = 'friends'; token = $r.token }
        "friends: " + (($f.friends | ForEach-Object { $_.name + '=' + $_.status }) -join ', ')
        "incoming: " + (($f.incoming | ForEach-Object { $_.name }) -join ', ')
        if ("$env:WOTLAN_TEST_ADD") { "add: " + ((Lobby-Call @{ t = 'friend_add'; token = $r.token; name = "$env:WOTLAN_TEST_ADD" }) | ConvertTo-Json -Compress) }
        foreach ($i in $f.incoming) { "accept " + $i.name + ": " + ((Lobby-Call @{ t = 'friend_accept'; token = $r.token; uid = $i.uid }).ok) }
        $reg = Lobby-Call @{ t = 'reg'; token = $r.token; client = 'launcher' }
        "notes: " + (($reg.notes) -join ' | ')
    }
    exit 0
}

# ------------------------------------------------------------------ GUI
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

if (-not (Test-Path $exe)) {
    [System.Windows.Forms.MessageBox]::Show("WorldOfTanks.exe не найден в`n$GameDir`n`nПоложите лаунчер в папку игры.", 'WoT LAN', 'OK', 'Error') | Out-Null
    exit 1
}
$cfg = Read-Cfg
$script:me = $null          # @{uid; name} when logged in
$script:platoon = $null
$script:friendsCache = @()

function Msg($text) { [System.Windows.Forms.MessageBox]::Show($text, 'WoT LAN') | Out-Null }
function New-Ctl($type, $parent, $text, $x, $y, $w, $h) {
    $c = New-Object "System.Windows.Forms.$type"
    if ($null -ne $text) { $c.Text = $text }
    $c.Location = New-Object System.Drawing.Point($x, $y)
    $c.Size = New-Object System.Drawing.Size($w, $h)
    $parent.Controls.Add($c)
    return $c
}

# ---------------- login dialog
function Show-Login {
    $f = New-Object System.Windows.Forms.Form
    $f.Text = 'WoT LAN - вход'; $f.Size = New-Object System.Drawing.Size(420, 330)
    $f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false
    $f.Font = New-Object System.Drawing.Font('Segoe UI', 10)
    New-Ctl Label $f 'Сервер' 20 22 100 24 | Out-Null
    $bSrv = New-Ctl TextBox $f $cfg.lobby 130 20 250 26
    New-Ctl Label $f 'Логин' 20 60 100 24 | Out-Null
    $bLogin = New-Ctl TextBox $f $cfg.account 130 58 250 26
    New-Ctl Label $f 'Пароль' 20 98 100 24 | Out-Null
    $bPw = New-Ctl TextBox $f '' 130 96 250 26; $bPw.UseSystemPasswordChar = $true
    New-Ctl Label $f 'Ник (для регистрации)' 20 136 110 40 | Out-Null
    $bNick = New-Ctl TextBox $f '' 130 140 250 26
    $info = New-Ctl Label $f '' 20 178 370 36; $info.ForeColor = [System.Drawing.Color]::Firebrick
    $bIn  = New-Ctl Button $f 'Войти' 20 220 110 36
    $bReg = New-Ctl Button $f 'Регистрация' 140 220 120 36
    $bOff = New-Ctl Button $f 'Без аккаунта' 270 220 110 36
    $f.AcceptButton = $bIn
    $result = @{ ok = $false }

    $doLogin = {
        param($register)
        $info.Text = ''
        if (-not (Set-Lobby $bSrv.Text)) { $info.Text = 'Сервер не найден. Пример: 1.2.3.4:27016'; return }
        $login = $bLogin.Text.Trim(); $pw = $bPw.Text
        if (-not $login -or -not $pw) { $info.Text = 'Введите логин и пароль'; return }
        $h = Pw-Hash $login $pw
        if ($register) {
            $nick = $bNick.Text.Trim(); if (-not $nick) { $nick = $login }
            $r = Lobby-Call @{ t = 'register'; login = $login; pw = $h; name = $nick }
            if ($null -eq $r) { $info.Text = 'Сервер не отвечает'; return }
            if ($r.err) { $info.Text = $r.err; return }
        }
        $r = Lobby-Call @{ t = 'login'; login = $login; pw = $h }
        if ($null -eq $r) { $info.Text = 'Сервер не отвечает'; return }
        if ($r.err) { $info.Text = $r.err; return }
        Set-CfgValue $cfg 'lobby' $bSrv.Text.Trim(); Set-CfgValue $cfg 'account' $login
        Set-CfgValue $cfg 'token' $r.token; Set-CfgValue $cfg 'uid' $r.uid; Set-CfgValue $cfg 'name' $r.name
        Save-Cfg $cfg
        $script:me = @{ uid = $r.uid; name = $r.name }
        $result.ok = $true
        $f.Close()
    }
    $bIn.Add_Click({ & $doLogin $false })
    $bReg.Add_Click({ & $doLogin $true })
    $bOff.Add_Click({
        Set-CfgValue $cfg 'token' ''; Save-Cfg $cfg
        $script:me = $null; $result.ok = $true; $f.Close()
    })
    [void]$f.ShowDialog()
    return $result.ok
}

# check a saved session, else ask to log in
$loggedIn = $false
if ($cfg.token -and $cfg.lobby -and (Set-Lobby $cfg.lobby)) {
    $r = Lobby-Call @{ t = 'reg'; token = $cfg.token; client = 'launcher' }
    if ($r -and $r.account) { $script:me = @{ uid = $r.uid; name = $r.name }; $script:platoon = $r.platoon; $loggedIn = $true }
}
if (-not $loggedIn) {
    if (-not (Show-Login)) { exit 0 }
}

# ---------------- main window
$form = New-Object System.Windows.Forms.Form
$form.Text = 'World of Tanks 0.8.2 - LAN'
$form.Size = New-Object System.Drawing.Size(560, 600)
$form.StartPosition = 'CenterScreen'; $form.FormBorderStyle = 'FixedDialog'; $form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

$top = New-Ctl Label $form '' 12 8 400 24
$btnOut = New-Ctl Button $form 'Выйти' 440 4 90 28
$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Location = New-Object System.Drawing.Point(8, 38); $tabs.Size = New-Object System.Drawing.Size(530, 480)
$form.Controls.Add($tabs)
$noteBar = New-Ctl Label $form '' 12 524 520 36; $noteBar.ForeColor = [System.Drawing.Color]::DarkGreen

$tPlay = New-Object System.Windows.Forms.TabPage; $tPlay.Text = 'Игра'
$tFr   = New-Object System.Windows.Forms.TabPage; $tFr.Text = 'Друзья'
$tPl   = New-Object System.Windows.Forms.TabPage; $tPl.Text = 'Взвод'
$tSt   = New-Object System.Windows.Forms.TabPage; $tSt.Text = 'Статистика'
$tabs.TabPages.Add($tPlay)
if ($script:me) { $tabs.TabPages.Add($tFr); $tabs.TabPages.Add($tPl) }
$tabs.TabPages.Add($tSt)

function Update-Top {
    if ($script:me) {
        $pl = ''
        if ($script:platoon) { $pl = '   |   взвод: ' + (($script:platoon.members | ForEach-Object { $_.name }) -join ', ') }
        $top.Text = 'Вы: ' + $script:me.name + $pl
        $btnOut.Text = 'Выйти'
    } else {
        $top.Text = 'Без аккаунта (' + $cfg.name + ')'
        $btnOut.Text = 'Войти'
    }
}
$btnOut.Add_Click({
    if ($script:me) { [void](Lobby-Call @{ t = 'logout'; token = $cfg.token }); Set-CfgValue $cfg 'token' ''; Save-Cfg $cfg }
    $form.Close()
    Start-Process powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-STA', '-File', "`"$PSCommandPath`"") | Out-Null
})

# ---------------- tab: play
$y = 16
if (-not $script:me) {
    New-Ctl Label $tPlay 'Ник' 20 ($y + 2) 120 24 | Out-Null
    $bName = New-Ctl TextBox $tPlay $cfg.name 150 $y 330 26
    $y += 40
}
$rbHost  = New-Ctl RadioButton $tPlay 'Играть хостом (ко мне подключаются)' 20 $y 470 26; $y += 28
$rbGuest = New-Ctl RadioButton $tPlay 'Подключиться к другу' 20 $y 470 26; $y += 28
$rbSolo  = New-Ctl RadioButton $tPlay 'Один, без сети' 20 $y 470 26; $y += 40
switch ("$($cfg.role)") { 'guest' { $rbGuest.Checked = $true } 'off' { $rbSolo.Checked = $true } default { $rbHost.Checked = $true } }
New-Ctl Label $tPlay 'IP хоста' 20 ($y + 2) 120 24 | Out-Null;  $bIp = New-Ctl TextBox $tPlay $cfg.host_ip 150 $y 330 26; $y += 36
New-Ctl Label $tPlay 'Порт' 20 ($y + 2) 120 24 | Out-Null;      $bPort = New-Ctl TextBox $tPlay $cfg.port 150 $y 100 26
New-Ctl Label $tPlay 'Ботов в команде' 270 ($y + 2) 130 24 | Out-Null
$bBots = New-Object System.Windows.Forms.NumericUpDown
$bBots.Minimum = 0; $bBots.Maximum = 15; $bBots.Value = (Get-BotsPerTeam); $bBots.SetBounds(410, $y, 70, 26); $tPlay.Controls.Add($bBots)
$tip = New-Object System.Windows.Forms.ToolTip
$tip.SetToolTip($bBots, 'Сколько танков в каждой команде, включая игроков. Меньше ботов - больше FPS. В бою по сети считается настройка хоста.')
$y += 36
New-Ctl Label $tPlay 'Пароль комнаты' 20 ($y + 2) 125 24 | Out-Null; $bKey = New-Ctl TextBox $tPlay $cfg.key 150 $y 330 26; $y += 36
New-Ctl Label $tPlay 'Комната' 20 ($y + 2) 120 24 | Out-Null;   $bRoom = New-Ctl TextBox $tPlay $cfg.room 150 $y 330 26; $y += 36
$plHint = New-Ctl Label $tPlay '' 20 $y 470 40; $plHint.ForeColor = [System.Drawing.Color]::SteelBlue; $y += 44
$btnPlay = New-Ctl Button $tPlay 'Играть' 20 $y 150 40
$btnPlay.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold); $y += 50
$status = New-Ctl TextBox $tPlay '' 20 $y 470 (430 - $y)
$status.Multiline = $true; $status.ReadOnly = $true; $status.ScrollBars = 'Vertical'
$status.Font = New-Object System.Drawing.Font('Consolas', 8.5)

function Update-PlayControls {
    $inPl = ($null -ne $script:platoon)
    foreach ($c in @($rbHost, $rbGuest, $rbSolo, $bIp, $bRoom)) { $c.Enabled = -not $inPl }
    if ($inPl) {
        if ($script:platoon.leader -eq $script:me.uid) { $plHint.Text = 'Вы командир взвода: игра запустится хостом, взвод подключится к вам.' }
        else { $plHint.Text = 'Вы во взводе ' + $script:platoon.leader_name + ': игра подключится к командиру сама.' }
    } else {
        $plHint.Text = ''
        $bIp.Enabled = $rbGuest.Checked
    }
}
$rbHost.Add_CheckedChanged({ Update-PlayControls }); $rbGuest.Add_CheckedChanged({ Update-PlayControls }); $rbSolo.Add_CheckedChanged({ Update-PlayControls })

$btnPlay.Add_Click({
    $port = 0
    if (-not [int]::TryParse($bPort.Text.Trim(), [ref]$port) -or $port -lt 1 -or $port -gt 65535) { Msg 'Порт должен быть числом от 1 до 65535.'; return }
    if ($script:platoon) {
        if ($script:platoon.leader -eq $script:me.uid) { $role = 'host' } else { $role = 'guest' }
        Set-CfgValue $cfg 'host_ip' ''
        Set-CfgValue $cfg 'room' ''
    } else {
        $role = 'host'; if ($rbGuest.Checked) { $role = 'guest' }; if ($rbSolo.Checked) { $role = 'off' }
        if ($role -eq 'guest' -and -not $bIp.Text.Trim() -and -not $bRoom.Text.Trim()) { Msg 'Укажите IP хоста или комнату.'; return }
        Set-CfgValue $cfg 'host_ip' $bIp.Text.Trim()
        Set-CfgValue $cfg 'room' $bRoom.Text.Trim()
    }
    if (-not $script:me -and $bName) { Set-CfgValue $cfg 'name' $bName.Text.Trim() }
    Set-CfgValue $cfg 'role' $role
    Set-CfgValue $cfg 'port' $port
    Set-CfgValue $cfg 'key' $bKey.Text
    Save-Cfg $cfg
    Set-BotsPerTeam ([int]$bBots.Value)
    if (Get-Process WorldOfTanks -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
        Msg 'Игра уже запущена. Настройки сохранены, они применятся при следующем запуске.'; return
    }
    if ($role -ne 'guest') { Start-Brain }     # a guest's bots follow the host: no brain needed
    Start-Process $exe -WorkingDirectory $GameDir
    $status.Text = "Запуск... ($role)`r`n" + $script:brainNote
})

# ---------------- tab: friends
if ($script:me) {
    $lvF = New-Ctl ListView $tFr $null 10 10 500 230
    $lvF.View = 'Details'; $lvF.FullRowSelect = $true; $lvF.MultiSelect = $false
    [void]$lvF.Columns.Add('Друг', 250); [void]$lvF.Columns.Add('Статус', 200)
    $bInvite = New-Ctl Button $tFr 'Пригласить во взвод' 10 246 180 32
    $bRemove = New-Ctl Button $tFr 'Удалить из друзей' 200 246 160 32
    New-Ctl Label $tFr 'Добавить по нику:' 10 292 140 24 | Out-Null
    $bAddName = New-Ctl TextBox $tFr '' 150 290 220 26
    $bAdd = New-Ctl Button $tFr 'Добавить' 380 288 130 30
    New-Ctl Label $tFr 'Заявки в друзья:' 10 330 200 24 | Out-Null
    $lvR = New-Ctl ListView $tFr $null 10 356 330 80
    $lvR.View = 'Details'; $lvR.FullRowSelect = $true; [void]$lvR.Columns.Add('Игрок', 300)
    $bAcc = New-Ctl Button $tFr 'Принять' 350 356 160 32
    $bDec = New-Ctl Button $tFr 'Отклонить' 350 396 160 32

    function Refresh-Friends {
        $r = Lobby-Call @{ t = 'friends'; token = $cfg.token }
        if ($null -eq $r -or $r.err) { return }
        $script:platoon = $r.platoon
        $lvF.Items.Clear()
        foreach ($fr in $r.friends) {
            $it = New-Object System.Windows.Forms.ListViewItem($fr.name)
            [void]$it.SubItems.Add((Status-Ru $fr.status)); $it.Tag = $fr.uid
            if ($fr.status -eq 'offline') { $it.ForeColor = [System.Drawing.Color]::Gray }
            [void]$lvF.Items.Add($it)
        }
        $lvR.Items.Clear()
        foreach ($rq in $r.incoming) { $it = New-Object System.Windows.Forms.ListViewItem($rq.name); $it.Tag = $rq.uid; [void]$lvR.Items.Add($it) }
        Update-Top; Update-PlayControls
    }
    function Sel-Uid($lv) { if ($lv.SelectedItems.Count -gt 0) { return $lv.SelectedItems[0].Tag } else { return $null } }
    $bAdd.Add_Click({
        $n = $bAddName.Text.Trim(); if (-not $n) { return }
        $r = Lobby-Call @{ t = 'friend_add'; token = $cfg.token; name = $n }
        if ($null -eq $r) { Msg 'Сервер не отвечает'; return }
        if ($r.err) { Msg $r.err } else { $noteBar.Text = 'Заявка отправлена: ' + $n; $bAddName.Text = '' }
        Refresh-Friends
    })
    $bAcc.Add_Click({ $u = Sel-Uid $lvR; if ($u) { [void](Lobby-Call @{ t = 'friend_accept'; token = $cfg.token; uid = $u }); Refresh-Friends } })
    $bDec.Add_Click({ $u = Sel-Uid $lvR; if ($u) { [void](Lobby-Call @{ t = 'friend_decline'; token = $cfg.token; uid = $u }); Refresh-Friends } })
    $bRemove.Add_Click({
        $u = Sel-Uid $lvF; if (-not $u) { return }
        if ([System.Windows.Forms.MessageBox]::Show('Удалить из друзей?', 'WoT LAN', 'YesNo') -eq 'Yes') {
            [void](Lobby-Call @{ t = 'friend_remove'; token = $cfg.token; uid = $u }); Refresh-Friends
        }
    })
    $bInvite.Add_Click({
        $u = Sel-Uid $lvF; if (-not $u) { Msg 'Выберите друга в списке.'; return }
        $r = Lobby-Call @{ t = 'pl_invite'; token = $cfg.token; uid = $u }
        if ($null -eq $r) { Msg 'Сервер не отвечает'; return }
        if ($r.err) { Msg $r.err } else { $noteBar.Text = 'Приглашение во взвод отправлено' }
        Refresh-Platoon
    })

    # ---------------- tab: platoon
    $lvP = New-Ctl ListView $tPl $null 10 10 500 200
    $lvP.View = 'Details'; $lvP.FullRowSelect = $true
    [void]$lvP.Columns.Add('Во взводе', 250); [void]$lvP.Columns.Add('Статус', 200)
    $bKick  = New-Ctl Button $tPl 'Исключить' 10 216 140 32
    $bLeave = New-Ctl Button $tPl 'Покинуть взвод' 160 216 160 32
    New-Ctl Label $tPl 'Приглашения во взвод:' 10 262 300 24 | Out-Null
    $lvI = New-Ctl ListView $tPl $null 10 288 330 90
    $lvI.View = 'Details'; $lvI.FullRowSelect = $true; [void]$lvI.Columns.Add('От кого', 300)
    $bPAcc = New-Ctl Button $tPl 'Вступить' 350 288 160 32
    $bPDec = New-Ctl Button $tPl 'Отказаться' 350 328 160 32
    $plNote = New-Ctl Label $tPl 'Пригласить можно на вкладке «Друзья». Командир играет хостом, взвод подключается к нему.' 10 390 500 40
    $plNote.ForeColor = [System.Drawing.Color]::Gray

    function Refresh-Platoon {
        $r = Lobby-Call @{ t = 'pl_state'; token = $cfg.token }
        if ($null -eq $r -or $r.err) { return }
        $script:platoon = $r.platoon
        $lvP.Items.Clear()
        if ($r.platoon) {
            foreach ($m in $r.platoon.members) {
                $label = $m.name; if ($m.uid -eq $r.platoon.leader) { $label += '  (командир)' }
                $it = New-Object System.Windows.Forms.ListViewItem($label); [void]$it.SubItems.Add((Status-Ru $m.status)); $it.Tag = $m.uid
                [void]$lvP.Items.Add($it)
            }
            foreach ($m in $r.platoon.invited) {
                $it = New-Object System.Windows.Forms.ListViewItem($m.name + '  (приглашён)'); [void]$it.SubItems.Add(''); $it.ForeColor = [System.Drawing.Color]::Gray
                [void]$lvP.Items.Add($it)
            }
        }
        $lvI.Items.Clear()
        foreach ($inv in $r.invites) { $it = New-Object System.Windows.Forms.ListViewItem($inv.name); $it.Tag = $inv.leader; [void]$lvI.Items.Add($it) }
        $bKick.Enabled = ($r.platoon -and $r.platoon.leader -eq $script:me.uid)
        $bLeave.Enabled = [bool]$r.platoon
        Update-Top; Update-PlayControls
    }
    $bPAcc.Add_Click({
        $l = Sel-Uid $lvI; if (-not $l) { return }
        $r = Lobby-Call @{ t = 'pl_accept'; token = $cfg.token; leader = $l }
        if ($r -and $r.err) { Msg $r.err }
        Refresh-Platoon
        if ($script:platoon -and (Get-Process WorldOfTanks -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })) {
            Msg 'Вы во взводе. Перезапустите игру кнопкой «Играть», чтобы подключиться к командиру.'
        }
    })
    $bPDec.Add_Click({ $l = Sel-Uid $lvI; if ($l) { [void](Lobby-Call @{ t = 'pl_decline'; token = $cfg.token; leader = $l }); Refresh-Platoon } })
    $bLeave.Add_Click({ [void](Lobby-Call @{ t = 'pl_leave'; token = $cfg.token }); Refresh-Platoon })
    $bKick.Add_Click({ $u = Sel-Uid $lvP; if ($u -and $u -ne $script:me.uid) { [void](Lobby-Call @{ t = 'pl_kick'; token = $cfg.token; uid = $u }); Refresh-Platoon } })
}

# ---------------- tab: stats
$grid = New-Object System.Windows.Forms.DataGridView
$grid.Location = New-Object System.Drawing.Point(10, 10); $grid.Size = New-Object System.Drawing.Size(500, 400)
$grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false; $grid.RowHeadersVisible = $false; $grid.AutoSizeColumnsMode = 'Fill'
foreach ($col in @('Игрок', 'Бои', 'Победы', '% побед', 'Фраги', 'Урон', 'Ср. урон')) { [void]$grid.Columns.Add($col, $col) }
$tSt.Controls.Add($grid)
$stNote = New-Ctl Label $tSt '' 10 416 500 24; $stNote.ForeColor = [System.Drawing.Color]::Gray

function Load-Stats {
    $grid.Rows.Clear()
    $rows = @()
    if ($script:me) {
        $r = Lobby-Call @{ t = 'stats' }
        $stNote.Text = 'Общая статистика аккаунтов с сервера.'
        if ($r -and $r.players) { foreach ($p in $r.players) { $rows += [pscustomobject]@{ n = "$($p.name)"; b = [int]$p.battles; w = [int]$p.wins; f = [int]$p.frags; d = [int]$p.damage } } }
    } else {
        $stNote.Text = 'Статистика копится у хоста (lan_stats.json).'
        if (Test-Path $statsDb) {
            try { $db = Get-Content $statsDb -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $db = $null }
            if ($db -and $db.players) { foreach ($p in $db.players.PSObject.Properties) { $s = $p.Value; $rows += [pscustomobject]@{ n = "$($s.name)"; b = [int]$s.battles; w = [int]$s.wins; f = [int]$s.frags; d = [int]$s.damage } } }
        }
    }
    foreach ($row in ($rows | Sort-Object -Property d -Descending)) {
        $pct = if ($row.b) { [math]::Round(100.0 * $row.w / $row.b) } else { 0 }
        $avg = if ($row.b) { [math]::Round($row.d / $row.b) } else { 0 }
        [void]$grid.Rows.Add($row.n, $row.b, $row.w, "$pct%", $row.f, $row.d, $avg)
    }
}

$tabs.Add_SelectedIndexChanged({
    if ($tabs.SelectedTab -eq $tSt) { Load-Stats }
    elseif ($script:me -and $tabs.SelectedTab -eq $tFr) { Refresh-Friends }
    elseif ($script:me -and $tabs.SelectedTab -eq $tPl) { Refresh-Platoon }
})

# ---------------- periodic: presence, notifications, link status
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 4000
$timer.Add_Tick({
    if ($script:me) {
        $r = Lobby-Call @{ t = 'reg'; token = $cfg.token; client = 'launcher'; name = $script:me.name }
        if ($r -and $r.err -eq 'auth') { $timer.Stop(); Msg 'Сессия истекла, войдите заново.'; $btnOut.PerformClick(); return }
        if ($r) {
            $script:platoon = $r.platoon
            if ($r.notes -and $r.notes.Count -gt 0) { $noteBar.Text = ($r.notes -join '   |   ') }
            Update-Top; Update-PlayControls
            if ($tabs.SelectedTab -eq $tFr) { Refresh-Friends } elseif ($tabs.SelectedTab -eq $tPl) { Refresh-Platoon }
        }
    }
    if ($tabs.SelectedTab -eq $tPlay) {
        try {
            $txt = ''
            if (Test-Path $statPath) {
                $age = ((Get-Date) - (Get-Item $statPath).LastWriteTime).TotalSeconds
                $txt = Get-Content $statPath -Raw -Encoding UTF8
                if ($age -gt 10) { $txt = "(игра не запущена или не отвечает)`r`n" + $txt }
            }
            if ($script:brainNote) { $txt += "`r`n" + $script:brainNote }
            foreach ($bf in @('brain_status.txt', 'brain_link.txt')) {
                $bp = Join-Path $userDir $bf
                if ((Test-Path $bp) -and ((Get-Date) - (Get-Item $bp).LastWriteTime).TotalSeconds -lt 30) {
                    $txt += "`r`n" + (Get-Content $bp -Raw -Encoding UTF8).Trim()
                }
            }
            if ($txt) { $status.Text = $txt -replace "`r?`n", "`r`n" }
        } catch {}
    }
})
$timer.Start()

Update-Top; Update-PlayControls
[void]$form.ShowDialog()
$timer.Stop()
