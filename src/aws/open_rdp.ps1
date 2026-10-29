# Configuration
$filePath = "./ips.json"
$user = "Administrator"
# The password is never stored in this file: set HERA_RDP_PASSWORD or type it when asked.
$pass = $env:HERA_RDP_PASSWORD
if (-not $pass) {
    $securePass = Read-Host "Administrator password" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass)
    try { $pass = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

if (-not (Test-Path $filePath)) {
    Write-Error "File not found at: $filePath"
    exit
}

try {
    # Read the file and extract IPv4 addresses
    $rawText = Get-Content -Raw -Path $filePath
    $ipMatches = [regex]::Matches($rawText, '\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')

    if ($ipMatches.Count -eq 0) {
        Write-Warning "No IP addresses were found in the file."
        exit
    }

    Write-Host "Found $($ipMatches.Count) IPs. Adding credentials and launching RDP..." -ForegroundColor Cyan

    foreach ($match in $ipMatches) {
        $ip = $match.Value
        Write-Host "Processing: $ip" -ForegroundColor Green
        
        # 1. Save credentials to Windows Credential Manager
        # This allows mstsc to log in automatically
        cmdkey /add:$ip /user:$user /pass:$pass | Out-Null
        
        # 2. Launch RDP
        # /v: server address
        Start-Process "mstsc.exe" -ArgumentList "/v:$ip"
        
        # Small delay to ensure windows open in order
        Start-Sleep -Milliseconds 500
    }

    Write-Host "`nAll windows opened." -ForegroundColor Yellow
    Write-Host "Note: Credentials remain in Credential Manager for these IPs." -ForegroundColor Gray
}
catch {
    Write-Error "An unexpected error occurred: $_"
}