# TickerView 二期一键构建
# 用法(repo 根):
#   pwsh -File build.ps1              # 出 onedir + 安装器
#   pwsh -File build.ps1 -SkipInstaller   # 只出 dist\TickerView\
#   pwsh -File build.ps1 -Python "C:\path\to\python.exe"   # 指定装了依赖的解释器
param(
    [switch]$SkipInstaller,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> [1/2] PyInstaller onedir" -ForegroundColor Cyan
& $Python -m PyInstaller --noconfirm --clean TickerView.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败(exit $LASTEXITCODE)" }
if (-not (Test-Path "dist\TickerView\TickerView.exe")) {
    throw "未产出 dist\TickerView\TickerView.exe"
}
Write-Host "    onedir OK: dist\TickerView\" -ForegroundColor Green

if ($SkipInstaller) {
    Write-Host "==> 已按 -SkipInstaller 跳过安装器" -ForegroundColor Yellow
    exit 0
}

Write-Host "==> [2/2] Inno Setup" -ForegroundColor Cyan
$candidates = @(
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "未找到 ISCC.exe。请安装 Inno Setup 6,或用 -SkipInstaller 只产出 onedir 目录。"
}
& $iscc "installer\TickerView.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失败(exit $LASTEXITCODE)" }
Write-Host "==> 完成:installer\Output\TickerView-Setup-0.2.0.exe" -ForegroundColor Green
