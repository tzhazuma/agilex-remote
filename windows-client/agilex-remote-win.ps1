# agilex-remote-win.ps1 - PowerShell entry point for the AgileX Windows client.
#
# It only locates Git Bash and forwards every argument to
# agilex-remote-win.sh. No robot command is implemented here.
#
# Usage:
#   .\agilex-remote-win.ps1                 # start
#   .\agilex-remote-win.ps1 status
#   .\agilex-remote-win.ps1 check --strict
#   .\agilex-remote-win.ps1 stop
#
# If Git Bash is not on PATH, set AGILEX_GIT_BASH to bash.exe.

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    [Console]::Error.WriteLine("[agilex-win] ERROR: $Message")
    exit 127
}

# Make Chinese paths/messages readable in the Windows console where possible.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
    # Older hosts or redirected output: keep the default encoding.
}

$scriptDir = $PSScriptRoot
$script = Join-Path $scriptDir 'agilex-remote-win.sh'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    Fail "agilex-remote-win.sh not found next to this script: $script"
}

$candidates = New-Object System.Collections.Generic.List[string]
if ($env:AGILEX_GIT_BASH) {
    $candidates.Add($env:AGILEX_GIT_BASH)
}
if ($env:ProgramFiles) {
    $candidates.Add((Join-Path $env:ProgramFiles 'Git\bin\bash.exe'))
}
if (${env:ProgramFiles(x86)}) {
    $candidates.Add((Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe'))
}
if ($env:LOCALAPPDATA) {
    $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe'))
}

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($git) {
    $gitRoot = Split-Path (Split-Path $git.Source -Parent) -Parent
    $candidates.Add((Join-Path $gitRoot 'bin\bash.exe'))
}

$bashCommand = Get-Command bash.exe -ErrorAction SilentlyContinue
if ($bashCommand) {
    $candidates.Add($bashCommand.Source)
}

$bash = $null
foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    # WSL's bash.exe cannot run this script (it sees a different filesystem).
    if ($candidate -match '\\System32\\bash\.exe$') { continue }
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $bash = $candidate
        break
    }
}

if (-not $bash) {
    Fail "Git Bash not found. Install Git for Windows, or set AGILEX_GIT_BASH to C:\Program Files\Git\bin\bash.exe"
}

try {
    $version = (& $bash --version 2>&1 | Select-Object -First 1)
} catch {
    Fail "cannot execute Git Bash at $bash"
}
if ($version -notmatch 'GNU bash') {
    Fail "the selected bash does not look like Git Bash: $bash ($version)"
}

# $args keeps options such as --host intact; a param() block would try to bind
# them as PowerShell parameters.
& $bash $script @args
exit $LASTEXITCODE
