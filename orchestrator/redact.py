"""敏感信息脱敏。

存在的理由很具体（验收项 V9）：统一层要把上游的原始输出写进日志和
`error.adapter_raw`，而上游很可能把手机号、密码、cookie 一起打进 stdout。
如果不做统一脱敏，一次排障日志就可能把凭据写进磁盘。

因此脱敏是**日志层的强制环节**，不是可选的调用方义务。
"""

from __future__ import annotations

import re
from typing import Any

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")

#: key=value / "key": "value" 形式，key 命中敏感词时掩码 value
SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "uname",
    "username",
    "phone",
    "mobile",
    "token",
    "secret",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    # `header` 在这里特指 cookie 请求头字符串。它整串就是凭据，
    # 而 redact() 的 KV 规则认不出 `UID=xxx` 这类自定义名字，
    # 所以必须在**键名**层面拦掉，而不是指望值层面的正则。
    "header",
    "cookies",
    "sessionid",
    "session_id",
)
KV_RE = re.compile(
    r"(?i)\b(" + "|".join(SENSITIVE_KEYS) + r")\b(\s*[:=]\s*)(\"?)([^\s,;&'\"}\]]+)(\"?)"
)

#: JSON 风格的 "key": "value"
JSON_KV_RE = re.compile(
    r'(?i)"(' + "|".join(SENSITIVE_KEYS) + r')"\s*:\s*"([^"]*)"'
)

MASK = "***"


def mask_phone(value: str) -> str:
    """13812345678 -> 138****5678"""
    return PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", value)


def _mask_kv(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}{MASK}{match.group(5)}"


def redact(value: Any) -> Any:
    """递归脱敏字符串 / 容器。

    幂等：对已脱敏的内容再次调用不会产生额外变化。
    """
    if isinstance(value, str):
        text = mask_phone(value)
        text = JSON_KV_RE.sub(lambda m: f'"{m.group(1)}": "{MASK}"', text)
        text = KV_RE.sub(_mask_kv, text)
        return text
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                result[key] = MASK
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return type(value)(redact(item) for item in value)
    return value
