# Run watcher: started by the owner, by hand, in their own desktop session ("Run watcher.cmd"). It lets a
# remote session (ssh, which lands in session 0 and cannot show windows) run the desktop launchers of
# D:\SatisfactoryRecomp while the owner is away. It is not persistent: it lives in this console window and
# stops when the window is closed or the owner signs out.
#
# A job is a text file dropped in <root>\queue\<name>.job:
#   launcher=Run recomp.cmd     a .cmd in <root> named "Run ....cmd", nothing else
#   seconds=120                 the game is closed after that long (at most 1800)
#   at 40 shot                  screenshot at t=40 s, saved as queue\<name>-40.png
#   at 50 click 960 540         left click at screen pixel (960, 540)
#   at 55 key {ESC}             keystrokes, in SendKeys syntax
# The result is queue\<name>.done: what ran, the screenshots, and a copy of the game's log.
param([string]$Root = 'D:\SatisfactoryRecomp')
$ErrorActionPreference = 'Stop'
$host.UI.RawUI.WindowTitle = 'Recomp watcher - close this window to stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace W -Name U -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, System.UIntPtr e);
[DllImport("kernel32.dll")] public static extern int SetThreadExecutionState(int f);
'@
$queue = Join-Path $Root 'queue'
New-Item -ItemType Directory -Force $queue | Out-Null
$gameLog = Join-Path $env:LOCALAPPDATA 'FactoryGame\Saved\Logs\FactoryGame.log'

function Shot([string]$path) {
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}
function GameProcesses { Get-Process | Where-Object { $_.ProcessName -like 'FactoryGame*' } }

Write-Host "Watching $queue. Close this window to stop."
while ($true) {
    # Keep the display on while the watcher runs (ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED).
    [W.U]::SetThreadExecutionState(0x80000003) | Out-Null
    foreach ($job in Get-ChildItem $queue -Filter *.job) {
        $name = $job.BaseName
        $out = Join-Path $queue "$name.done"
        $lines = Get-Content $job.FullName
        Remove-Item $job.FullName
        $report = New-Object System.Collections.Generic.List[string]
        try {
            $launcher = ($lines | Where-Object { $_ -like 'launcher=*' } | Select-Object -First 1) -replace '^launcher=', ''
            if ($launcher -notmatch '^Run [A-Za-z0-9 _-]+\.cmd$') { throw "refused launcher '$launcher'" }
            $path = Join-Path $Root $launcher
            if (-not (Test-Path $path)) { throw "no launcher $path" }
            $seconds = [int](($lines | Where-Object { $_ -like 'seconds=*' } | Select-Object -First 1) -replace '^seconds=', '')
            if ($seconds -le 0 -or $seconds -gt 1800) { $seconds = 120 }
            $steps = @($lines | Where-Object { $_ -match '^at \d+ ' } | ForEach-Object {
                $p = $_ -split ' ', 4; [pscustomobject]@{ T = [int]$p[1]; Verb = $p[2]; Arg = ($p[3], '')[!$p[3]] } } | Sort-Object T)
            if (GameProcesses) { throw 'the game is already running' }
            Write-Host "$(Get-Date -Format T) $name : $launcher for $seconds s"
            $report.Add("job $name, launcher '$launcher', $seconds s, started $(Get-Date -Format s)")
            Start-Process -FilePath $path -WorkingDirectory $Root
            $start = Get-Date
            foreach ($s in $steps) {
                while (((Get-Date) - $start).TotalSeconds -lt $s.T) { Start-Sleep -Milliseconds 200 }
                switch ($s.Verb) {
                    'shot'  { $png = Join-Path $queue "$name-$($s.T).png"; Shot $png; $report.Add("t=$($s.T) shot $png") }
                    'click' { $xy = $s.Arg -split ' '; [W.U]::SetCursorPos([int]$xy[0], [int]$xy[1]) | Out-Null
                              Start-Sleep -Milliseconds 100
                              [W.U]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60
                              [W.U]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero); $report.Add("t=$($s.T) click $($s.Arg)") }
                    'key'   { [System.Windows.Forms.SendKeys]::SendWait($s.Arg); $report.Add("t=$($s.T) key $($s.Arg)") }
                    default { $report.Add("t=$($s.T) unknown step '$($s.Verb)'") }
                }
            }
            while (((Get-Date) - $start).TotalSeconds -lt $seconds -and (GameProcesses)) { Start-Sleep -Seconds 1 }
            $alive = GameProcesses
            if ($alive) { $alive | ForEach-Object { $_.CloseMainWindow() | Out-Null }; Start-Sleep -Seconds 10 }
            GameProcesses | Stop-Process -Force
            $report.Add("game closed after $([int]((Get-Date) - $start).TotalSeconds) s (it was $(if ($alive) {'still running'} else {'already gone'}))")
            if (Test-Path $gameLog) { Copy-Item $gameLog (Join-Path $queue "$name.log"); $report.Add("log $queue\$name.log") }
        } catch {
            $report.Add("error: $($_.Exception.Message)")
        }
        $report | Set-Content $out
        Write-Host "$(Get-Date -Format T) $name done"
    }
    Start-Sleep -Seconds 5
}
