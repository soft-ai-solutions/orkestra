# Orkestra Windows entry: forwards every argument to install.py (the real installer).
#
#   .\install.ps1                      install
#   .\install.ps1 --dry-run            print the plan, change nothing
#   .\install.ps1 --uninstall [--purge]
#   .\install.ps1 --yes --with-tests     (the ~10 minute test suites are skipped unless --with-tests)
#
# This file is plain ASCII on purpose: Windows PowerShell 5.1 reads a script without a byte order mark in the ANSI
# code page, so Turkish letters are written as \u escapes and expanded with [regex]::Unescape.
# Python is looked up as: the py launcher, python, python3. A candidate must really run Python 3.9 or newer, which
# skips the Microsoft Store stub (python.exe / python3.exe that only opens the Store).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-Lang {
    # Same order as orkestra: ORKESTRA_LANG, then the locale (LC_ALL, LC_MESSAGES, LANG), then the Windows UI language.
    $explicit = "$env:ORKESTRA_LANG".Trim().ToLower()
    if ($explicit -and $explicit -ne "auto") { if ($explicit -like "tr*" -or $explicit -eq "turkish") { return "tr" } else { return "en" } }
    foreach ($v in @($env:LC_ALL, $env:LC_MESSAGES, $env:LANG)) {
        if ($v) { if ($v.ToLower() -like "tr*") { return "tr" } elseif ($v.ToLower() -like "en*") { return "en" } }
    }
    try { if ((Get-UICulture).TwoLetterISOLanguageName -eq "tr") { return "tr" } } catch { }
    return "en"
}

function Say([string]$en, [string]$tr) {
    if ((Get-Lang) -eq "tr") { return [regex]::Unescape($tr) }
    return $en
}

function Test-Python([string]$exe, [string[]]$pre) {
    # True when `exe` runs and is Python 3.9+. A failing stub or a missing command is simply "no".
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $old
    }
}

$Candidates = @(
    @{ Exe = "py"; Pre = @("-3") },
    @{ Exe = "python"; Pre = @() },
    @{ Exe = "python3"; Pre = @() }
)
$Chosen = $null
foreach ($c in $Candidates) {
    if ((Get-Command $c.Exe -ErrorAction SilentlyContinue) -and (Test-Python $c.Exe $c.Pre)) { $Chosen = $c; break }
}
if (-not $Chosen) {
    [Console]::Error.WriteLine((Say `
        "Python 3.9 or newer was not found (tried py -3, python, python3). Install it with: winget install Python.Python.3.12 (or from https://www.python.org/downloads/), then open a new terminal and run this again." `
        'Python 3.9 veya \u00fcst\u00fc bulunamad\u0131 (py -3, python, python3 denendi). \u015eununla kurun: winget install Python.Python.3.12 (ya da https://www.python.org/downloads/), sonra yeni bir terminal a\u00e7\u0131p tekrar \u00e7al\u0131\u015ft\u0131r\u0131n.'))
    exit 2
}

# install.py prints UTF-8 when its output is not a console: read it as UTF-8 here too.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
& $Chosen.Exe @($Chosen.Pre) "$Root\install.py" @args
exit $LASTEXITCODE
