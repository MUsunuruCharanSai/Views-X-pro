# Export cookies.txt as Base64 for Railway variable YTDLP_COOKIES_BASE64
# 1) Install browser extension "Get cookies.txt LOCALLY"
# 2) Sign into YouTube with a THROWAWAY Google account
# 3) Export cookies.txt into this project folder
# 4) Run:  powershell -File scripts\export_cookies_base64.ps1

$cookiePath = Join-Path (Split-Path $PSScriptRoot -Parent) "cookies.txt"
if (-not (Test-Path $cookiePath)) {
    Write-Error "Missing cookies.txt at $cookiePath"
    exit 1
}

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($cookiePath))
Write-Host ""
Write-Host "Copy this into Railway → Variables → YTDLP_COOKIES_BASE64 :"
Write-Host ""
Write-Host $b64
Write-Host ""
Set-Clipboard -Value $b64 -ErrorAction SilentlyContinue
Write-Host "(Also copied to clipboard if available)"
