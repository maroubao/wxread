"""日志脱敏：cookie、token、密钥一律只留前 2 位。

THREAT-MODEL T-01：任何进入日志、异常、commit message 的敏感值都必须先过这里。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# 值本身敏感的键（小写比较）
SENSITIVE_KEYS = frozenset(
    {
        "wr_skey",
        "wr_vid",
        "skey",
        "vid",
        "cookie",
        "set-cookie",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "smtp_password",
        "pushplus_token",
        "telegram_bot_token",
        "feishu_webhook",
        "wxread_state_key",
        "wxread_control_key",
        "github_token",
        "secret",
        "state_key",
        "control_key",
    }
)

VISIBLE_PREFIX = 2

_PATTERNS = (
    re.compile(r"(?i)\b(wr_skey|wr_vid|wr_rt)\s*[=:]\s*([^\s;&'\"]+)"),
    re.compile(r"(?i)\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(token|password|secret)(\s*[=:]\s*)([^\s&,'\"]{4,})"),
)


def mask(value: Any) -> str:
    """把单个值变成 `ab***`。非字符串统一 str() 后再处理。"""
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    if len(text) <= VISIBLE_PREFIX:
        return "***"
    return f"{text[:VISIBLE_PREFIX]}***"


def scrub(text: str) -> str:
    """从自由文本里抹掉凭据片段（用于异常消息、curl 原文、响应片段）。"""
    if not text:
        return ""
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}={mask(m.group(2))}", text)
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}_***", out)
    out = _PATTERNS[2].sub(lambda m: f"{m.group(1)} ***", out)
    out = _PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}{mask(m.group(3))}", out)
    return out


def scrub_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """按键名脱敏，返回新 dict（不改原对象）。"""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            result[key] = mask(value)
        elif isinstance(value, Mapping):
            result[key] = scrub_mapping(value)
        elif isinstance(value, str):
            result[key] = scrub(value)
        else:
            result[key] = value
    return result


def cookies_brief(cookies: Mapping[str, str]) -> str:
    """只用于日志：`wr_skey=wr***, wr_vid=ab***`。"""
    return ", ".join(f"{k}={mask(v)}" for k, v in sorted(cookies.items()))
