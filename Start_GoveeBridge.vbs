Option Explicit

Dim shell, fso, bridgePath, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
bridgePath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "bridge.py")
command = "pyw.exe """ & bridgePath & """"
shell.Run command, 0, False
