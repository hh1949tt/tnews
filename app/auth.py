"""账号与令牌：密码哈希（PBKDF2）+ 随机会话令牌。

不引入额外依赖：哈希用标准库 hashlib.pbkdf2_hmac，
令牌用 secrets 生成、明文存 SQLite（局域网自用服务，够用）。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        ).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, AttributeError):
        return False


# 令牌字符集：只用字母数字，去掉 0/o/1/l/i 这类容易看错的字符。
# 连接令牌会直接出现在地址里（/s/<token>、/feed/<token>.xml、?ext=<token>），
# 用 token_urlsafe 会带 - 和 _，有些播客 App / 自动化在转发地址时会把它截断或转义。
TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
TOKEN_ALPHABET_SAFE = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def random_token(length: int = 32, *, mixed_case: bool = False) -> str:
    """生成纯字母数字令牌（不带 - _ 等符号）。"""
    alphabet = TOKEN_ALPHABET_SAFE if mixed_case else TOKEN_ALPHABET
    return "".join(secrets.choice(alphabet) for _ in range(int(length)))


def new_token() -> str:
    """登录口令（请求头里的那种）。"""
    return random_token(40, mixed_case=True)


def is_clean_token(tok: str) -> bool:
    """令牌是否只由字母数字组成。

    早期版本用 secrets.token_urlsafe() 生成，会带 `-` 和 `_`；
    现在统一改成纯字母数字，这类旧令牌在界面上会提示「可换新」。
    """
    t = str(tok or "").strip()
    return bool(t) and re.fullmatch(r"[0-9A-Za-z]+", t) is not None


def random_password(length: int = 12) -> str:
    """首次启动时给管理员生成随机密码（去掉 0/O/1/l/I 这类容易看错的字符）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
