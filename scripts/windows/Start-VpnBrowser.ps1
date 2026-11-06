# Starts an isolated browser profile through the existing local Xray SOCKS proxy.
# Run as the signed-in Windows user; administrator privileges are unnecessary.
[CmdletBinding()]
param(
    [ValidateSet('Edge', 'Chrome')][string]$Browser = 'Edge',
    [ValidateRange(1, 65535)][int]$SocksPort = 10808,
    [string]$ExpectedExitIp = '89.125.210.172'
)

$ErrorActionPreference = 'Stop'
$parsedAddress = $null
if (-not [System.Net.IPAddress]::TryParse($ExpectedExitIp, [ref]$parsedAddress)) {
    throw 'ExpectedExitIp must be a valid IP address.'
}

$curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (-not (Test-Path -LiteralPath $curl)) { throw 'Windows curl.exe is unavailable.' }

$executables = if ($Browser -eq 'Edge') {
    @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )
} else {
    @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
}
$executable = $executables | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $executable) { throw "$Browser is not installed in a supported location." }

# --noproxy= prevents an inherited NO_PROXY value from bypassing SOCKS.
# TLS verification remains enabled; DNS for this request is resolved via SOCKS.
$result = & $curl --silent --show-error --fail --connect-timeout 5 --max-time 15 `
    '--noproxy=' --proxy "socks5h://127.0.0.1:$SocksPort" 'https://api.ipify.org'
if ($LASTEXITCODE -ne 0) {
    throw 'Xray proxy is unavailable. Start Xray using its configured hidden task and retry.'
}
if (($result | Out-String).Trim() -ne $ExpectedExitIp) {
    throw 'The observed exit IP differs from ExpectedExitIp. Browser launch aborted.'
}

# A separate profile prevents a running ordinary browser from ignoring new flags.
# Do not launch this profile through any other shortcut without the SOCKS flags.
$profile = Join-Path $env:LOCALAPPDATA "NodeRental\VpnBrowser\$Browser"
$arguments = @(
    ('--user-data-dir="{0}"' -f $profile),
    "--proxy-server=socks5://127.0.0.1:$SocksPort",
    '--disable-quic',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--no-first-run',
    'https://api.ipify.org'
)
Start-Process -FilePath $executable -ArgumentList $arguments
Write-Host "$Browser launched through the verified SOCKS proxy. Only this browser profile is covered."
