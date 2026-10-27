#requires -Version 7
<#
.SYNOPSIS
  ⛔ 已废弃（2026-09-07）。请改用 scripts/migrate.py。

.DESCRIPTION
  ## 为什么废弃 —— 这个脚本会重复灌迁移

  它无条件把 scripts/migrations/*.sql 按文件名全跑一遍，**不认 schema_migrations
  跟踪表**（写它的时候那张表还不存在）。对一个已经跑过迁移的库执行它，等于把
  所有 ALTER TABLE 灌第二遍 —— 加列的迁移第二次直接报错，而在那之前已经执行过的
  语句不会回滚（MySQL 的 DDL 是隐式提交的）。

  现在统一用：

      MIGRATE_DSN="mysql://…" python3 scripts/migrate.py --dry-run   # 先看要跑什么
      MIGRATE_DSN="mysql://…" python3 scripts/migrate.py

  它按 schema_migrations 跳过已跑的文件，用 GET_LOCK 防两个容器同时 ALTER，
  失败即退出非零。下面那些关于 PowerShell 编码的坑对它也不适用 ——
  Python 直接读文件，不经过 shell 重定向。

  留着这个文件只是为了记录当时为什么要这么绕。**不要执行它。**

.NOTES
  以下为原始说明，仅作历史记录 ——

  按顺序执行 scripts/migrations/*.sql（PowerShell 7）。

.DESCRIPTION
  ⚠️ 全新的库不需要跑这个。

  这些迁移绝大多数是 ALTER TABLE，改的是**已经存在**的表。空库上第一条就会以
  "Table 'audit_gateway.models' doesn't exist" 失败。全新的库正确做法是：建库 →
  启动一次服务，SQLAlchemy 的 create_all 会按当前 ORM 直接建成最新结构。
  （每个迁移文件自己的头注释也是这么写的。）

  这个脚本是给**已经跑过、需要升级**的库用的 —— 也就是第二次以后的部署。

.NOTES
  为什么不用 PowerShell 的 `<` 重定向：
    PowerShell 至今不支持输入重定向，`mysql ... < file.sql` 会直接报语法错误。

  为什么不用 `Get-Content file.sql | mysql`：
    管道会经过 PowerShell 的编码转换。这些 SQL 里全是中文注释，转换一旦不是
    UTF-8，MySQL 收到的就是乱码字节 —— 而注释里的乱码**不会报错**，它只是被
    当成注释吃掉；真正出问题的是 COMMENT '...' 里的中文，那会原样写进表结构，
    以后 SHOW CREATE TABLE 出来是一堆问号，且没有任何一步报过错。

  所以走 cmd 的重定向：cmd 是字节透传，PowerShell 碰都不碰文件内容。

  密码不落盘：用 Read-Host 读一次，塞进当前进程的 MYSQL_PWD，跑完在 finally 里
  清掉。代价是这段时间内同机的其它进程能从环境块里读到它 —— 比写进 .my.cnf 好，
  但不是零风险，跑完记得确认脚本正常退出过。
#>


param(
  [string]$MysqlExe      = 'mysql',
  [string]$Database      = 'audit_gateway',
  [string]$User          = 'root',
  [string]$MigrationsDir = (Join-Path $PSScriptRoot 'migrations'),
  [switch]$WhatIf
)

Write-Host ""
Write-Host "  这个脚本已废弃，它会把已经跑过的迁移再灌一遍。" -ForegroundColor Red
Write-Host "  请改用:  MIGRATE_DSN=... python3 scripts/migrate.py" -ForegroundColor Yellow
Write-Host ""
exit 1


$ErrorActionPreference = 'Stop'

# mysql.exe 不在 PATH 时给出确切的找法，而不是让 cmd 回一句 'mysql' 不是内部命令
$resolved = Get-Command $MysqlExe -ErrorAction SilentlyContinue
if (-not $resolved) {
  Write-Host "找不到 $MysqlExe。MySQL 8 默认装在：" -ForegroundColor Yellow
  Write-Host '  C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
  Write-Host '用 -MysqlExe 指定完整路径，或把该目录加进 PATH。'
  exit 1
}

$files = Get-ChildItem -Path $MigrationsDir -Filter '*.sql' | Sort-Object Name
if (-not $files) { Write-Host "没有找到迁移文件：$MigrationsDir"; exit 1 }

Write-Host "将按以下顺序执行 $($files.Count) 个文件（文件名即日期序）：" -ForegroundColor Cyan
$files | ForEach-Object { Write-Host "  $($_.Name)" }

if ($WhatIf) { Write-Host "`n-WhatIf：只列出，未执行。"; exit 0 }

$secure = Read-Host -Prompt "MySQL 用户 $User 的密码" -AsSecureString
$env:MYSQL_PWD = [System.Net.NetworkCredential]::new('', $secure).Password

try {
  foreach ($f in $files) {
    Write-Host "`n== $($f.Name)" -ForegroundColor Cyan

    # --default-character-set=utf8mb4 必须给：客户端默认字符集跟着 Windows 代码页走，
    # 在中文系统上是 gbk，COMMENT 里的中文会按 gbk 解释后再转存，结果是花的。
    # 不要用 $args 当变量名 —— 那是 PowerShell 的自动变量
    $cmdLine = "`"$($resolved.Source)`" -u $User --default-character-set=utf8mb4 $Database < `"$($f.FullName)`""
    cmd /c $cmdLine

    if ($LASTEXITCODE -ne 0) {
      Write-Host "`n失败于 $($f.Name)（退出码 $LASTEXITCODE），已停止。" -ForegroundColor Red
      Write-Host '前面的文件已经执行过了 —— 修好之后从这一个继续，不要从头再来：'
      Write-Host '重复执行 ALTER 会报 Duplicate column name，那个报错本身是安全的，'
      Write-Host '但它会盖住真正的问题。'
      exit $LASTEXITCODE
    }
    Write-Host "   ok" -ForegroundColor Green
  }

  Write-Host "`n全部完成。启动服务后看一眼日志里的表结构自检结果。" -ForegroundColor Green
}
finally {
  Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
}
