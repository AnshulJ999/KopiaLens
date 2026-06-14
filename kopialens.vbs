Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
shell.Run "pythonw """ & scriptDir & "\kopialens_tray.py""", 0, False
