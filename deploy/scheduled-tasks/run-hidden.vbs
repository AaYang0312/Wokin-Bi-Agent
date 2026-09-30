' BI Agent 调度任务隐藏启动器（无需管理员权限的零弹窗方案）。
' 用法: wscript //B //Nologo run-hidden.vbs <日志文件绝对路径> <完整命令行...>
' 以隐藏窗口运行命令，stdout/stderr 追加进日志文件（结构化证据另在
' backend/logs/sync.log 的轮转 JSON 日志里，本文件只为补齐 stdout 的逐店统计行）。
' 给 cmd /c 的整条命令加外层引号：命令行里有多个引号时，cmd 的老式剥引号
' 规则会把首尾引号剥掉、把重定向目标弄丢——不加外层引号时任务能跑但日志为空。
Dim args, logFile, cmd, i
Set args = WScript.Arguments
If args.Count < 2 Then
    WScript.Quit 2
End If
logFile = args.Item(0)
cmd = ""
For i = 1 To args.Count - 1
    cmd = cmd & " " & args.Item(i)
Next
CreateObject("WScript.Shell").Run _
    "cmd /c """ & cmd & " >> """ & logFile & """ 2>&1""", 0, False
