"""账号与令牌：密码哈希（PBKDF2）+ 随机会话令牌。

不引入额外依赖：哈希用标准库 hashlib.pbkdf2_hmac，
令牌用 secrets 生成、明文存 SQLite（局域网自用服务，够用）。
"""
from __future__ import annotations

import hashlib
import hmac
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


def new_token() -> str:
    return secrets.token_urlsafe(32)


def random_password(length: int = 12) -> str:
    """首次启动时给管理员生成随机密码（去掉 0/O/1/l/I 这类容易看错的字符）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
