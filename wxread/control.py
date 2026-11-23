"""指令解封与执行（`control.yml` 的大脑）。

信任模型见 `docs/THREAT-MODEL.md` §6：

- 指令由页面用**仓库公钥**加密（临时 X25519 + 显式 nonce），worker 用 Actions Secret 里的私钥解；
- 指令必须带 `session_id` / `issued_at` / `nonce`；`issued_at` 超出 60 秒窗口即拒；
- `sessions/<id>.enc.json` 用 `create_only` 抢锁，抢到即视为"已消费"（T-02），重复 dispatch 同一份密文会失败；
- 执行结果用**页面临时公钥**加密写回 `responses/<id>.enc.json`（T-03），10 分钟后由清理任务删除。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import notify, qr
from .accounts import Account, MissingFieldsError, account_from_curl, clamp_target_count, normalize_read_payload
from .crypto import CryptoFailure, b64d, b64e, seal, unseal
from .logsafe import scrub
from .state import StateError, StateStore, utcnow_iso

log = logging.getLogger(__name__)

ACTIONS = (
    "init",
    "qr",
    "import",
    "update",
    "delete",
    "schedule",
    "notifications",
    "test-mail",
    "test-push",
)
DEFAULT_SKEW = 60
DEFAULT_DAILY = "01:07"
DEFAULT_WARM_HOURS = 6
PUBLIC_KEY_LEN = 32


class ControlError(RuntimeError):
    """指令不可执行（格式、时效、重放、业务校验）。"""


@dataclass
class Instruction:
    action: str
    session_id: str
    issued_at: int
    nonce: str
    payload: dict = field(default_factory=dict)
    reply_key: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Instruction":
        action = str(data.get("action") or "")
        session_id = str(data.get("session_id") or "")
        if not action or not session_id:
            raise ControlError("指令缺少 action/session_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ControlError("session_id 只能包含字母、数字、下划线与短横线")
        try:
            issued_at = int(data.get("issued_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ControlError("issued_at 不是整数") from exc
        return cls(
            action=action,
            session_id=session_id,
            issued_at=issued_at,
            nonce=str(data.get("nonce") or ""),
            payload=dict(data.get("payload") or {}),
            reply_key=str(data.get("reply_key") or ""),
        )


def decode(envelope: Mapping[str, Any], secret_key: bytes) -> Instruction:
    try:
        data = unseal(envelope, secret_key)
    except CryptoFailure as exc:
        raise ControlError(f"指令解封失败：{exc}") from exc
    return Instruction.from_dict(data)


def check_fresh(instruction: Instruction, *, now: int | None = None, skew: int = DEFAULT_SKEW) -> None:
    current = int(now if now is not None else time.time())
    age = current - instruction.issued_at
    if age > skew:
        raise ControlError(f"指令过期（{age}s > {skew}s）")
    if age < -skew:
        raise ControlError(f"指令时间超前（{-age}s）")
    if not instruction.nonce or len(instruction.nonce) < 8:
        raise ControlError("指令缺少 nonce")


def check_action(instruction: Instruction) -> None:
    if instruction.action not in ACTIONS:
        raise ControlError(f"未知动作 {instruction.action}")


@dataclass
class ControlContext:
    """处理器依赖：全部可注入，便于测试。"""

    store: StateStore
    now_iso: Callable[[], str] = utcnow_iso
    qr_page_factory: Callable[[], qr.LoginPage] | None = None
    qr_timeout: float = qr.DEFAULT_TIMEOUT
    qr_out: Callable[[qr.QrShot], None] | None = None
    qr_progress: Callable[[dict], None] | None = None
    notify_send: Callable[..., list] | None = None


def _accounts_as_dicts(store: StateStore) -> dict[str, dict]:
    return store.load_accounts()


def handle_init(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    """建或换指令密钥对。

    公钥可以由调用方给（`payload["public_key"]`，本地 genkey 生成后传入）；
    不给才在进程内生成——但生成路径的私钥只能在本机用，CI 日志是公开的，
    `cmd_control` 因此拒绝在无公钥时走这条路。
    """
    control = ctx.store.load_control()
    stored = control.get("public_key") if control else None
    supplied = payload.get("public_key")
    if supplied:
        try:
            public = b64d(str(supplied))
        except (ValueError, CryptoFailure) as exc:
            raise ControlError("public_key 不是合法 base64") from exc
        if len(public) != PUBLIC_KEY_LEN:
            raise ControlError(f"public_key 必须是 {PUBLIC_KEY_LEN} 字节")
        text = b64e(public)
        if stored == text:
            return {"public_key": text, "key_version": (control or {}).get("key_version", 1), "reused": True}
        saved = ctx.store.save_control(public)
        # 旧私钥随即失效：页面必须用新公钥重新封指令
        return {
            "public_key": saved["public_key"],
            "key_version": saved.get("key_version", 1),
            "reused": False,
            "rotated": bool(stored),
        }

    if stored:
        return {"public_key": stored, "key_version": (control or {}).get("key_version", 1), "reused": True}

    from .crypto import new_keypair

    public, secret = new_keypair()
    saved = ctx.store.save_control(public)
    # 私钥只在这一条回执里出现一次；后续由 Secret 管理，不再回传
    return {
        "public_key": saved["public_key"],
        "key_version": saved.get("key_version", 1),
        "server_secret_key": b64e(secret),
        "reused": False,
    }


def handle_qr(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    factory = ctx.qr_page_factory or qr.ChromeLoginPage
    page = factory()
    try:
        page.open()
        result = qr.wait_for_login(
            page,
            timeout=float(payload.get("timeout") or ctx.qr_timeout),
            on_qr=ctx.qr_out,
        )
        if not result.ok:
            return {"ok": False, "reason": result.reason, "qr_count": len(result.shots)}
        if ctx.qr_progress:
            ctx.qr_progress({"stage": "capturing", "message": "登录已确认，正在获取阅读参数"})
        cookies = page.cookies()
        payload_defaults = dict(payload.get("read_payload") or {})
        captured, capture_headers = _capture_read_payload(page)
        cookies.update(page.cookies())
        read_payload = dict(payload_defaults)
        read_payload.update({k: v for k, v in captured.items() if v is not None and str(v).strip()})
        headers = {"User-Agent": page.user_agent()}
        headers.update(capture_headers)
        account = qr.account_from_cookies(
            cookies,
            headers=headers,
            label=str(payload.get("label") or "")[:24],
            target_count=clamp_target_count(payload.get("target_count")),
            payload=read_payload,
            now_iso=ctx.now_iso(),
        )
        account.mark_renewed(cookies.get("wr_skey", ""), ctx.now_iso())
        rebound = False

        def bind(existing: dict | None) -> dict:
            nonlocal rebound
            rebound = existing is not None
            merged = _merge_accounts(existing, account) if existing is not None else account
            if merged.read_payload.get("ps") and merged.read_payload.get("pc"):
                merged.note = ""
            return merged.to_dict()

        saved = ctx.store.mutate_account(account.id, bind, "control: qr bind")
        return {
            "ok": True,
            "account_id": account.id,
            "label": saved["label"],
            "rebound": rebound,
            "payload_captured": bool(captured),
        }
    finally:
        page.close()


def _capture_read_payload(page: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """登录态下顺手把 `web/book/read` 的参数抓回来（抓不到就算了，不打断扫码）。"""
    capture = getattr(page, "capture_read_payload", None)
    if not callable(capture):
        return {}, {}
    try:
        captured, headers = capture()
    except Exception as exc:  # noqa: BLE001 - 抓包失败只能退回手工补
        log.warning("自动抓取阅读参数失败：%s", scrub(str(exc)))
        return {}, {}
    if not captured:
        log.warning("自动抓取阅读参数失败：页面没有发出 web/book/read")
        return {}, {}
    return dict(captured), dict(headers or {})


def _merge_accounts(
    existing: Mapping[str, Any],
    fresh: Account,
    *,
    keep_label: bool = True,
    take_target: bool = False,
) -> Account:
    """把新解码出来的账户合到已存的账户上。

    `Account.merge()` 是原地改 self 并返回 None，直接 `account = x.merge(y)` 会拿到 None，
    后面 `.to_dict()` 就炸——这就是 import 报 `'NoneType' object has no attribute 'to_dict'` 的原因。
    """
    base = Account.from_dict(existing)
    base.merge(fresh, keep_label=keep_label)
    if take_target:
        base.target_count = fresh.target_count
    return base


def handle_import(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    curl = str(payload.get("curl") or "").strip()
    if not curl:
        raise ControlError("import 需要 curl")
    account = account_from_curl(
        curl,
        label=str(payload.get("label") or "")[:24],
        target_count=clamp_target_count(payload.get("target_count")),
        now=ctx.now_iso(),
    )
    updated = False

    def merge(existing: dict | None) -> dict:
        nonlocal updated
        updated = existing is not None
        fresh = account
        if existing is not None:
            fresh = _merge_accounts(existing, account, keep_label=not payload.get("label"), take_target="target_count" in payload)
        fresh.note = ""
        return fresh.to_dict()

    saved = ctx.store.mutate_account(account.id, merge, "control: import")
    return {"ok": True, "account_id": account.id, "label": saved["label"], "updated": updated}


def handle_update(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    account_id = str(payload.get("account_id") or "")
    if not account_id:
        raise ControlError("update 需要 account_id")

    def update(raw: dict | None) -> dict:
        if raw is None:
            raise ControlError(f"账户不存在 {account_id}")
        account = Account.from_dict(raw)
        if "label" in payload:
            account.label = str(payload["label"])[:24]
        if "target_count" in payload:
            account.target_count = clamp_target_count(payload["target_count"])
        if "enabled" in payload:
            account.enabled = bool(payload["enabled"])
        if "status" in payload:
            status = str(payload["status"])
            if status not in {"ok", "needs_login", "stopped"}:
                raise ControlError(f"非法状态 {status}")
            account.status = status
        if "note" in payload:
            account.note = str(payload["note"])[:200]
        if "read_payload" in payload:
            account.read_payload.update(normalize_read_payload(payload["read_payload"]))
            account.validate()
        return account.to_dict()

    saved = ctx.store.mutate_account(account_id, update, "control: update")
    return {"ok": True, "account_id": account_id, "label": saved["label"]}


def handle_delete(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    account_id = str(payload.get("account_id") or "")
    if not account_id:
        raise ControlError("delete 需要 account_id")
    if not ctx.store.load_account(account_id):
        raise ControlError(f"账户不存在 {account_id}")
    ctx.store.delete_account(account_id, message="control: delete")
    return {"ok": True, "account_id": account_id, "deleted": True}


def handle_schedule(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    def update(settings: dict) -> dict:
        schedule = dict(settings.get("schedule") or {})
        if "enabled" in payload:
            schedule["enabled"] = bool(payload["enabled"])
        if payload.get("daily"):
            hour, separator, minute = str(payload["daily"]).partition(":")
            if not separator or not hour.isdigit() or not minute.isdigit():
                raise ControlError("daily 需形如 01:07")
            if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
                raise ControlError("daily 超出时间范围")
            schedule["daily"] = f"{int(hour):02d}:{int(minute):02d}"
        if "warm_hours" in payload:
            hours = int(payload["warm_hours"])
            if hours not in (0, 1, 3, 6, 12):
                raise ControlError("warm_hours 只允许 0/1/3/6/12")
            schedule["warm_hours"] = hours
        schedule.setdefault("daily", DEFAULT_DAILY)
        schedule.setdefault("warm_hours", DEFAULT_WARM_HOURS)
        settings["schedule"] = schedule
        settings["updated_at"] = ctx.now_iso()
        return settings

    settings = ctx.store.mutate_settings(update)
    return {"ok": True, "schedule": settings["schedule"]}


def handle_notifications(ctx: ControlContext, payload: Mapping[str, Any]) -> dict:
    def update(settings: dict) -> dict:
        block = dict(settings.get("notifications") or {})
        if "enabled" in payload:
            block["enabled"] = bool(payload["enabled"])
        if "channels" in payload:
            channels = [str(item) for item in payload["channels"]]
            unknown = [item for item in channels if item not in notify.CHANNELS]
            if unknown:
                raise ControlError(f"未知渠道 {unknown}")
            block["channels"] = channels
        for channel in notify.CHANNELS:
            if channel in payload and isinstance(payload[channel], Mapping):
                merged = dict(block.get(channel) or {})
                merged.update(dict(payload[channel]))
                block[channel] = merged
        settings["notifications"] = block
        settings["updated_at"] = ctx.now_iso()
        return settings

    settings = ctx.store.mutate_settings(update)
    block = settings["notifications"]
    safe = {k: ({"configured": True} if k in notify.CHANNELS else v) for k, v in block.items()}
    return {"ok": True, "notifications": safe}


def handle_test(ctx: ControlContext, payload: Mapping[str, Any], *, kind: str) -> dict:
    settings = ctx.store.load_settings()
    title, text = notify.render_test(kind)
    sender = ctx.notify_send or notify.send
    if kind == "test-mail":
        results = [sender("email", dict((settings.get("notifications") or {}).get("email") or {}), title, text)]
    else:
        channel = str(payload.get("channel") or "")
        if channel not in notify.CHANNELS:
            raise ControlError("test-push 需要合法 channel")
        results = [sender(channel, dict((settings.get("notifications") or {}).get(channel) or {}), title, text)]
    return {"ok": all(item.ok for item in results), "results": [item.as_dict() for item in results]}


_HANDLERS: dict[str, Callable[[ControlContext, Mapping[str, Any]], dict]] = {
    "init": handle_init,
    "qr": handle_qr,
    "import": handle_import,
    "update": handle_update,
    "delete": handle_delete,
    "schedule": handle_schedule,
    "notifications": handle_notifications,
    "test-mail": lambda ctx, payload: handle_test(ctx, payload, kind="test-mail"),
    "test-push": lambda ctx, payload: handle_test(ctx, payload, kind="test-push"),
}


def _write_receipt(ctx: ControlContext, instruction: Instruction, receipt: Mapping[str, Any]) -> None:
    envelope: dict = {"v": 0, "plain": dict(receipt)}
    if instruction.reply_key:
        try:
            envelope = seal(receipt, b64d(instruction.reply_key))
        except CryptoFailure as exc:
            raise ControlError("回执公钥无效") from exc
    ctx.store.write_response(instruction.session_id, envelope)


def execute(
    ctx: ControlContext,
    instruction: Instruction,
    *,
    now: int | None = None,
    skew: int = DEFAULT_SKEW,
) -> dict:
    """校验、消费会话、执行并写加密回执，拒绝的指令也产生失败回执。"""
    receipt: dict = {"ok": False, "action": instruction.action, "session_id": instruction.session_id, "at": ctx.now_iso()}
    try:
        check_action(instruction)
        check_fresh(instruction, now=now, skew=skew)
        try:
            ctx.store.claim_session(
                instruction.session_id,
                {"action": instruction.action, "issued_at": instruction.issued_at, "claimed_at": ctx.now_iso()},
            )
        except StateError as exc:
            raise ControlError(f"session 已被消费（重放防护）：{scrub(str(exc))}") from exc
    except ControlError as exc:
        receipt["error"] = scrub(str(exc))
        if ctx.store.read_response(instruction.session_id) is None:
            _write_receipt(ctx, instruction, receipt)
        raise

    previous_progress = ctx.qr_progress
    if instruction.action == "qr":
        ctx.qr_progress = lambda progress: _write_receipt(ctx, instruction, {**receipt, **progress, "pending": True})
    try:
        result = _HANDLERS[instruction.action](ctx, instruction.payload)
        receipt.update(result)
        receipt["ok"] = bool(result.get("ok", True))
    except MissingFieldsError as exc:
        receipt.update({"ok": False, "error": str(exc), "missing_fields": exc.fields})
    except ControlError as exc:
        receipt.update({"ok": False, "error": scrub(str(exc))})
    except Exception as exc:
        receipt.update({"ok": False, "error": f"{type(exc).__name__}: {scrub(str(exc))}"})
    finally:
        ctx.qr_progress = previous_progress
    _write_receipt(ctx, instruction, receipt)
    return receipt


def summary(receipt: Mapping[str, Any]) -> str:
    if receipt.get("ok"):
        return f"{receipt.get('action')} ok"
    return f"{receipt.get('action')} 失败：{scrub(str(receipt.get('error') or ''))}"
