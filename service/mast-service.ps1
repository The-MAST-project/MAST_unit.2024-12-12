#Requires -Version 5.1
<#
.SYNOPSIS
    Install or uninstall the MAST Unit Windows service using NSSM.

.PARAMETER Action
    'install' or 'uninstall'

.EXAMPLE
    .\mast-service.ps1 install
    .\mast-service.ps1 uninstall
#>

param(
    [Parameter(Mandatory)]
    [ValidateSet('install', 'uninstall')]
    [string] $Action
)

# ── Self-elevate if not running as admin ─────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Not running as administrator — re-launching elevated..."
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $Action"
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}

# ── Configuration ─────────────────────────────────────────────────────────────
$ServiceName  = "mast-unit"
$DisplayName  = "MAST Unit Service"
$ProjectDir   = "C:\Users\mast\PycharmProjects\MAST_unit.2024-12-12"
$Python       = "$ProjectDir\.venv\Scripts\python.exe"
$AppScript    = "$ProjectDir\src\app.py"
$LogDir       = "C:\MAST\Logs\mast-service"
$StdoutLog    = "$LogDir\stdout.txt"
$StderrLog    = "$LogDir\stderr.txt"
$Nssm         = "C:\Users\mast\Downloads\nssm\nssm-2.24\win64\nssm.exe"
$ServiceUser  = ".\mast"

# ── Helpers ───────────────────────────────────────────────────────────────────
function Invoke-Nssm {
    param([string[]] $Args)
    & $Nssm @Args
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Args -join ' ') failed (exit $LASTEXITCODE)"
    }
}

# ── Install ───────────────────────────────────────────────────────────────────
if ($Action -eq 'install') {

    # Check prerequisites
    if (-not (Test-Path $Nssm))   { throw "nssm not found at $Nssm" }
    if (-not (Test-Path $Python)) { throw "Python venv not found at $Python" }
    if (-not (Test-Path $AppScript)) { throw "app.py not found at $AppScript" }

    # Prompt for mast user password (not echoed)
    $SecurePass = Read-Host "Enter password for user '$ServiceUser'" -AsSecureString
    $PlainPass  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePass))

    # Create log directory
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        Write-Host "Created log directory: $LogDir"
    }

    # Remove existing service if present
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing service '$ServiceName'..."
        Invoke-Nssm 'stop', $ServiceName
        Invoke-Nssm 'remove', $ServiceName, 'confirm'
    }

    Write-Host "Installing service '$ServiceName'..."

    Invoke-Nssm 'install',        $ServiceName, $Python
    Invoke-Nssm 'set',            $ServiceName, 'AppParameters',         $AppScript
    Invoke-Nssm 'set',            $ServiceName, 'AppDirectory',          "$ProjectDir\src"
    Invoke-Nssm 'set',            $ServiceName, 'DisplayName',           $DisplayName
    Invoke-Nssm 'set',            $ServiceName, 'AppStdout',             $StdoutLog
    Invoke-Nssm 'set',            $ServiceName, 'AppStderr',             $StderrLog
    Invoke-Nssm 'set',            $ServiceName, 'AppStdoutCreationDisposition', '4'  # append
    Invoke-Nssm 'set',            $ServiceName, 'AppStderrCreationDisposition', '4'  # append
    Invoke-Nssm 'set',            $ServiceName, 'AppRotateFiles',        '1'
    Invoke-Nssm 'set',            $ServiceName, 'AppRotateOnline',       '1'
    Invoke-Nssm 'set',            $ServiceName, 'AppRotateBytes',        '10485760'  # 10 MB

    # Run as mast user
    Invoke-Nssm 'set',            $ServiceName, 'ObjectName', $ServiceUser, $PlainPass

    # Delayed auto-start
    Invoke-Nssm 'set',            $ServiceName, 'Start', 'SERVICE_DELAYED_AUTO_START'

    # Restart on crash: restart after 5 s, reset failure count after 1 h
    Invoke-Nssm 'set',            $ServiceName, 'AppRestartDelay',       '5000'
    & sc.exe failure $ServiceName reset= 3600 actions= restart/5000/restart/5000/restart/5000 | Out-Null

    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePass))

    Write-Host ""
    Write-Host "Service '$ServiceName' installed successfully."
    Write-Host "  Logs : $LogDir"
    Write-Host "  Start: sc start $ServiceName"
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Action -eq 'uninstall') {

    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Service '$ServiceName' is not installed."
        exit 0
    }

    Write-Host "Stopping service '$ServiceName'..."
    Invoke-Nssm 'stop', $ServiceName

    Write-Host "Removing service '$ServiceName'..."
    Invoke-Nssm 'remove', $ServiceName, 'confirm'

    Write-Host "Service '$ServiceName' removed."
    Write-Host "(Logs in $LogDir were not deleted.)"
}
