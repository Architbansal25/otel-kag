# Downloads the two standalone binaries the demo needs. No admin, no installers.
#   1. OpenTelemetry Java agent  -> vendor/opentelemetry-javaagent.jar
#   2. Jaeger all-in-one (v1)    -> vendor/jaeger/jaeger-all-in-one.exe
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root   = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

# --- 1. OTel Java agent (stable "latest" redirect URL) ---------------------
$agent = Join-Path $vendor "opentelemetry-javaagent.jar"
if (Test-Path $agent) {
    Write-Host "[skip] OTel agent already present"
} else {
    Write-Host "[..]   Downloading OpenTelemetry Java agent"
    Invoke-WebRequest -Uri "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar" -OutFile $agent
    Write-Host "[ok]   OTel agent -> $agent ($([math]::Round((Get-Item $agent).Length/1MB,1)) MB)"
}

# --- 2. Jaeger all-in-one --------------------------------------------------
$jaegerDir = Join-Path $vendor "jaeger"
$jaegerExe = Join-Path $jaegerDir "jaeger-all-in-one.exe"
if (Test-Path $jaegerExe) {
    Write-Host "[skip] Jaeger already present"
} else {
    Write-Host "[..]   Resolving latest Jaeger 1.x release"
    $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/jaegertracing/jaeger/releases?per_page=60" `
                                  -Headers @{ "User-Agent" = "otel-kag-demo" }
    # Pin to the v1 line: its /api/traces query API is what the KAG graph builder reads.
    $rel = $releases | Where-Object { $_.tag_name -like "v1.*" -and -not $_.prerelease } |
           Sort-Object { [version]($_.tag_name.TrimStart("v")) } -Descending | Select-Object -First 1
    if (-not $rel) { throw "Could not find a Jaeger v1 release" }

    $asset = $rel.assets | Where-Object { $_.name -like "*windows-amd64.tar.gz" } | Select-Object -First 1
    if (-not $asset) { throw "No windows-amd64 asset on Jaeger $($rel.tag_name)" }

    Write-Host "[..]   Downloading Jaeger $($rel.tag_name) ($($asset.name))"
    $tgz = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tgz

    New-Item -ItemType Directory -Force -Path $jaegerDir | Out-Null
    tar -xzf $tgz -C $jaegerDir --strip-components=1
    Remove-Item $tgz -Force

    if (-not (Test-Path $jaegerExe)) { throw "jaeger-all-in-one.exe not found after extraction" }
    Write-Host "[ok]   Jaeger $($rel.tag_name) -> $jaegerExe"
}

Write-Host ""
Write-Host "All dependencies ready in $vendor"
