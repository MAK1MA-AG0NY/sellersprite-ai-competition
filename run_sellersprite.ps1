# 运行卖家精灵 AI 报告自动化的一键启动脚本
# 用法示例：
#   .\run_sellersprite.ps1 B07Z82895W B0XXXXXXXX
#   .\run_sellersprite.ps1 -LoginOnly        # 仅打开浏览器刷新登录态

param(
    [switch]$LoginOnly,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Asins
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 让 playwright 使用工作区内的浏览器内核（默认会去 %LOCALAPPDATA% 找）
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $here '.pw-browsers'

$python = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error "venv 不存在：$python —— 请先运行环境安装步骤"
    exit 1
}

if ($LoginOnly) {
    # 强制重新登录：删除旧登录态后带界面打开
    Remove-Item (Join-Path $here 'storage_state.json') -ErrorAction SilentlyContinue
    & $python (Join-Path $here 'sellersprite_ai_report.py') DUMMY_ASIN_FOR_LOGIN
    exit 0
}

if (-not $Asins -or $Asins.Count -eq 0) {
    Write-Host "用法: .\run_sellersprite.ps1 ASIN1 ASIN2 ..."
    Write-Host "      .\run_sellersprite.ps1 -LoginOnly   # 仅刷新登录态"
    exit 0
}

& $python (Join-Path $here 'sellersprite_ai_report.py') @Asins
