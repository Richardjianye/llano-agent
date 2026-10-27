"""测试公共配置

当前这批是不依赖 MySQL/Redis 的纯逻辑测试。
配置项通过环境变量注入，避免测试机上必须有 .env。
需要真实数据库的集成测试请另建 tests/integration/，先 docker compose up -d。
"""

import os
import sys

# 让测试直接 import server 包下的模块（与运行时的 import 风格保持一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "MYSQL_URL", "mysql+aiomysql://test:test@localhost:3306/test?charset=utf8mb4"
)
os.environ.setdefault("ENVIRONMENT", "dev")
