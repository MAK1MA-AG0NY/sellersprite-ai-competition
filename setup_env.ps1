# 环境重建脚本（迁移到新电脑 / venv 损坏时使用）
# 前提：已安装 uv（https://docs.astral.sh/uv/），或本机有任意 Python 3.10+
# 用法：  .\setup_env.ps1

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) 浏览器内核：若随包带了 .pw-browsers 则无需下载，只做校验
$pw = Join-Path $here '.pw-browsers'
$chrome = Join-Path $pw 'chromium-1234\chrome-win64\chrome.exe'
$shell  = Join-Path $pw 'chromium_headless_shell-1234\chrome-headless-shell-win64\chrome-headless-shell.exe'
if ((Test-Path $chrome) -and (Test-Path $shell)) {
    Write-Host "[+] 浏览器内核已就位: $pw" -ForegroundColor Green
} else {
    Write-Host "[!] 内核缺失，改用 playwright 官方安装（需网络）" -ForegroundColor Yellow
    $env:PLAYWRIGHT_BROWSERS_PATH = $pw
    & (Join-Path $here '.venv\Scripts\playwright.exe') install chromium
}

# 2) venv：已存在则跳过；损坏/不存在则用 uv 重建
$venvPy = Join-Path $here '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    & $venvPy -c "import playwright" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] venv 可用: $venvPy" -ForegroundColor Green
        exit 0
    }
    Write-Host "[!] venv 损坏（多为换机器后路径失效），重建中…" -ForegroundColor Yellow
    Remove-Item (Join-Path $here '.venv') -Recurse -Force
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { $uvPath = "$env:USERPROFILE\.local\bin\uv.exe"; if (Test-Path $uvPath) { $uv = $uvPath } else { Write-Error "未找到 uv，请先安装: https://docs.astral.sh/uv/"; exit 1 } }
else { $uv = $uv.Source }

$env:UV_CACHE_DIR = Join-Path $here '.uv-cache'
& $uv venv (Join-Path $here '.venv') --python 3.14
& $uv pip install --python $venvPy -r (Join-Path $here 'requirements.txt')
Write-Host "[+] venv 重建完成" -ForegroundColor Green
Write-Host ""
Write-Host "运行: .\.venv\Scripts\python.exe sellersprite_ai_report.py" -ForegroundColor Cyan
