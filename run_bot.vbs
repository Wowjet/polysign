' Локальный запуск polysign в фоне: без окна консоли, вывод — в bot_run.log.
' Этим файлом стартует бот и вручную, и через автозагрузку
' (%APPDATA%\...\Startup\polysign.lnk указывает именно сюда).
'
' config.local.json, а не config.json: локальная копия пишет сигналы в
' signals.local.log, чтобы не конфликтовать с signals.log, который бот
' на GitHub Actions сам коммитит в репозиторий.
Dim sh, fso, pythonExe, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
' Работаем из папки этого скрипта — все пути (конфиг, логи) относительные
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' Предпочитаем python из локального .venv: там зафиксирована версия 3.13+
' и websocket-client. Версия важна: ESPN отдаёт 403 запросам urllib с
' Python <= 3.12 (подробности — sources.py и README). Если венва нет,
' падаем на системный python из PATH.
pythonExe = fso.BuildPath(dir, ".venv\Scripts\python.exe")
If Not fso.FileExists(pythonExe) Then
    pythonExe = "python"
End If

' -u: небуферизованный вывод, чтобы лог писался сразу;
' >> 2>&1: stdout и stderr в один файл. Дубль не создастся: бот берёт
' лок на порт 47891 и второй экземпляр сам выходит.
cmd = "cmd /c """ & pythonExe & """ -u bot.py --config config.local.json >> bot_run.log 2>&1"
' 0 = скрытое окно, False = не ждать завершения (скрипт сразу выходит)
sh.Run cmd, 0, False
