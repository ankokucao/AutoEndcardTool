param(
    [switch]$SkipTools
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$distDir = Join-Path $projectRoot "dist\AutoEndcardTool"
$bundledTclLibrary = Join-Path $projectRoot "_internal\_tcl_data"
$bundledTkLibrary = Join-Path $projectRoot "_internal\_tk_data"
$previousTclLibrary = $env:TCL_LIBRARY
$previousTkLibrary = $env:TK_LIBRARY

Push-Location $projectRoot
try {
    # Some Python installations ship a broken Tcl search path. The checked-in
    # runtime contains a verified Tcl/Tk data set, so prefer it while building.
    if ((Test-Path -LiteralPath $bundledTclLibrary) -and (Test-Path -LiteralPath $bundledTkLibrary)) {
        $env:TCL_LIBRARY = $bundledTclLibrary
        $env:TK_LIBRARY = $bundledTkLibrary
    }

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
    if ($null -eq $previousTclLibrary) {
        Remove-Item Env:TCL_LIBRARY -ErrorAction SilentlyContinue
    }
    else {
        $env:TCL_LIBRARY = $previousTclLibrary
    }

    if ($null -eq $previousTkLibrary) {
        Remove-Item Env:TK_LIBRARY -ErrorAction SilentlyContinue
    }
    else {
        $env:TK_LIBRARY = $previousTkLibrary
    }

    Pop-Location
}
