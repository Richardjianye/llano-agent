"""日志系统配置 - Loguru + InterceptHandler"""

import logging
import os
import sys
from pathlib import Path

from config import get_settings
from loguru import logger

config = get_settings()


class InterceptHandler(logging.Handler):
    """将 Python 标准日志重定向到 Loguru"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(log_dir: Path = Path("logs"), name: str = "app"):
    """配置日志系统"""
    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: ^8}</level> | "
        "process [<cyan>{process}</cyan>]:<cyan>{thread}</cyan> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # 控制台输出
    logger.add(
        sys.stdout,
        format=log_format,
        level=config.log_level,
        enqueue=True,
        colorize=True,
    )

    # 创建日志目录
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 常规日志文件
    logger.add(
        str(Path(log_dir, f"{name}.log")),
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 week",
        encoding="utf-8",
        enqueue=True,
    )

    # 错误日志文件
    logger.add(
        str(Path(log_dir, "error.log")),
        format=log_format,
        level="ERROR",
        rotation="10 MB",
        retention="3 week",
        encoding="utf-8",
        enqueue=True,
    )

    # 拦截第三方库日志
    for logger_name in ["uvicorn", "uvicorn.error", "fastapi", "sqlalchemy"]:
        _logger = logging.getLogger(logger_name)
        _logger.handlers.clear()
        _logger.addHandler(InterceptHandler())
        _logger.propagate = False
        _logger.setLevel(logging.INFO)
