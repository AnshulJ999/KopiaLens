# Runs inside Windows Sandbox for packaging/installer_test.py: the installer's PATH edits on PATHs
# longer than an NSIS string, in both install modes, and closing a running copy without touching an
# unrelated program of the same name. Writes C:\kltest\out\result.json, then shuts the Sandbox down.
$in = 'C:\kltest\in'
$out = 'C:\kltest\out'
$setup = Join-Path $in 'setup.exe'
$logFile = Join-Path $out 'log.txt'
$checks = New-Object System.Collections.ArrayList
$userDir = Join-Path $env:LOCALAPPDATA 'Programs\KopiaLens'
$allDir = Join-Path $env:ProgramFiles 'KopiaLens'

function Log([string]$msg) {
    Add-Content -Path $logFile -Value "$(Get-Date -Format HH:mm:ss) $msg"
}

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    [void]$checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    Log ('{0} {1} {2}' -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name, $detail)
}

function EnvKey([string]$scope, [bool]$write = $false) {
    if ($scope -eq 'user') {
        return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $write)
    }
    [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $write)
}

# PATH exactly as stored (unexpanded), with its registry type.
function Read-Path([string]$scope) {
    $k = EnvKey $scope
    try {
        $value = [string]$k.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
        $kind = if ($k.GetValueNames() -contains 'Path') { [string]$k.GetValueKind('Path') } else { 'missing' }
    } finally {
        $k.Close()
    }
    [pscustomobject]@{ value = $value; kind = $kind }
}

# Makes PATH longer than an NSIS string (1024 characters), keeping its entries and type.
function Grow-Path([string]$scope) {
    $now = Read-Path $scope
    $pad = (1..60 | ForEach-Object { 'C:\kltest\pad\{0}-a-folder-name-long-enough' -f $_ }) -join ';'
    $value = if ($now.value) { $now.value.TrimEnd(';') + ';' + $pad } else { $pad }
    $kind = if ($now.kind -eq 'missing') { 'ExpandString' } else { $now.kind }
    $k = EnvKey $scope $true
    try {
        $k.SetValue('Path', $value, [Microsoft.Win32.RegistryValueKind]$kind)
    } finally {
        $k.Close()
    }
}

function Run-Program([string]$file, [string[]]$arguments) {
    $p = Start-Process -FilePath $file -ArgumentList $arguments -PassThru
    $p.WaitForExit()
    Log "$(Split-Path $file -Leaf) $($arguments -join ' ') -> exit $($p.ExitCode)"
    $p.ExitCode
}

function Run-Setup([string[]]$arguments) {
    Run-Program $setup $arguments
}

# _?= runs the uninstaller in place, so waiting covers it (it leaves uninstall.exe behind).
function Run-Uninstall([string]$dir) {
    Run-Program (Join-Path $dir 'uninstall.exe') @('/S', "_?=$dir")
}

function Same-Path([string]$name, [string]$scope, $want) {
    $got = Read-Path $scope
    $ok = ($got.value -ceq $want.value) -and ($got.kind -eq $want.kind)
    Check $name $ok ('{0} PATH {1} chars ({2}), expected {3} chars ({4})' -f $scope, $got.value.Length, $got.kind, $want.value.Length, $want.kind)
}

function With-Bin($before, [string]$dir) {
    [pscustomobject]@{ value = $before.value.TrimEnd(';') + ';' + (Join-Path $dir 'bin'); kind = $before.kind }
}

function Test-Mode([string]$mode, [string]$flag, [string]$dir, [string]$scope, [string]$other) {
    $before = Read-Path $scope
    $otherBefore = Read-Path $other
    Check "$mode install exits 0" ((Run-Setup @('/S', $flag)) -eq 0)
    Same-Path "$mode install adds bin to PATH" $scope (With-Bin $before $dir)
    Same-Path "$mode install leaves the other PATH alone" $other $otherBefore
    Check "$mode reinstall exits 0" ((Run-Setup @('/S')) -eq 0)
    Same-Path "$mode reinstall adds no second entry" $scope (With-Bin $before $dir)
    Check "$mode update exits 0" ((Run-Setup @('/P', '/UPDATE')) -eq 0)
    Same-Path "$mode update keeps PATH" $scope (With-Bin $before $dir)
    Check "$mode uninstall exits 0" ((Run-Uninstall $dir) -eq 0)
    Same-Path "$mode uninstall restores PATH exactly" $scope $before
    Same-Path "$mode uninstall leaves the other PATH alone" $other $otherBefore
}

function Test-NoPath {
    $before = Read-Path 'user'
    Check 'no-PATH install exits 0' ((Run-Setup @('/S', '/CURRENTUSER', '/NOPATH')) -eq 0)
    Same-Path 'no-PATH install leaves PATH alone' 'user' $before
    [void](Run-Uninstall $userDir)
    Same-Path 'no-PATH uninstall leaves PATH alone' 'user' $before
}

function Running-From([string]$dir) {
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) })
}

# An upgrade must close the installed copy and leave another program with the same name running.
function Test-CloseRunning {
    Check 'install for the closing test exits 0' ((Run-Setup @('/S', '/CURRENTUSER')) -eq 0)
    # Decoy: the same exe from another folder. With no server beside it, it shows an error and stays open.
    $decoyDir = 'C:\kltest\decoy'
    New-Item -ItemType Directory -Force $decoyDir | Out-Null
    Copy-Item (Join-Path $userDir 'KopiaLens.exe') $decoyDir
    $env:KOPIALENS_SINGLE_INSTANCE = '0'
    $decoy = Start-Process -FilePath (Join-Path $decoyDir 'KopiaLens.exe') -PassThru
    Remove-Item Env:KOPIALENS_SINGLE_INSTANCE
    Start-Process -FilePath (Join-Path $userDir 'KopiaLens.exe') -ArgumentList '--hidden'
    $serverDir = Join-Path $userDir 'server'
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline -and (Running-From $serverDir).Count -eq 0) {
        Start-Sleep -Seconds 2
    }
    Check 'installed app starts its server' ((Running-From $serverDir).Count -gt 0)
    Check 'upgrade over the running app exits 0' ((Run-Setup @('/S')) -eq 0)
    Check 'upgrade closed the installed app' ((Running-From $userDir).Count -eq 0)
    $decoy.Refresh()
    Check 'upgrade left the other copy running' (-not $decoy.HasExited)
    if (-not $decoy.HasExited) {
        $decoy.Kill()
    }
    [void](Run-Uninstall $userDir)
}

$result = [ordered]@{ ok = $false; checks = $checks; error = $null }
try {
    Log "started as $env:USERNAME"
    Grow-Path 'user'
    Grow-Path 'machine'
    Log ('PATH lengths: user {0}, machine {1}' -f (Read-Path 'user').value.Length, (Read-Path 'machine').value.Length)
    Test-Mode 'just-me' '/CURRENTUSER' $userDir 'user' 'machine'
    Test-Mode 'everyone' '/ALLUSERS' $allDir 'machine' 'user'
    Test-NoPath
    Test-CloseRunning
    $result.ok = ($checks.Count -gt 0) -and -not ($checks | Where-Object { -not $_.ok })
} catch {
    $result.error = $_.ToString()
    Log "ERROR $($_.ToString())"
} finally {
    $tmp = Join-Path $out 'result.json.tmp'
    $result | ConvertTo-Json -Depth 5 | Set-Content -Path $tmp -Encoding UTF8
    Move-Item -Force $tmp (Join-Path $out 'result.json')
    Start-Sleep -Seconds 2
    Stop-Computer -Force
}
