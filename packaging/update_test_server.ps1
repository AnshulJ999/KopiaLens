# The update service for packaging/update_test.py, inside Windows Sandbox: answers the app's check-in
# with the new version, the desktop updater with latest.json, and serves the new installer.
# Every request is logged to C:\kltest\out\server.txt.
$in = 'C:\kltest\in'
$requests = 'C:\kltest\out\server.txt'
$latest = Get-Content (Join-Path $in 'latest.json') -Raw
$version = ($latest | ConvertFrom-Json).version

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add('http://127.0.0.1:8765/')
$listener.Start()
while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    $req, $resp = $ctx.Request, $ctx.Response
    Add-Content -Path $requests -Value ("{0} {1} {2}" -f (Get-Date -Format HH:mm:ss), $req.HttpMethod, $req.Url.PathAndQuery)
    $body, $type = $null, 'application/json'
    $path = $req.Url.AbsolutePath
    if ($path -eq '/v1/checkin') {
        $body = [Text.Encoding]::UTF8.GetBytes((@{ latest = $version; url = $null; notice = $null; latest_by_major = @{} } | ConvertTo-Json))
    } elseif ($path -like '/v1/update/*') {
        $current = [Uri]::UnescapeDataString($path.Split('/')[-1])
        if ($current -ne $version) {
            $body = [Text.Encoding]::UTF8.GetBytes($latest)
        } else {
            $resp.StatusCode = 204
        }
    } elseif ($path -eq '/files/new-setup.exe') {
        $body = [IO.File]::ReadAllBytes((Join-Path $in 'new-setup.exe'))
        $type = 'application/octet-stream'
    } else {
        $resp.StatusCode = 404
    }
    if ($body) {
        $resp.ContentType = $type
        $resp.ContentLength64 = $body.Length
        $resp.OutputStream.Write($body, 0, $body.Length)
    }
    $resp.Close()
}
