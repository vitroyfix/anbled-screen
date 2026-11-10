<#
.SYNOPSIS
    Installs Anbled Screen on Windows: a cast receiver for the Anbled app (songs and videos in a VLC player window).

.DESCRIPTION
    Installs for the current user. It finds Python 3.9+ and VLC, and installs them with winget if they're missing.
    It also adds Start menu shortcuts, opens the firewall for phones on private networks (one administrator prompt),
    can start Anbled Screen at sign-in, and adds an entry under Settings > Apps for uninstalling.

        irm https://raw.githubusercontent.com/vitroyfix/anbled-screen/main/windows/install.ps1 | iex

    From a clone:   .\windows\install.ps1 [-Autostart] [-NoStart] [-Quiet]
    Uninstall:      .\windows\install.ps1 -Uninstall    (or Settings > Apps > Anbled Screen > Uninstall)
#>
param(
    [switch]$Uninstall,
    [switch]$Autostart,
    [switch]$NoStart,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo     = 'https://raw.githubusercontent.com/vitroyfix/anbled-screen/main'
$AppDir   = Join-Path $env:LOCALAPPDATA 'Programs\Anbled Screen'
$Settings = Join-Path $env:APPDATA 'Anbled Screen'
$Cache    = Join-Path $env:LOCALAPPDATA 'Anbled Screen'
$Menu     = Join-Path ([Environment]::GetFolderPath('Programs')) 'Anbled Screen'
$AppsKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AnbledScreen'
$RuleName = 'Anbled Screen'

function Say($text)  { Write-Host $text }
function Step($text) { Write-Host "  > $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "Error: $text" -ForegroundColor Red; throw $text }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Runs PowerShell commands as administrator: directly if already elevated, otherwise after one Windows prompt.
function Invoke-Admin($command) {
    if (Test-Admin) {
        try { & ([scriptblock]::Create($command)); return $true } catch { return $false }
    }
    try {
        $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command
        return ($p.ExitCode -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $py) {
        foreach ($p in "$env:LOCALAPPDATA\Programs\Python\Launcher\py.exe", "$env:SystemRoot\py.exe") {
            if (Test-Path $p) { $py = Get-Item $p; break }
        }
    }
    if ($py) {
        $path = if ($py.Source) { $py.Source } else { $py.FullName }
        try {
            $exe = & $path -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates.Add($exe.Trim()) }
        } catch { }
    }
    foreach ($name in 'python.exe', 'python3.exe') {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        # (the WindowsApps "python.exe" only opens the Microsoft Store)
        if ($c -and $c.Source -notlike '*\WindowsApps\*') { $candidates.Add($c.Source) }
    }
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $candidates.Add($_.FullName) }
    foreach ($exe in $candidates) {
        try {
            & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $exe }
        } catch { }
    }
    return $null
}

function Find-Vlc {
    foreach ($key in 'HKLM:\SOFTWARE\VideoLAN\VLC', 'HKLM:\SOFTWARE\WOW6432Node\VideoLAN\VLC', 'HKCU:\SOFTWARE\VideoLAN\VLC') {
        $item = Get-ItemProperty $key -ErrorAction SilentlyContinue
        if ($item) {
            foreach ($value in $item.'(default)', $item.InstallDir) {
                if (-not $value) { continue }
                $exe = if ($value -like '*.exe') { $value } else { Join-Path $value 'vlc.exe' }
                if (Test-Path $exe) { return $exe }
            }
        }
    }
    foreach ($base in $env:ProgramFiles, ${env:ProgramFiles(x86)}) {
        if ($base -and (Test-Path "$base\VideoLAN\VLC\vlc.exe")) { return "$base\VideoLAN\VLC\vlc.exe" }
    }
    return $null
}

function Install-WithWinget($id, $what, $extra) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { return $false }
    Step "Installing $what with winget..."
    $arguments = @('install', '--id', $id, '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements') + $extra
    & winget.exe @arguments | Out-Null
    return $true
}

