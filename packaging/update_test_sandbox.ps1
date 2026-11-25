# Runs inside Windows Sandbox for packaging/update_test.py: installs the old build, switches it to the
# beta channel, finds the new version, installs it through the app's Install button and checks that
# the new version runs with the setting kept. Writes C:\kltest\out\result.json, then shuts down.
$in = 'C:\kltest\in'
$out = 'C:\kltest\out'
$logFile = Join-Path $out 'log.txt'
$checks = New-Object System.Collections.ArrayList
$appDir = Join-Path $env:LOCALAPPDATA 'Programs\KopiaLens'
$base = 'http://127.0.0.1:7123'
$new = (Get-Content (Join-Path $in 'latest.json') -Raw | ConvertFrom-Json).version

# The host reads this file while the test runs; retry when it holds the file open.
function Log([string]$msg) {
    $line = "$(Get-Date -Format HH:mm:ss) $msg`r`n"
    for ($i = 0; $i -lt 20; $i++) {
        try {
            [IO.File]::AppendAllText($logFile, $line)
            return
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
}

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    [void]$checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    Log ('{0} {1} {2}' -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name, $detail)
}

function Api([string]$method, [string]$path, $body = $null) {
    $params = @{ Method = $method; Uri = "$base$path"; TimeoutSec = 30 }
    if ($null -ne $body) {
        $params.Body = ($body | ConvertTo-Json -Depth 5)
        $params.ContentType = 'application/json'
    }
    Invoke-RestMethod @params
}

# The running server's version, or $null while it is down.
function Version-Now {
    try { (Api 'GET' '/health').version } catch { $null }
}

function Wait-Version([string]$want, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $v = Version-Now
        if ($v -eq $want) { return $true }
        Start-Sleep -Seconds 3
    }
    $false
}

$result = [ordered]@{ ok = $false; checks = $checks; error = $null }
try {
    Log "started as $env:USERNAME; updating to $new"
    Start-Process powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $in 'update_test_server.ps1')) -WindowStyle Hidden
    # The installed app's server reads the check-in address from its environment, inherited from here.
    $env:KOPIALENS_CHECKIN_URL = 'http://127.0.0.1:8765/v1/checkin'
    $p = Start-Process -FilePath (Join-Path $in 'old-setup.exe') -ArgumentList @('/S', '/CURRENTUSER') -PassThru
    $p.WaitForExit()
    Check 'old version installs' ($p.ExitCode -eq 0) "exit $($p.ExitCode)"
    Start-Process -FilePath (Join-Path $appDir 'KopiaLens.exe') -ArgumentList '--hidden'
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline -and -not (Version-Now)) { Start-Sleep -Seconds 3 }
    $old = Version-Now
    Check 'old version starts' ([bool]$old) "version $old"

    [void](Api 'POST' '/api/settings' @{ kopialens = @{ update_channel = 'beta' } })
    $check = Api 'POST' '/api/help/check-updates'
    $u = $check.update
    Check 'the new version is offered' ($check.ok -and $u.available -and $u.latest -eq $new) "available=$($u.available) latest=$($u.latest)"
    Check 'it can install the update itself' ([bool]$u.can_install)
    Check 'the beta channel is in use' ($u.channel -eq 'beta') "channel=$($u.channel)"

    [void](Api 'POST' '/api/help/install-update')
    Log 'install requested; waiting for the new version'
    Check 'the new version runs after the update' (Wait-Version $new 600) "version $(Version-Now)"
    $info = Api 'GET' '/api/help/info'
    Check 'the update channel setting is kept' ($info.update.channel -eq 'beta') "channel=$($info.update.channel)"
    $served = Get-Content (Join-Path $out 'server.txt') -ErrorAction SilentlyContinue
    Check 'the updater asked for the beta channel' ([bool]($served | Where-Object { $_ -match '/v1/update/.*channel=beta' })) (($served | Where-Object { $_ -match '/v1/update/' }) -join ' | ')
    Check 'the update was downloaded from the update service' ([bool]($served | Where-Object { $_ -match '/files/new-setup.exe' }))
    $result.ok = ($checks.Count -gt 0) -and -not ($checks | Where-Object { -not $_.ok })
} catch {
    $result.error = $_.ToString()
    Log "ERROR $($_.ToString())"
} finally {
    Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000 } -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Message } | Set-Content -Path (Join-Path $out 'crashes.txt')
    $logs = New-Item -ItemType Directory -Force (Join-Path $out 'app-logs')
    Get-ChildItem (Join-Path $env:LOCALAPPDATA 'KopiaLens') -Recurse -Filter *.log -ErrorAction SilentlyContinue |
        Copy-Item -Destination $logs -Force -ErrorAction SilentlyContinue
    $tmp = Join-Path $out 'result.json.tmp'
    $result | ConvertTo-Json -Depth 5 | Set-Content -Path $tmp -Encoding UTF8
    Move-Item -Force $tmp (Join-Path $out 'result.json')
    Start-Sleep -Seconds 2
    Stop-Computer -Force
}
