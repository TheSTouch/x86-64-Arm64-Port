# An overlay game folder on Windows: the Windows counterpart of ./port overlay (portkit/overlay.py).
#
#   powershell -File overlay.ps1 -Game D:\SatisfactoryRecomp\Game -Out D:\SatisfactoryRecomp\Overlay-recomp `
#       -Dll D:\SatisfactoryRecomp\builds\v1-watermark\FactoryGameSteam-FactoryPreEarlyLoadingScreen-Win64-Shipping.dll
#
# Real folders only on the way to each replaced file; every other folder is a directory junction and every
# other file a hard link to the game's (same volume), so nothing is copied and the game folder is never
# written to. A .pdb next to a built DLL replaces the shipped one; otherwise the shipped .pdb is left out.
# A rebuild deletes only what a previous run made (the marker file says so); junctions are removed, never
# followed.

param(
    [Parameter(Mandatory)] [string] $Game,
    [Parameter(Mandatory)] [string] $Out,
    [string[]] $Dll = @()
)
$ErrorActionPreference = 'Stop'
$Marker = '.portkit-overlay.json'
$Game = (Resolve-Path $Game).Path.TrimEnd('\')
$Out = [IO.Path]::GetFullPath($Out).TrimEnd('\')

if ($Out.StartsWith($Game + '\', 'OrdinalIgnoreCase') -or $Game.StartsWith($Out + '\', 'OrdinalIgnoreCase') -or $Out -ieq $Game) {
    throw "$Out is inside the game folder or holds it; an overlay goes beside it"
}

# Each built file replaces the game's file of the same name, which must exist exactly once.
$replaced = [ordered]@{}
foreach ($d in $Dll) {
    $src = (Resolve-Path $d).Path
    $name = Split-Path $src -Leaf
    $found = @(Get-ChildItem $Game -Recurse -File -Filter $name)
    if ($found.Count -eq 0) { throw "${name}: the game ships no such file, so there is nothing to replace" }
    if ($found.Count -gt 1) { throw "$name is in several places in the game: $($found.FullName -join ', ')" }
    $replaced[$found[0].FullName.Substring($Game.Length + 1)] = $src
}

function Remove-Overlay([string] $folder) {
    foreach ($e in Get-ChildItem -LiteralPath $folder -Force) {
        if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) { [IO.Directory]::Delete($e.FullName) }   # the junction only
        elseif ($e.PSIsContainer) { Remove-Overlay $e.FullName; [IO.Directory]::Delete($e.FullName) }
        else { [IO.File]::Delete($e.FullName) }
    }
}

if (Test-Path -LiteralPath $Out) {
    $isEmpty = -not (Get-ChildItem -LiteralPath $Out -Force | Select-Object -First 1)
    if (-not $isEmpty -and -not (Test-Path -LiteralPath (Join-Path $Out $Marker))) {
        throw "$Out is not an overlay (no $Marker); refusing to touch it"
    }
    Remove-Overlay $Out
} else {
    New-Item -ItemType Directory -Path $Out | Out-Null
}

function Link-Children([string] $source, [string] $folder) {
    foreach ($e in Get-ChildItem -LiteralPath $source -Force) {
        $target = Join-Path $folder $e.Name
        if ($e.PSIsContainer) { New-Item -ItemType Junction -Path $target -Target $e.FullName | Out-Null }
        else { New-Item -ItemType HardLink -Path $target -Target $e.FullName | Out-Null }
    }
}

Link-Children $Game $Out
foreach ($rel in $replaced.Keys) {
    $folder = $Out
    $parts = (Split-Path $rel -Parent).Split('\')
    foreach ($part in $parts) {
        $folder = Join-Path $folder $part
        $item = Get-Item -LiteralPath $folder -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            [IO.Directory]::Delete($folder)      # the junction, not the game's folder
            New-Item -ItemType Directory -Path $folder | Out-Null
            Link-Children (Join-Path $Game $folder.Substring($Out.Length + 1)) $folder
        }
    }
    $src = $replaced[$rel]
    foreach ($pair in @(@($rel, $src), @([IO.Path]::ChangeExtension($rel, '.pdb'), [IO.Path]::ChangeExtension($src, '.pdb')))) {
        $target = Join-Path $Out $pair[0]
        if (Test-Path -LiteralPath $target) { [IO.File]::Delete($target) }   # a hard link: the game's file stays
        if (Test-Path -LiteralPath $pair[1]) { Copy-Item -LiteralPath $pair[1] -Destination $target }
    }
}

@{ game = $Game; replaced = $replaced } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Out $Marker) -Encoding UTF8
"Overlay of $Game in ${Out}: $($replaced.Count) file(s) replaced, everything else linked"
foreach ($rel in $replaced.Keys) { "  $rel  <-  $($replaced[$rel])" }
