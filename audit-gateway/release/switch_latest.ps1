# 把分发机 130 的固定下载入口 /releases/latest/{win,mac} 切到指定版本。
#
# 在 130 上运行（PowerShell 5+）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File switch_latest.ps1 -Version 0.18.4
# 只看不改：
#   powershell -NoProfile -ExecutionPolicy Bypass -File switch_latest.ps1 -Version 0.18.4 -DryRun
#
# 以前每次都是手工按字节替换 web.config 里两处 destination（见 docs/releases/0.18.0-release.md），
# 于是常被忘掉：0.18.3 网关 09-24 就发布了，固定入口到 09-29 还指着 0.18.2。
# 切完在 Mac 上跑 check_latest.py，两边一致才算发完。
#
# 做法与之前手工操作一致：
#   - 只替换安装包文件名里的版本号，其余字节（编码、无 BOM、中文注释）原样保留
#   - 改之前先备份 web.config.backup-<时间>-<版本>
#   - 目标安装包不在目录里就不改 —— 入口指向一个 404 比指向旧版更糟

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$ReleasesDir = 'C:\inetpub\wwwroot\releases',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "版本号格式不对：$Version（应形如 0.18.4）"
}

$config = Join-Path $ReleasesDir 'web.config'
$targets = @(
    "LlanoAIWorkbench-$Version-production-win-x64.exe",
    "LlanoAIWorkbench-$Version-production-mac-arm64.dmg"
)

foreach ($name in $targets) {
    if (-not (Test-Path (Join-Path $ReleasesDir $name))) {
        throw "分发目录里没有 $name，先把安装包放上来再切入口"
    }
}

# 按字节读写，不经过 PowerShell 的编码猜测
$bytes = [System.IO.File]::ReadAllBytes($config)
$latin1 = [System.Text.Encoding]::GetEncoding(28591)
$text = $latin1.GetString($bytes)

$patterns = @(
    'LlanoAIWorkbench-\d+\.\d+\.\d+-production-win-x64\.exe',
    'LlanoAIWorkbench-\d+\.\d+\.\d+-production-mac-arm64\.dmg'
)

$updated = $text
for ($i = 0; $i -lt $patterns.Count; $i++) {
    $found = [regex]::Matches($updated, $patterns[$i])
    if ($found.Count -ne 1) {
        throw "web.config 里应当恰好有 1 处匹配 $($patterns[$i])，实际 $($found.Count) 处；结构变了，请人工处理"
    }
    Write-Host "$($found[0].Value) -> $($targets[$i])"
    $updated = [regex]::Replace($updated, $patterns[$i], $targets[$i])
}

if ($updated -eq $text) {
    Write-Host "已经指向 $Version，无需修改"
    exit 0
}

if ($DryRun) {
    Write-Host '（DryRun，未修改）'
    exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "$config.backup-$stamp-$($Version -replace '\.', '')"
Copy-Item $config $backup
[System.IO.File]::WriteAllBytes($config, $latin1.GetBytes($updated))

Write-Host "已切换到 $Version；备份 $backup；$($bytes.Length) -> $((Get-Item $config).Length) 字节"
