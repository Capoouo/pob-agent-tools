<#
.SYNOPSIS
    One-click installer and updater for pob-agent-tools (standalone Windows x64 `pob.exe`).

.EXAMPLE
    irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex

.EXAMPLE
    $env:POB_PROXY = "http://127.0.0.1:7890"
    irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]$Version = $env:POB_VERSION,
    [string]$InstallDir = $env:POB_INSTALL_DIR,
    [string]$Proxy = $(if ($env:POB_PROXY) { $env:POB_PROXY } elseif ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } else { $env:HTTP_PROXY }),
    [string]$Mirror = $env:POB_GITHUB_MIRROR,
    [switch]$NoPath
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "exilemoulei/pob-agent-tools"
if (-not $InstallDir) {
    $baseLocal = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME ".pob-agent-tools" }
    $InstallDir = Join-Path $baseLocal "pob-agent-tools\bin"
}

function Apply-Mirror([string]$Url) {
    if (-not $Mirror) { return $Url }
    $prefix = $Mirror.TrimEnd("/")
    return "$prefix/$Url"
}

function Resolve-LatestTag([string]$ProxyUrl) {
    $latestUrl = Apply-Mirror "https://github.com/$Repo/releases/latest"
    $req = [System.Net.HttpWebRequest]::Create($latestUrl)
    $req.Method = "GET"
    $req.AllowAutoRedirect = $false
    $req.UserAgent = "pob-installer/1.0"
    $req.Timeout = 15000
    if ($ProxyUrl) {
        $req.Proxy = New-Object System.Net.WebProxy($ProxyUrl)
    }
    try {
        $resp = [System.Net.HttpWebResponse]$req.GetResponse()
    } catch [System.Net.WebException] {
        $resp = [System.Net.HttpWebResponse]$_.Exception.Response
        if (-not $resp) { throw }
    }
    try {
        $location = $resp.Headers["Location"]
    } finally {
        $resp.Close()
    }
    if ($location -and $location -match "/releases/tag/(v[^/\s\?#]+)") {
        return $Matches[1]
    }
    throw "Could not resolve latest release tag from $latestUrl (Location: $location)."
}

function Download-File([string]$Url, [string]$OutFile, [string]$ProxyUrl) {
    $iwrParams = @{
        Uri     = $Url
        OutFile = $OutFile
        Headers = @{ "User-Agent" = "pob-installer/1.0" }
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $iwrParams["UseBasicParsing"] = $true
    }
    if ($ProxyUrl) {
        $iwrParams["Proxy"] = $ProxyUrl
    }
    Invoke-WebRequest @iwrParams
}

if ([Environment]::Is64BitOperatingSystem -eq $false) {
    throw "pob-agent-tools standalone binary requires 64-bit Windows (x64)."
}

if ($Version) {
    $Tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
} else {
    Write-Host "Resolving latest release from github.com/$Repo ..."
    $Tag = Resolve-LatestTag -ProxyUrl $Proxy
}

$AssetName = "pob-$Tag-windows-x64.zip"
$ZipUrl = Apply-Mirror "https://github.com/$Repo/releases/download/$Tag/$AssetName"
$SumsUrl = Apply-Mirror "https://github.com/$Repo/releases/download/$Tag/SHA256SUMS.txt"

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pob-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

try {
    $ZipPath = Join-Path $TempDir $AssetName
    $SumsPath = Join-Path $TempDir "SHA256SUMS.txt"
    $StageDir = Join-Path $TempDir "stage"

    Write-Host "Downloading $AssetName ($Tag) ..."
    Download-File -Url $ZipUrl -OutFile $ZipPath -ProxyUrl $Proxy

    Write-Host "Downloading SHA256SUMS.txt and verifying checksum ..."
    Download-File -Url $SumsUrl -OutFile $SumsPath -ProxyUrl $Proxy

    $expectedHash = $null
    foreach ($line in Get-Content -LiteralPath $SumsPath -Encoding UTF8) {
        if ($line -match "^\s*([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($AssetName))\s*$") {
            $expectedHash = $Matches[1].ToLowerInvariant()
            break
        }
    }
    if (-not $expectedHash) {
        throw "Checksum entry for $AssetName not found in SHA256SUMS.txt."
    }

    $actualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "SHA256 mismatch for $AssetName! Expected $expectedHash, got $actualHash."
    }
    Write-Host "SHA256 verified: $actualHash"

    Expand-Archive -LiteralPath $ZipPath -DestinationPath $StageDir -Force
    if (-not (Test-Path -LiteralPath (Join-Path $StageDir "pob.exe"))) {
        throw "Invalid archive layout: pob.exe not found in $AssetName."
    }

    # Stop any running daemon before replacing binaries
    $ExistingExe = Join-Path $InstallDir "pob.exe"
    if (Test-Path -LiteralPath $ExistingExe) {
        Write-Host "Stopping existing pob daemon before upgrading ..."
        try { & $ExistingExe stop 2>$null | Out-Null } catch {}
        Start-Sleep -Milliseconds 300
    }

    $ParentDir = Split-Path -Parent $InstallDir
    if ($ParentDir -and -not (Test-Path -LiteralPath $ParentDir)) {
        New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
    }

    $BackupDir = "$InstallDir.old"
    if (Test-Path -LiteralPath $BackupDir) {
        Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $InstallDir) {
        Move-Item -LiteralPath $InstallDir -Destination $BackupDir -Force
    }
    try {
        Move-Item -LiteralPath $StageDir -Destination $InstallDir -Force
        if (Test-Path -LiteralPath $BackupDir) {
            Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch {
        if ((Test-Path -LiteralPath $BackupDir) -and -not (Test-Path -LiteralPath $InstallDir)) {
            Move-Item -LiteralPath $BackupDir -Destination $InstallDir -Force
        }
        throw
    }

    if (-not $NoPath) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $entries = @()
        if ($userPath) {
            $entries = $userPath.Split(";") | Where-Object { $_ -and $_.Trim() -ne "" }
        }
        $normalizedTarget = $InstallDir.TrimEnd("\")
        $alreadyInPath = $false
        foreach ($e in $entries) {
            if ($e.TrimEnd("\").Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
                $alreadyInPath = $true
                break
            }
        }
        if (-not $alreadyInPath) {
            $newUserPath = if ($entries.Count -gt 0) { ($entries + $InstallDir) -join ";" } else { $InstallDir }
            [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
            Write-Host "Added $InstallDir to your User PATH."
        }
        if (-not ($env:Path.Split(";") | Where-Object { $_.TrimEnd("\").Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase) })) {
            $env:Path = "$InstallDir;$env:Path"
        }
    }

    $InstalledExe = Join-Path $InstallDir "pob.exe"
    $VerOut = & $InstalledExe --version
    Write-Host ""
    Write-Host "Installed successfully: $VerOut ($InstalledExe)"
    Write-Host "Next steps:"
    Write-Host "  1. Open a new terminal (or run directly in this shell)."
    Write-Host "  2. Bootstrap the headless PoB engine (pass --proxy if GitHub requires a proxy):"
    Write-Host "       pob bootstrap"
    Write-Host "       pob bootstrap --proxy http://127.0.0.1:7890"
    Write-Host "  3. Verify with: pob status"
} finally {
    if (Test-Path -LiteralPath $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
