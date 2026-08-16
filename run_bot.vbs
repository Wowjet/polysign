' Скрытый запуск polysign в фоне (без окна консоли), лог — в bot_run.log
' Локально пишем в signals.local.log, чтобы не конфликтовать с серверным signals.log
Dim sh, fso
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c python -u bot.py --config config.local.json >> bot_run.log 2>&1", 0, False
