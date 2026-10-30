' 启动 supervisor（无窗口）。放进 shell:startup 即可开机自启，不需要管理员权限。
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\桌面\py\bili-pet"
shell.Run """D:\桌面\py\bili-pet\.venv\Scripts\pythonw.exe"" -m bili.supervisor", 0, False
