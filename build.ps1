param(
    [switch]$SkipTools
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$distDir = Join-Path $projectRoot "dist\AutoEndcardTool"

Push-Location $projectRoot
try {
    python -m PyInstaller --noconfirm --clean AutoEndcardTool.spec

    $localTools = Join-Path $projectRoot "tools"
    if (-not $SkipTools -and (Test-Path -LiteralPath $localTools)) {
        $targetTools = Join-Path $distDir "tools"
        New-Item -ItemType Directory -Path $targetTools -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $localTools "ffmpeg.exe") -Destination $targetTools -Force
        Copy-Item -LiteralPath (Join-Path $localTools "ffprobe.exe") -Destination $targetTools -Force
    }

    Write-Output "Build completed: $distDir"
}
finally {
    Pop-Location
}
