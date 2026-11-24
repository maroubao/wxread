"""通知：邮件 / PushPlus / 飞书 / Telegram。

规则（`docs/THREAT-MODEL.md` T-09）：正文只允许账户 label、状态、读书数、原因码；
所有文本出站前过 `logsafe.scrub()`，任何渠道都**不接受** cookie/token 字样。

渠道统一接口 `send(channel_cfg, title, text) -> str`：成功返回空串，失败抛 `NotifyError`。
"""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import smtplib
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Mapping

from .logsafe import scrub

CHANNELS = ("email", "pushplus", "feishu", "telegram")
HTTP_TIMEOUT = 15
DEFAULT_SUBJECT_PREFIX = "[wxread]"


class NotifyError(RuntimeError):
    """渠道发送失败（网络、鉴权、配置缺失）。"""


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"channel": self.channel, "ok": self.ok, "detail": scrub(self.detail)}


def render_run_report(records: list[Mapping]) -> tuple[str, str]:
    """把一批 run 记录拼成简报：一行一账户，绝不带 cookie。"""
    total = len(records)
    ok = sum(1 for r in records if r.get("level") == "L0")
    read = sum(int(r.get("read_count") or 0) for r in records)
    title = f"{DEFAULT_SUBJECT_PREFIX} 阅读完成 {ok}/{total} 账户，+{read} 次"
    lines = []
    for record in records:
        label = record.get("label") or record.get("account_id") or "?"
        level = record.get("level") or "-"
        count = int(record.get("read_count") or 0)
        reason = record.get("reason") or ""
        tail = f"（{reason}）" if reason and level != "L0" else ""
        lines.append(f"{label}：{level} +{count}{tail}")
    return title, "\n".join(lines)


def render_daily_report(records: list[Mapping], accounts: list[Mapping]) -> tuple[str, str]:
    by_account = {str(record.get("account_id")): record for record in records}
    enabled = [account for account in accounts if account.get("enabled", True)]
    completed = 0
    total_reads = sum(int(record.get("read_count") or 0) for record in records)
    lines = []
    for account in enabled:
        record = by_account.get(str(account.get("id")), {})
        count = int(record.get("read_count") or 0)
        target = int(record.get("target_count") or account.get("target_count") or 40)
        ok = bool(record) and count >= target and int(record.get("exit_code", 0)) == 0
        completed += int(ok)
        status = "完成" if ok else "需要重新扫码" if account.get("status") == "needs_login" else "未完成"
        if record.get("reason") == "cancelled":
            status = "已停止"
        if record.get("state_error"):
            status = "结果保存失败"
        lines.append(f"{account.get('label') or account.get('id')}：{count / 2:g} / {target / 2:g} 分，{status}")
    title = f"{DEFAULT_SUBJECT_PREFIX} 今日阅读 {completed}/{len(enabled)} 账户完成，{total_reads / 2:g} 分钟"
    return scrub(title), scrub("\n".join(lines) or "没有可运行账户，打开控制台扫码绑定。")


def render_failure(account_label: str, reason: str, hint: str = "") -> tuple[str, str]:
    title = f"{DEFAULT_SUBJECT_PREFIX} 需要重新扫码：{account_label}"
    text = f"账户：{account_label}\n原因：{reason}"
    if hint:
        text += f"\n建议：{hint}"
    text = scrub(text)
    return title, text


def render_read_alert(record: Mapping) -> tuple[str, str]:
    label = str(record.get("label") or record.get("account_id") or "账户")
    reason = str(record.get("reason") or "")
    if record.get("level") == "L3":
        if reason in {"payload_invalid", "no_synckey"}:
            return f"{DEFAULT_SUBJECT_PREFIX} 自动取参需重试：{label}", scrub(f"{label} 的阅读参数需要更新，控制台可重新自动获取。")
        return render_failure(label, reason, "重新扫码后恢复每日阅读")
    count = int(record.get("read_count") or 0)
    target = int(record.get("target_count") or 0)
    recovered = count >= target and int(record.get("exit_code", 0)) == 0
    status = "阅读已恢复" if recovered else "阅读未完成"
    if record.get("state_error"):
        status = "阅读结果保存失败"
    return f"{DEFAULT_SUBJECT_PREFIX} {status}：{label}", scrub(f"{label}：{count}/{target} 次，原因 {reason}")


