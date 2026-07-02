# Unzip and run the Polymarket latency probe on Windows.
# Results are written to data/latency-probe.txt
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BinDir = Join-Path $Root "bin"
$ExtractDir = Join-Path $BinDir ".extracted\windows"
$OutDir = Join-Path $Root "data"
$OutFile = Join-Path $OutDir "latency-probe.txt"

$ZipName = "polymarket-latency-probe-windows-x64.zip"
$BinaryName = "polymarket-latency-probe-windows-x64.exe"
$ZipPath = Join-Path $BinDir $ZipName
$Probe = Join-Path $ExtractDir $BinaryName

New-Item -ItemType Directory -Force -Path $ExtractDir, $OutDir | Out-Null

if ((Test-Path $OutFile) -and ((Get-Date) - (Get-Item $OutFile).LastWriteTimeUtc).TotalMinutes -lt 5) {
    Write-Host "Skipping latency probe; recent results exist at $OutFile"
    exit 0
}

if (-not (Test-Path $Probe)) {
    if (-not (Test-Path $ZipPath)) {
        throw "Missing probe archive: $ZipPath"
    }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
}

$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$HostName = $env:COMPUTERNAME

$Header = @(
    "Polymarket Latency Probe"
    "Generated: $Timestamp"
    "Platform: windows"
    "Binary: $BinaryName"
    "Host: $HostName"
    "---"
)

$ProbeOutput = & $Probe --json -q 2>$null
$Content = ($Header + $ProbeOutput) -join [Environment]::NewLine

$TempFile = "$OutFile.tmp"
Set-Content -Path $TempFile -Value $Content -Encoding UTF8
Move-Item -Path $TempFile -Destination $OutFile -Force

Write-Host "Latency results saved to $OutFile"
