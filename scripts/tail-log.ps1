# 看最近的运行日志（自动挑最新的那个日志文件，按 UTF-8 读）。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\tail-log.ps1
#       ... -Lines 40

param(
    [int]$Lines = 20
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'logs'

if (-not (Test-Path $logDir)) { throw "no logs dir: $logDir" }

$latest = Get-ChildItem (Join-Path $logDir '*.log') | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $latest) { throw "no log files in $logDir" }

Write-Host "== $($latest.Name) =="
# 必须显式 -Encoding utf8：默认会按 GBK 解码，中文全是乱码
Get-Content $latest.FullName -Encoding utf8 -Tail $Lines
