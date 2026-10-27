# LLANO Agent 本地部署

## 环境

Python 3.11–3.13、Node.js >=22.22、npm、MySQL（原项目要求 8.x）、Redis、uv。

## 审计网关

1. 在 MySQL 创建独立数据库和专用账号，授予该账号此数据库的权限。
2. 在 `audit-gateway/server` 下创建 Python 环境并安装锁定依赖：

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.lock.txt
cp .env.example .env.dev
```

编辑 `.env.dev`：填写 `MYSQL_URL`、`REDIS_URL`，生成独立的 JWT 密钥和两把 Fernet 加密密钥。不要复制生产凭据，不要提交 `.env.dev`。

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

每次命令生成一个值，为不同字段分别生成。模型、Gitea 和钉钉配置可先留空。

3. 构建管理后台：

```sh
cd ../client
npm ci
npm run build
cd ../server
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 7899
```

首次运行使用单进程自动建表并生成管理员。用户名 `admin`，初始密码只在启动日志中显示一次，登录后自行修改。新数据库不要执行历史迁移；升级已有数据库才需要评估迁移脚本。

打开 `http://127.0.0.1:7899/`。`/healthz` 是存活检查，`/readyz` 检查 MySQL 和 Redis。

## 桌面客户端

回到仓库根目录：

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
npm ci
LLANO_APP_ENV=development HERMES_HOME="$PWD/.local-demo/hermes" HERMES_DESKTOP_USER_DATA_DIR="$PWD/.local-demo/desktop" HERMES_DESKTOP_HERMES_ROOT="$PWD" HERMES_DESKTOP_PYTHON="$PWD/.venv/bin/python" HERMES_DESKTOP_CDP_PORT=off npm run dev --workspace apps/desktop
```

以上使用独立本地目录，适用于 macOS/Linux shell。Windows 可按桌面 README 设置等价环境变量。

使用本地管理员账号登录。在后台配置模型渠道、模型和授权后，才能进行真实 AI 对话。可选平台和语音依赖未包含在最小安装中，按功能说明另行安装。

## 自行部署

`apps/desktop/product-environments.json` 中 development 指向本机，其它环境使用占位地址。自行部署时配置自己的域名、更新签名公钥、身份服务和外部系统地址。`.env.example` 的可选集成默认留空。

`audit-gateway/release/manifest-vectors.json` 中的固定密钥只用于跨语言签名测试，不可用于发布。生产签名私钥必须自行生成并在仓库之外保管。
