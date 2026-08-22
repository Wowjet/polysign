' Скрытый запуск polysign в фоне (без окна консоли), лог — в bot_run.log
' Локально пишем в signals.local.log, чтобы не конфликтовать с серверным signals.log
Dim sh, fso, pythonExe, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
pythonExe = fso.BuildPath(dir, ".venv\Scripts\python.exe")
If Not fso.FileExists(pythonExe) Then
    pythonExe = "python"
End If
cmd = "cmd /c """ & pythonExe & """ -u bot.py --config config.local.json >> bot_run.log 2>&1"
sh.Run cmd, 0, False
