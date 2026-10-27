#!/bin/sh
# 网关容器入口
#
# 做三件事，然后把自己换成 gunicorn（exec，保证信号能传到 worker，
# 不然平台发的 SIGTERM 会被 shell 吃掉，最后走的是超时强杀）：
#
#   1. 检查会话附件目录能不能写
#   2. 检查它是不是真的挂了持久卷
#   3. 单进程把库初始化掉（多 worker 同时建表会撞车，见下）
#   4. 起服务
#
# ⚠️ 这里**不跑数据库迁移**，原因见 Dockerfile 顶部。
set -eu

PORT="${PORT:-7899}"
# gunicorn 的 worker 数。DEPLOY.md 里手工起的是 -w 4，默认对齐它。
# Zeabur 上小规格实例（512MB 那档）要往下调，4 个 worker 各带一份
# SQLAlchemy 连接池，内存会顶到 OOM 而日志上只留下一个没头没尾的重启。
WEB_CONCURRENCY="${WEB_CONCURRENCY:-4}"

STORAGE="${AUDIT_STORAGE_PATH:-/app/server/static/audit}"
STORE_BYTES="$(printf '%s' "${AUDIT_STORE_ATTACHMENT_BYTES:-true}" | tr 'A-Z' 'a-z')"

case "$STORE_BYTES" in
  false|0|no|off) STORE_BYTES="off" ;;
  *)              STORE_BYTES="on"  ;;
esac

mkdir -p "$STORAGE" 2>/dev/null || true

# ---- 1. 写得进去吗 ----
if [ "$STORE_BYTES" = "on" ] && ! touch "$STORAGE/.write-probe" 2>/dev/null; then
  echo "FATAL: 附件目录写不进去：$STORAGE" >&2
  echo "" >&2
  echo "  当前进程 uid=$(id -u)。挂上来的卷如果属主是别人且没给写权限，就是现在这样。" >&2
  echo "  （镜像里写了 USER app(10001)，但**平台可能覆盖它** —— Zeabur 实测就是以" >&2
  echo "   root 跑的。所以这里打真实 uid，不写死 10001：写死的那个数字会把人" >&2
  echo "   引到错误的方向上去。）" >&2
  echo "  两条路选一条：" >&2
  echo "    - 把卷的属主/权限改成上面这个 uid 写得进去" >&2
  echo "    - 确实不需要留附件本体，就设 AUDIT_STORE_ATTACHMENT_BYTES=false" >&2
  echo "" >&2
  echo "  在这里直接失败是故意的。让它起来的话，附件会在每次写盘时静默失败，" >&2
  echo "  而审计页照样显示「有这个附件」—— 点开才是 404，那时已经过去几周了。" >&2
  exit 1
fi
rm -f "$STORAGE/.write-probe" 2>/dev/null || true

# ---- 2. 是持久卷还是容器层 ----
# 用设备号比：和 / 同一个设备就说明它还在容器的可写层里，重新部署即丢。
# 这一条只警告不拦 —— 不挂卷是一个可以接受的取舍，前提是有人知道自己在取舍。
if [ "$STORE_BYTES" = "on" ] && [ "$(stat -c %d "$STORAGE" 2>/dev/null || echo x)" = "$(stat -c %d / 2>/dev/null || echo y)" ]; then
  echo "WARNING: $STORAGE 没有挂持久卷，它在容器可写层里。" >&2
  echo "         会话附件会在每次重新部署后消失，而库里的元数据还在 ——" >&2
  echo "         审计页会显示「有这个附件」但点开是 404，且没有任何一步报错。" >&2
  echo "         要么把卷挂到 /app/server/static，要么明确设" >&2
  echo "         AUDIT_STORE_ATTACHMENT_BYTES=false 只留元数据。" >&2
fi

# ---- 3. 先单进程把库初始化掉 ----
#
# ⚠️ 不这么做的话，全新的库 + 多 worker 起不来。
#
# `init_db()`（建表 + 写默认部门 + 建总裁账号）挂在 FastAPI 的 lifespan 上，
# 也就是**每个 gunicorn worker 各跑一遍**。空库上 N 个 worker 同时
# `create_all`，第二个就会撞
#
#     pymysql.err.OperationalError: (1050, "Table 'token_quotas' already exists")
#
# 整个容器随之退出（2026-08-27 用 4 个 worker 对着空库实测到的）。
# 更难查的是 `_seed_president`：它先查「有没有总裁」再插入，两个 worker
# 同时走到这里就可能建出两个总裁账号，各自打印一个不同的初始密码。
#
# DEPLOY.md 第三节的手工流程之所以没暴露，是因为它第 3 步先用单进程 uvicorn
# 起一次（那一步就把表建完了），第 5 步才换 gunicorn。这里做的是同一件事，
# 只是把「顺序很重要」这件事写进代码而不是写在文档里。
#
# 跑完之后 worker 里那几遍 init_db 仍然会执行，但都是幂等的空转：
# create_all 默认 checkfirst，总裁已存在就不再建。
cd /app/server
echo "初始化数据库（建表 + 默认部门 + 总裁账号，幂等）…"
python - <<'PYEOF'
import asyncio

from db import close_db_engine, init_db, init_db_engine


async def main():
    await init_db_engine()
    await init_db()
    await close_db_engine()


asyncio.run(main())
PYEOF

# ---- 4. 起服务 ----
# 工作目录必须是 server/：get_settings() 传给 pydantic 的是相对路径（".env"、
# ".env.prod"），按进程当前目录解析。在别处启动的话所有配置项静默消失，
# 报出来的是 `mysql_url Field required` —— 看着像 .env 写错了。
# （这条是 2026-08-26 在 108 上真撞过的，见 DEPLOY.md 第三·五节。）
cd /app/server

# ⚠️ 不要加 --access-logfile
#
# 访问日志由 RequestContextMiddleware 统一打（main.py 里把 uvicorn.access
# 这个 logger 明确 disabled 掉了，就是为了不出现两份）。在这里打开 gunicorn
# 自己的访问日志，等于每个请求记两行，格式还不一样。
#
# --error-logfile - 要留着：prod 下 setup_loguru 把 loguru 挂到
# gunicorn.error 的 handler 上，全部应用日志（包括首次启动那条**只打印一次**
# 的总裁初始密码）都从这条道出来。
#
# uvicorn.workers.UvicornWorker 在 uvicorn 0.52 上会报一条 DeprecationWarning，
# 官方让改用独立的 uvicorn-worker 包。现在照旧 —— 与 DEPLOY.md 里手工起服务的
# 命令、与测试服上跑的保持一致；等它真被删掉时是加一个依赖 + 改这一行的事。
exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY}" \
  --error-logfile - \
  "$@"
