Set WshShell = CreateObject("Wscript.Shell")
WshShell.CurrentDirectory = "C:\Users\juder\jarvis"
WshShell.Run """C:\Users\juder\jarvis\.venv-win\Scripts\python.exe"" ""C:\Users\juder\jarvis\src\widget_app.py""", 0, False