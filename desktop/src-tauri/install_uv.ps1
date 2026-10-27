# Installer phase: download uv (runtime for MCP servers started via uvx) into the app folder.
# Called from installer-hooks.nsh after the files are copied. Never blocks installation:
# on any failure it prints the reason and exits 1, the installer continues.
# The version and SHA-256 are pinned: a different file is rejected, nothing is executed from the net.
param([Parameter(Mandatory = $true)][string]$Dest)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Version = '0.13.5'
$Sha256 = 'a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a'
$Url = "https://github.com/astral-sh/uv/releases/download/$Version/uv-x86_64-pc-windows-msvc.zip"

$zip = Join-Path $env:TEMP "aria-uv-$Version.zip"
$tmp = Join-Path $env:TEMP "aria-uv-$Version"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (Test-Path (Join-Path $Dest 'uvx.exe')) { Write-Output "uv already installed in $Dest"; exit 0 }
    Write-Output "Downloading uv $Version ..."
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $zip -TimeoutSec 180
    $actual = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $Sha256) { throw "SHA-256 mismatch: $actual" }
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item (Join-Path $tmp 'uv.exe'), (Join-Path $tmp 'uvx.exe') -Destination $Dest -Force
    Write-Output "uv $Version installed to $Dest"
    exit 0
}
catch {
    Write-Output "uv was not installed: $($_.Exception.Message)"
    exit 1
}
finally {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