def render_test(channel: str) -> tuple[str, str]:
    return f"{DEFAULT_SUBJECT_PREFIX} 测试通知（{channel}）", f"这是一条来自 wxread 的测试消息：{channel} 通道可用。"


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> str:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read(8192).decode("utf-8", "replace")
    except Exception as exc:  # 网络/证书/超时统一收口
        raise NotifyError(f"HTTP通知请求失败（{type(exc).__name__}）") from exc


def send_email(cfg: Mapping, title: str, text: str) -> str:
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    password = cfg.get("password") or ""
    to = cfg.get("to") or user
    if not host or not user or not password:
        raise NotifyError("邮件配置缺少 host/user/password")
    port = int(cfg.get("port") or 465)
    message = MIMEText(scrub(text), "plain", "utf-8")
    message["Subject"] = Header(scrub(title), "utf-8")
    message["From"] = formataddr(("wxread", user))
    message["To"] = to
    try:
        if port == 587:
            server = smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT)
        with server:
            server.login(user, password)
            server.sendmail(user, [item.strip() for item in to.split(",") if item.strip()], message.as_string())
    except Exception as exc:
        raise NotifyError(f"{type(exc).__name__}: {exc}") from exc
    return ""


def send_pushplus(cfg: Mapping, title: str, text: str) -> str:
    token = (cfg.get("token") or "").strip()
    if not token:
        raise NotifyError("PushPlus 缺少 token")
    body = json.dumps(
        {"token": token, "title": scrub(title), "content": scrub(text), "template": "txt"},
        ensure_ascii=False,
    ).encode("utf-8")
    raw = _post("https://www.pushplus.plus/send", body, {"Content-Type": "application/json"})
    return "" if '"code":200' in raw.replace(" ", "") else raw[:200]


def send_feishu(cfg: Mapping, title: str, text: str) -> str:
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        raise NotifyError("飞书缺少 webhook")
    payload = {"msg_type": "text", "content": {"text": f"{scrub(title)}\n{scrub(text)}"}}
    secret = str(cfg.get("secret") or "")
    if secret:
        timestamp = str(int(time.time()))
        signing_key = f"{timestamp}\n{secret}".encode("utf-8")
        payload.update({"timestamp": timestamp, "sign": base64.b64encode(hmac.new(signing_key, b"", hashlib.sha256).digest()).decode()})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    raw = _post(webhook, body, {"Content-Type": "application/json"})
    try:
        response = json.loads(raw)
    except ValueError:
        raise NotifyError("飞书响应不是JSON")
    return "" if response.get("code", response.get("StatusCode")) == 0 else str(response.get("msg") or response.get("StatusMessage") or "飞书接口返回失败")[:200]


def send_telegram(cfg: Mapping, title: str, text: str) -> str:
    token = (cfg.get("bot_token") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        raise NotifyError("Telegram 缺少 bot_token/chat_id")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": f"{scrub(title)}\n{scrub(text)}", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    raw = _post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})
    return "" if '"ok":true' in raw.replace(" ", "") else raw[:200]


_SENDERS = {
    "email": send_email,
    "pushplus": send_pushplus,
    "feishu": send_feishu,
    "telegram": send_telegram,
}


def send(channel: str, cfg: Mapping, title: str, text: str) -> NotifyResult:
    sender = _SENDERS.get(channel)
    if sender is None:
        return NotifyResult(channel, False, f"未知渠道 {channel}")
    try:
        leftover = sender(cfg, title, text)
    except (NotifyError, ValueError, TypeError) as exc:
        return NotifyResult(channel, False, str(exc))
    if leftover:
        return NotifyResult(channel, False, f"接口返回异常：{leftover}")
    return NotifyResult(channel, True)


def dispatch(settings: Mapping, title: str, text: str, channels: list[str] | None = None) -> list[NotifyResult]:
    """按 settings.notifications 发一遍；未启用则返回空列表（不是错误）。"""
    block = dict(settings.get("notifications") or {})
    if not block.get("enabled"):
        return []
    wanted = channels or list(block.get("channels") or [])
    results: list[NotifyResult] = []
    for channel in wanted:
        if channel not in CHANNELS:
            results.append(NotifyResult(channel, False, "未支持的渠道"))
            continue
        try:
            results.append(send(channel, dict(block.get(channel) or {}), scrub(title), scrub(text)))
        except Exception as exc:
            results.append(NotifyResult(channel, False, scrub(str(exc))))
    return results
