' Скрытый запуск polysign в фоне (без окна консоли), лог — в bot_run.log
Dim sh, fso
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c python -u bot.py >> bot_run.log 2>&1", 0, False
