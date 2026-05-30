$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Frontend = Join-Path $Root "frontend"
$FrontendPort = 5173
$ApiPort = 8000

function Get-LanIpAddress {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object InterfaceMetric |
        Select-Object -ExpandProperty IPAddress

    if ($addresses) {
        return $addresses[0]
    }

    try {
        return [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object { $_.AddressFamily -eq "InterNetwork" -and $_.IPAddressToString -notlike "127.*" } |
            Select-Object -First 1 -ExpandProperty IPAddressToString
    } catch {
        return "127.0.0.1"
    }
}

function Ensure-FirewallRule($Name, $Port) {
    try {
        $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue

        if (-not $existing) {
            New-NetFirewallRule `
                -DisplayName $Name `
                -Direction Inbound `
                -Action Allow `
                -Protocol TCP `
                -LocalPort $Port `
                -Profile Private `
                | Out-Null
            Write-Host "Allowed Windows Firewall access for port $Port."
        }
    } catch {
        Write-Host "Firewall rule for port $Port was not changed. If another laptop cannot connect, run this launcher as Administrator once."
    }
}

function Test-Url($Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Wait-ForUrl($Url, $Name) {
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        if (Test-Url $Url) {
            Write-Host "$Name is ready."
            return
        }

        Start-Sleep -Milliseconds 500
    }

    Write-Host "$Name is still starting. Opening the app anyway..."
}

Write-Host "Starting database API..."
if (-not (Test-Url "http://127.0.0.1:$ApiPort/api/health")) {
    Start-Process -FilePath python -ArgumentList "api_server.py" -WorkingDirectory $Root -WindowStyle Hidden
    Wait-ForUrl "http://127.0.0.1:$ApiPort/api/health" "Database API"
} else {
    Write-Host "Database API is already running."
}

Write-Host "Starting frontend..."
if (-not (Test-Url "http://127.0.0.1:$FrontendPort")) {
    Start-Process -FilePath npm.cmd -ArgumentList "run", "dev", "--", "--host", "0.0.0.0", "--port", "$FrontendPort" -WorkingDirectory $Frontend -WindowStyle Hidden
    Wait-ForUrl "http://127.0.0.1:$FrontendPort" "Frontend"
} else {
    Write-Host "Frontend is already running."
}

Ensure-FirewallRule "Event ID System Frontend" $FrontendPort
Ensure-FirewallRule "Event ID System API" $ApiPort

$LanIp = Get-LanIpAddress
$LocalUrl = "http://127.0.0.1:$FrontendPort"
$ShareUrl = "http://$LanIp`:$FrontendPort"

try {
    Set-Clipboard -Value $ShareUrl
    Write-Host "Share URL copied to clipboard."
} catch {
    Write-Host "Share URL could not be copied automatically."
}

Write-Host "Opening web app..."
Write-Host ""
Write-Host "Open on this laptop: $LocalUrl"
Write-Host "Open on another laptop on the same Wi-Fi/LAN: $ShareUrl"
Write-Host ""
Start-Process $LocalUrl
