$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Command
    )

    & $Command[0] @($Command | Select-Object -Skip 1)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $($Command -join ' ')"
    }
}

Invoke-NativeCommand @("python", "-m", "pip", "install", "-r", "requirements.txt", "-r", "requirements-build.txt")

if (-not (Test-Path -LiteralPath "bin")) {
    New-Item -ItemType Directory -Path "bin" | Out-Null
}

function Copy-ToolFromPathOrWinget {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Tool
    )

    $destination = "bin\$Tool.exe"
    if (Test-Path -LiteralPath $destination) {
        return
    }

    $command = Get-Command $Tool -ErrorAction SilentlyContinue
    if ($command -and (Test-Path -LiteralPath $command.Source)) {
        try {
            Copy-Item -LiteralPath $command.Source -Destination $destination -Force
            return
        }
        catch {
            Write-Warning "Could not copy $Tool from $($command.Source). Trying Winget package folder."
        }
    }

    $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    if (Test-Path -LiteralPath $packageRoot) {
        $match = Get-ChildItem -LiteralPath $packageRoot -Recurse -Filter "$Tool.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) {
            Copy-Item -LiteralPath $match.FullName -Destination $destination -Force
            return
        }
    }

    Write-Warning "Could not bundle $Tool. The target computer must have $Tool on PATH or in a bin folder beside the app."
}

foreach ($tool in @("ffmpeg", "ffprobe")) {
    Copy-ToolFromPathOrWinget -Tool $tool
}

foreach ($oldOutput in @("dist\YoutubeConverter", "dist\YoutubeConverter.zip")) {
    if (Test-Path -LiteralPath $oldOutput) {
        Remove-Item -LiteralPath $oldOutput -Recurse -Force
    }
}

Invoke-NativeCommand @("python", "-m", "PyInstaller", "YoutubeConverter.spec", "--clean", "--noconfirm")

Write-Host ""
Write-Host "Build complete:"
Write-Host "dist\YoutubeConverter.exe"
