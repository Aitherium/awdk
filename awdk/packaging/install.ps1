# AitherADK installer -- irm https://aitherium.com/install.ps1 | iex
$ErrorActionPreference = "Stop"

Write-Host "AitherADK Installer" -ForegroundColor Cyan
Write-Host "==================="
Write-Host

# Check Python
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        $major = & $cmd -c "import sys; print(sys.version_info.major)" 2>$null
        $minor = & $cmd -c "import sys; print(sys.version_info.minor)" 2>$null
        if ([int]$major -ge 3 -and [int]$minor -ge 10) {
            $python = $cmd
            Write-Host "[OK] Python $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $python) {
    Write-Host "[!!] Python >= 3.10 required" -ForegroundColor Yellow
    Write-Host
    # Try uv
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Write-Host "Installing Python via uv..."
        uv python install 3.12
        $python = "uv run python"
    } else {
        Write-Host "Installing uv first..."
        irm https://astral.sh/uv/install.ps1 | iex
        uv python install 3.12
        $python = "uv run python"
    }
}

# Install awdk
Write-Host
Write-Host "Installing awdk..."
& $python -m pip install --upgrade awdk

Write-Host
Write-Host "[OK] awdk installed" -ForegroundColor Green
Write-Host

# Run doctor
try {
    adk doctor
} catch {
    & $python -m adk.cli doctor 2>$null
}

Write-Host
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  adk init my-agent    # Create an agent"
Write-Host "  adk setup            # Set up GPU inference"
Write-Host "  adk doctor           # Check system health"
Write-Host