function Remove-Firewall {
    if (Get-NetFirewallRule -DisplayName "$RuleName*" -ErrorAction SilentlyContinue) {
        [void](Invoke-Admin "Remove-NetFirewallRule -DisplayName '$RuleName*'")
    }
}

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
    Say 'Uninstalling Anbled Screen...'
    $python = Find-Python
    $script = Join-Path $AppDir 'anbled_screen.py'
    if ($python -and (Test-Path $script)) {
        & $python $script --stop | Out-Null
        & $python $script --no-autostart | Out-Null
    }
    Remove-Item $Menu -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $AppsKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Settings, $Cache -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Remove-Firewall }
    # The uninstaller may be running from the folder it removes: what's left goes once it has finished.
    Get-ChildItem $AppDir -Exclude 'install.ps1' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Start-Process cmd.exe -WindowStyle Hidden -ArgumentList '/c', "timeout /t 3 >nul & rmdir /s /q `"$AppDir`""
    Say 'Anbled Screen is uninstalled. Python and VLC were left installed.'
    return
}

# ------------------------------------------------------------------ install
Write-Host ''
Write-Host 'Anbled Screen' -ForegroundColor White -NoNewline
Write-Host ' - cast songs and videos from the Anbled app to this computer'
Write-Host ''

# Python
$python = Find-Python
if (-not $python) {
    if (Install-WithWinget 'Python.Python.3.12' 'Python 3.12' @('--scope', 'user')) { $python = Find-Python }
    if (-not $python) { Fail 'Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/ and run this again.' }
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Step "Python: $python"

# VLC
$vlc = Find-Vlc
if (-not $vlc) {
    if (Install-WithWinget 'VideoLAN.VLC' 'VLC' @()) { $vlc = Find-Vlc }
    if (-not $vlc) { Fail 'VLC is needed to play what phones cast. Install it from https://www.videolan.org/ and run this again.' }
}
Step "VLC: $vlc"

# The files: from this clone, or downloaded
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$here = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
$fromClone = $here -and (Test-Path (Join-Path $here '..\anbled_screen.py'))
foreach ($file in @(@('anbled_screen.py', 'anbled_screen.py'), @('windows/anbled-screen.ico', 'anbled-screen.ico'), @('windows/install.ps1', 'install.ps1'))) {
    $dest = Join-Path $AppDir $file[1]
    if ($fromClone) {
        $src = Join-Path (Join-Path $here '..') $file[0]
        if ((Resolve-Path $src).Path -ne $dest) { Copy-Item $src $dest -Force }
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$Repo/$($file[0])" -OutFile $dest
    }
}
$script = Join-Path $AppDir 'anbled_screen.py'
$version = (& $python $script --version).Trim()
Step "Installed Anbled Screen $version in $AppDir"

# Start menu
New-Item -ItemType Directory -Force -Path $Menu | Out-Null
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($name, $target, $arguments, $description) {
    $link = $shell.CreateShortcut((Join-Path $Menu "$name.lnk"))
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $AppDir
    $link.IconLocation = (Join-Path $AppDir 'anbled-screen.ico')
    $link.Description = $description
    $link.Save()
}
$uninstallArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$AppDir\install.ps1`" -Uninstall"
New-Shortcut 'Anbled Screen' $pythonw "`"$script`" --start" 'Let the Anbled app cast to this computer'
New-Shortcut 'Turn off Anbled Screen' $pythonw "`"$script`" --stop" 'Stop Anbled Screen'
Step 'Added Start menu shortcuts'

# Settings > Apps
New-Item -Path $AppsKey -Force | Out-Null
$entry = @{
    DisplayName = 'Anbled Screen'; DisplayVersion = $version; Publisher = 'Anbled'
    DisplayIcon = (Join-Path $AppDir 'anbled-screen.ico'); InstallLocation = $AppDir
    URLInfoAbout = 'https://github.com/vitroyfix/anbled-screen'
    UninstallString = "powershell.exe $uninstallArgs"
}
foreach ($k in $entry.Keys) { Set-ItemProperty -Path $AppsKey -Name $k -Value $entry[$k] }
Set-ItemProperty -Path $AppsKey -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $AppsKey -Name NoRepair -Value 1 -Type DWord

# Firewall: phones find it on UDP 1900 and control it on TCP 49494 (private networks only)
if (-not (Get-NetFirewallRule -DisplayName "$RuleName*" -ErrorAction SilentlyContinue)) {
    Step 'Letting phones on your home network reach it (Windows asks for permission once)...'
    $rules = "New-NetFirewallRule -DisplayName '$RuleName (discovery)' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 1900 -Profile Private,Domain | Out-Null; " +
             "New-NetFirewallRule -DisplayName '$RuleName (control)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 49494 -Profile Private,Domain | Out-Null"
    if (-not (Invoke-Admin $rules)) {
        Write-Host '    The firewall wasn''t changed. When Windows asks about Python, allow it on private networks.' -ForegroundColor Yellow
    }
}
$public = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' }
if ($public) {
    Write-Host "    Your network '$($public[0].Name)' is set to Public, so phones can't find this computer." -ForegroundColor Yellow
    Write-Host '    If it''s your home Wi-Fi: Settings > Network & internet > Wi-Fi > (your network) > Private network.' -ForegroundColor Yellow
}

# Start at sign-in
if (-not ($PSBoundParameters -and $PSBoundParameters.ContainsKey('Autostart')) -and -not $Quiet) {
    $answer = Read-Host '  Start Anbled Screen every time you sign in? [Y/n]'
    $Autostart = $answer -notmatch '^\s*[nN]'
}
if ($Autostart) { & $python $script --autostart | Out-Null; Step 'Starts when you sign in' }
else { & $python $script --no-autostart | Out-Null }

if (-not $NoStart) {
    & $python $script --stop | Out-Null   # (an update: the new version takes over)
    & $python $script --start
}

Write-Host ''
Write-Host 'Anbled Screen is installed.' -ForegroundColor Green
Say 'Open "Anbled Screen" from the Start menu. In the Anbled app, tap Cast and choose this computer.'
