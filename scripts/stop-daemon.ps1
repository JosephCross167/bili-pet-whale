# 让常驻进程自己停下（不需要管理员权限）。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-daemon.ps1
#
# 原理：在 state\ 下放一个 stop.flag，常驻进程每轮循环都会检查；
# 看到就删掉标记并正常退出（会走 finally，锁也会被释放）。
# 最多等一个轮询间隔（45~60 秒）。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$flag = Join-Path $root 'state\stop.flag'
$stateDir = Join-Path $root 'state'

if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir | Out-Null }
New-Item -ItemType File -Path $flag -Force | Out-Null

Write-Host "已在 $flag 放下停止标记"
Write-Host "常驻进程会在下一轮检查时退出（最多约 60 秒）。之后可用 Get-Process pythonw 确认。"
