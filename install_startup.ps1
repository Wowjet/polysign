$ws = New-Object -ComObject WScript.Shell
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\polysign.lnk"
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = '"' + (Join-Path $dir "run_bot.vbs") + '"'
$sc.WorkingDirectory = $dir
$sc.Save()
Write-Output "autostart shortcut: $lnk"
