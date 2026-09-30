<#
.SYNOPSIS
    Install wechat-read as an agent Skill.

.DESCRIPTION
    Copies this project into your agent's skills directory and installs
    dependencies. Nothing is written to the registry or to PATH - to
    uninstall, just delete the folder.

    The agent finds SKILL.md in the skills dir, and runs the bundled
    wechat-read.cmd next to it, using the absolute path it already knows.

.PARAMETER Dest
    Install location. Defaults to the skills dir of the agent you are using
    (auto-detected), as <skills dir>\wechat-read.

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -Dest "D:\config\skills\wechat-read"
#>
[CmdletBinding()]
param([string]$Dest)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$SkillName = 'wechat-read'

function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }

# ---- Where do skills live for the agent you use? ----
function Find-SkillsDir {
    # Any of these that already exists wins - don't invent directories for
    # agents the user does not have.
    $known = @(
        (Join-Path $env:USERPROFILE '.claude\skills'),
        (Join-Path $env:USERPROFILE '.codex\skills'),
        (Join-Path $env:USERPROFILE '.agents\skills'),
        (Join-Path $env:USERPROFILE 'config\skills'),
        (Join-Path $env:USERPROFILE '.cursor\skills')
    )
    foreach ($d in $known) { if (Test-Path $d) { return $d } }
    return $null
}

if (-not $Dest) {
    $skills = Find-SkillsDir
    if ($skills) {
        $Dest = Join-Path $skills $SkillName
    } else {
        Warn "No agent skills directory found."
        Write-Host "      Tell me where to put it instead:" -ForegroundColor Yellow
        Write-Host "        .\install.ps1 -Dest `"<your agent's skills dir>\$SkillName`"" -ForegroundColor Yellow
        exit 1
    }
}

# ---- Drop a stale install left by an earlier version ----
# The skill used to install as "wechat". Two skills with overlapping
# descriptions means the agent may trigger the wrong one.
$oldDir = Join-Path (Split-Path $Dest -Parent) 'wechat'
if ((Test-Path $oldDir) -and ($oldDir -ne $Dest)) {
    Remove-Item -Recurse -Force $oldDir
    Ok "Removed old install at $oldDir"
}

Write-Host "`nInstalling $SkillName`n" -ForegroundColor Cyan

# ---- 1. Find Python ----
Write-Host "[1/3] Finding Python..."
$Py = $null

# py launcher is the most reliable, and never hits the WindowsApps stub
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -c "pass" 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = 'py -3' }
}

# then python.exe on PATH, skipping the WindowsApps stub (a 0-byte alias
# that silently does nothing - the single most common Windows Python trap)
if (-not $Py) {
    foreach ($p in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($p.Source -and $p.Source -notlike '*WindowsApps*') {
            & $p.Source -c "pass" 2>$null
            if ($LASTEXITCODE -eq 0) { $Py = $p.Source; break }
        }
    }
}

# finally the usual install locations
if (-not $Py) {
    $hit = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                          "$env:ProgramFiles\Python3*\python.exe" `
                          -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $Py = $hit.FullName }
}

if (-not $Py) { Die "Python not found. Install Python 3.10+ from python.org first." }
Ok "Python: $Py"

# ---- 2. Copy the project into the skills dir ----
Write-Host "`n[2/3] Installing to $Dest ..."
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force $Dest | Out-Null

$skip = @('.venv','venv','.git','__pycache__','.claude','scratch_probe','reference')
$xd = @(); foreach ($d in $skip) { $xd += @('/XD', (Join-Path $Root $d)) }
robocopy $Root $Dest /E /NFL /NDL /NJH /NJS /NP @xd /XF '*.pyc' 'keys.json' | Out-Null
if ($LASTEXITCODE -ge 8) { Die "Copy failed." }

# Remember the interpreter so wechat-read.cmd never has to search for it.
if ($Py -ne 'py -3') { $Py | Out-File "$Dest\python-path.txt" -Encoding ascii }
Ok "SKILL.md, cli.py and wechat-read.cmd are all in that folder"

# ---- 3. Dependencies ----
Write-Host "`n[3/3] Installing dependencies..."
& $Py -m pip install -q -r "$Dest\requirements.txt"
if ($LASTEXITCODE -ne 0) { Warn "Failed. Run manually: pip install -r $Dest\requirements.txt" }
else { Ok "Done" }

Write-Host "`nInstalled. Nothing was added to PATH - to uninstall, just delete:`n" -ForegroundColor Green
Write-Host "  $Dest`n"
Write-Host "The agent now finds the skill and runs the bundled launcher:"
Write-Host "  & `"$Dest\wechat-read.cmd`" status`n"
