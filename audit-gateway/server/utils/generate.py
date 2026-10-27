"""通用生成工具 - 用户ID、随机密码等"""

import secrets
import string
import uuid


def generate_user_id() -> str:
    """生成唯一用户ID（32位hex）"""
    return uuid.uuid4().hex


# 初始密码要靠人工转达（口头、私信、当面），所以排除两类字符：
# 1. 引号与反斜杠 " ' ` \ —— 复制粘贴进各种输入框容易被转义或吞掉
# 2. 形近字符 0O1lI —— 口头转达和手抄时高频出错
_PASSWORD_SYMBOLS = "!#$%&*+-=?@^_"
_PASSWORD_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + _PASSWORD_SYMBOLS
)


def generate_password(length: int = 12) -> str:
    """生成随机初始密码

    保证至少各含一个大写、小写、数字和符号，避免偶然生成出全字母的弱密码。
    """
    if length < 8:
        length = 8

    upper = [c for c in string.ascii_uppercase if c not in "IO"]
    lower = [c for c in string.ascii_lowercase if c not in "l"]
    digits = [c for c in string.digits if c not in "01"]

    required = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(_PASSWORD_SYMBOLS),
    ]
    rest = [secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - len(required))]

    chars = required + rest
    # 打散位置，否则前四位的类型是固定的
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
