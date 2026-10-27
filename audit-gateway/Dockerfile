# 网关镜像 —— 管理后台前端与 FastAPI 服务端打成一个
#
# ## 构建上下文必须是 audit-gateway/，不是仓库根
#
#     docker build -t audit-gateway audit-gateway/
#
# 因为一个镜像里要同时放 client/ 的构建产物和 server/ 的代码。
# ⚠️ 仓库根目录**另有一个 Dockerfile**，那是 Hermes 客户端的，跟网关毫无关系。
# Zeabur 上把服务的根目录设成 `audit-gateway`，不然它会去构建那一个。
#
# ## 这个镜像不跑数据库迁移
#
# 迁移目前没有版本表（DEPLOY.md 第四节还挂着这一条），执行到哪了不可查。
# 容器每次启动都跑一遍的话，那些 `ALTER TABLE` 会重复执行 —— 加列的迁移
# 第二次就报错，而滚动更新时这会表现成「新容器起不来，旧的还在」，
# 排查方向全被带偏。迁移仍然是**部署前手工执行**的一步，见 DEPLOY.md 第三节。

# ==================== 第一段：构建管理后台前端 ====================
# Node 26 与仓库其它地方的工具链一致（根 Dockerfile 里也钉的 26）。
FROM node:26-slim AS client

WORKDIR /build

# 先只拷贝两个清单文件再 npm ci —— 改业务代码不会让依赖层失效，
# 本地反复构建时这一层能省两三分钟。
COPY client/package.json client/package-lock.json ./
RUN npm ci

COPY client/ ./

# ⚠️ 这里是 `build-only`（纯 vite build），**不含 vue-tsc 类型检查**。
#
# 本来用的是 `npm run build`（type-check + build），理由是「类型错误在这里挡住，
# 比出一个界面能打开但某个页面一点就白屏的包强」。换掉是被内存逼的：
# Zeabur 那台专用服务器是 2 核 4G，2026-08-27 实测已用 58%、只剩约 1.5GB，
# 而 vue-tsc 扫这个 Vuetify 项目峰值上 1GB 很常见。构建 pod 被 OOM 掉只是构建
# 失败，但节点级内存压力有可能波及同一台上跑着的其它服务 —— 那台机器上有
# 部门唯一的公网服务。
#
# **代价要说清楚：类型检查不再是构建门槛。** 它现在只在打包前的本地流程里
# （`npm run lint && npm run type-check && npm run test`），也就是靠纪律而不是靠流水线。
# 哪天那台机器内存宽裕了（负责人说搞定这套之后 NewAPI 那组服务就能停），
# 或者接上 CI，把这行换回 `npm run build`。
RUN npm run build-only


# ==================== 第二段：运行时 ====================
# 3.14 是对齐测试服（192.168.30.108 上跑的是 3.14.4）。
# 两个落点用不同的解释器，等于给自己留一类「测试服上好好的」的 bug，
# 而这正是要锁版本的同一个理由。
#
# 只钉到小版本，补丁位放开：CPython 的补丁版基本只有安全修复，
# 钉死 3.14.4 意味着以后每个 CVE 都要改一次这行才修得上。
FROM python:3.14-slim

# 日志不缓冲，否则容器平台上看到的日志永远慢一截，崩溃前那几行还会丢。
ENV PYTHONUNBUFFERED=1
# 不写 .pyc：镜像层是只读的，写进去只是让层变大。
ENV PYTHONDONTWRITEBYTECODE=1

# ⚠️ 时区必须钉在镜像里，不能只靠部署平台的环境变量。
#
# `model/database.now()` 存的是**服务器本地时间**（naive），注释里写着「部署时
# 保证服务器时区为 Asia/Shanghai 即可」—— 而在 2026-09-03 之前没有任何一处在
# 保证它：镜像没设 TZ，Zeabur 上也没配，容器一直跑在 UTC。结果是后台每一处
# 时间都比实际早 8 小时，而且不会有任何报错，只是所有人都觉得「时间怎么不对」。
#
# slim 镜像里不一定有 /usr/share/zoneinfo，所以 tzdata 显式装一份（约 3MB）。
# 少了它 TZ 会被静默忽略、时间退回 UTC —— 正是这次要修的那个失败形状，
# 不能靠「应该有吧」。启动时还有一道断言（main.py 的 _assert_timezone），
# 万一哪天基础镜像变了，日志里会直接说出来，而不是等人发现时间对不上。
#
# 改这一行要连带想一件事：库里的存量时间戳是按旧时区写的，换时区就得跟着搬
# （见 scripts/migrations/2026-09-03-container-timezone.sql）。
ENV TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/server

# ---- 依赖 ----
# 用锁文件，不是 requirements.txt —— 见 requirements.lock.txt 的文件头。
#
# `--only-binary=:all:` 是故意的：这套锁在 cp314/manylinux 上全部有 wheel
# （2026-08-27 用 pip --dry-run 对着 manylinux_2_28_x86_64 验过）。
# 哪天有人加了一个没有 wheel 的依赖，这里会直接失败并指名是哪个包，
# 而不是悄悄退回源码编译 —— 那时报出来的是一屏 gcc 错误，
# 真正的原因（镜像里没有编译器）要往上翻很久才看得到。
COPY server/requirements.lock.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.lock.txt

# ---- 代码 ----
# .dockerignore 已经挡掉 .env*、__pycache__、static/audit、tests 等，
# 那份清单与 DEPLOY.md 第一节的「不要带」是同一份东西，改一处要改两处。
COPY server/ ./

# 前端产物放到 ../client/dist —— 服务端 resolve_frontend_dist() 的默认位置，
# 这样镜像里不用再配 FRONTEND_DIST_PATH。
COPY --from=client /build/dist /app/client/dist

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ---- 运行期目录与用户 ----
# 附件与头像要能写。static/ 是**运行期数据**，正常部署要把持久卷挂到这里，
# 不挂的话每次重新部署附件就没了（entrypoint 会就这一点喊一嗓子）。
RUN mkdir -p /app/server/static/audit /app/server/static/avatar \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app

USER app

# 与测试服、与桌面端 test 包里写死的端口一致。
# Zeabur 会注入自己的 PORT，pydantic-settings 直接认这个环境变量。
ENV PORT=7899
EXPOSE 7899

# 用 /healthz（只看进程活着），不是 /readyz（会去连 MySQL 和 Redis）。
# 容器编排里把「依赖挂了」判成「这个容器坏了」会触发无意义的重启循环 ——
# 重启一百次也连不上别人家的数据库。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','7899') + '/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
