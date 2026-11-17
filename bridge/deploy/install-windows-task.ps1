# Registers the bridge to start at logon on Windows (run in an elevated PowerShell
# from the bridge folder after creating .venv and .env).
$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$python = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "Create the venv first: py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }

$action   = New-ScheduledTaskAction -Execute $python -Argument "run_bridge.py" -WorkingDirectory $here
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Days 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "WindsorGlanceBridge" -Action $action -Trigger $trigger -Settings $settings -Force

# Allow the badge to reach port 8000 on the private network only
New-NetFirewallRule -DisplayName "Windsor Glance Bridge" -Direction Inbound -Protocol TCP -LocalPort 8000 `
    -Profile Private -Action Allow -ErrorAction SilentlyContinue | Out-Null

Start-ScheduledTask -TaskName "WindsorGlanceBridge"
Write-Host "Started. Test: curl.exe -H 'X-Badge-Key: <key>' http://<lan-ip>:8000/api/v1/glance"
