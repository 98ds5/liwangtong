' campus_login - 静默启动脚本 v2.0
' 双击运行即可静默启动后台守护模式（自动重连）
'
' 行为：
'   1. 检查 exe 是否存在
'   2. 执行日志轮转（>500KB 时保留最近 200 行）
'   3. 以 daemon 模式静默启动（无窗口）

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
exePath = scriptDir & "\campus_login.exe"
logPath = scriptDir & "\campus_login.log"

If Not objFSO.FileExists(exePath) Then
    WScript.Quit
End If

' Log rotation: if log > 500KB, keep last 200 lines
If objFSO.FileExists(logPath) Then
    Set logFile = objFSO.GetFile(logPath)
    If logFile.Size > 512000 Then
        Set f = objFSO.OpenTextFile(logPath, 1, False, -1) ' TristateFalse + UTF-8 via -1
        allLines = Split(f.ReadAll, vbCrLf)
        f.Close
        keep = 200
        If UBound(allLines) + 1 > keep Then
            startLine = UBound(allLines) - keep + 1
        Else
            startLine = 0
        End If
        Set f = objFSO.CreateTextFile(logPath, True, True) ' True = Unicode (UTF-16), closest to UTF-8 for log
        For i = startLine To UBound(allLines)
            f.WriteLine allLines(i)
        Next
        f.Close
    End If
End If

' Silent daemon mode - no window, no popup, auto-reconnect
objShell.Run """" & exePath & """ daemon", 0, False
