param(
    [string]$PythonCommand = "python",
    [switch]$SkipBrowser,
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "This tool uses Windows current-user DPAPI." }
$sourceRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $env:LOCALAPPDATA "OmgtuStudent"
$runtimePython = Join-Path $runtimeDir "venv\Scripts\python.exe"

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $runtimePython @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python step failed (exit $LASTEXITCODE)." }
}

& $PythonCommand -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required." }
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & $PythonCommand -m venv (Join-Path $runtimeDir "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the private runtime." }
}

Invoke-CheckedPython -Arguments @("-m", "pip", "install", "-r", (Join-Path $sourceRoot "requirements.lock.txt"))
Invoke-CheckedPython -Arguments @("-m", "pip", "install", "--no-deps", "--no-build-isolation", $sourceRoot)
if (-not $SkipBrowser) {
    Invoke-CheckedPython -Arguments @("-m", "playwright", "install", "chromium")
}

$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$skillSource = Join-Path $sourceRoot "skills\omgtu-student"
$skillTarget = Join-Path $codexRoot "skills\omgtu-student"
New-Item -ItemType Directory -Force -Path $skillTarget | Out-Null
Copy-Item -Path (Join-Path $skillSource "*") -Destination $skillTarget -Recurse -Force

if (-not $SkipMcp) {
    $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if ($codexCommand) {
        & $codexCommand.Source mcp add omgtu_student -- $runtimePython -X utf8 -m omgtu_student serve
        if ($LASTEXITCODE -ne 0) { throw "MCP registration failed; the skill and runtime are installed." }
    } else {
        Write-Warning "Codex CLI was not found. Add a local STDIO MCP server in Codex settings using:"
        Write-Output ('"{0}" -X utf8 -m omgtu_student serve' -f $runtimePython)
    }
}

Write-Output "Installed omgtu-student. Reload Codex tools/skills if necessary."
Write-Output "Next: enter credentials yourself in the separate login window."
Write-Output ('& "{0}" -X utf8 -m omgtu_student auth login' -f $runtimePython)
Write-Output "If Chromium cannot open on this Windows setup, add: --channel msedge"
