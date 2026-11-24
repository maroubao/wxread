"""仅续期并回写，网络失败保持可重试，连续确认失效后提醒重绑。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import reader as reader_mod
from .accounts import Account
from .logsafe import scrub
from .state import StateError, StateStore

log = logging.getLogger("wxread.warm")
WARM_REASON_OK = "renewed"
WARM_REASON_SKIPPED = "skipped"
WARM_REASON_FAILED = "renew_failed"
WARM_REASON_STATE = "state_error"


@dataclass
class WarmResult:
    account_id: str
    label: str = ""
    ok: bool = False
    reason: str = WARM_REASON_OK
    renewed_at: str = ""
    note: str = ""
    level: str = "L0"
    notify: bool = False

    def as_run(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "label": self.label, "ok": self.ok,
            "reason": self.reason, "renewed_at": self.renewed_at, "level": self.level,
            **({"note": self.note} if self.note else {}),
        }


@dataclass
class WarmSummary:
    results: list[WarmResult] = field(default_factory=list)

    @property
    def warmed(self) -> int:
        return sum(1 for result in self.results if result.ok)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if not result.ok and result.reason != WARM_REASON_SKIPPED)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.reason == WARM_REASON_SKIPPED)

    def as_dict(self) -> dict[str, Any]:
        return {"warmed": self.warmed, "failed": self.failed, "skipped": self.skipped, "accounts": [result.as_run() for result in self.results]}


def warm_account(
    store: StateStore,
    account: Account,
    *,
    transport_factory: Callable[[], Any] = reader_mod.RequestsTransport,
    now_iso: Callable[[], str],
) -> WarmResult:
    result = WarmResult(account_id=account.id, label=account.label)
    try:
        baseline = store.load_account(account.id)
        if baseline is not None:
            account = Account.from_dict(baseline, account.id)
            result.label = account.label
    except (StateError, ValueError, TypeError) as exc:
        return WarmResult(account.id, account.label, reason=WARM_REASON_STATE, level="L2", note=scrub(str(exc)))
    if baseline is None:
        return WarmResult(account.id, account.label, reason=WARM_REASON_SKIPPED, note="账户已删除")
    legacy = account.status == "needs_login" and "warm_failures" not in account.extra and account.note.startswith("renewal")
    if not account.enabled or (account.status != "ok" and not legacy):
        return WarmResult(account.id, account.label, reason=WARM_REASON_SKIPPED, note=account.note or "已停用", level="L3" if account.status == "needs_login" else "L0", notify=account.status == "needs_login" and int(account.extra.get("warm_failures", 0)) >= 2 and not account.extra.get("warm_notified", False))
    try:
        skey = reader_mod.Reader(account, transport_factory()).renew()
        account.mark_renewed(skey, now_iso())
        account.status = "ok"
        if account.note.startswith(("续期", "renewal")):
            account.note = ""
        account.extra.update({"warm_failures": 0, "warm_notified": False})
        result.ok = True
        result.renewed_at = account.renewed_at or ""
    except reader_mod.SessionInvalid as exc:
        failures = int(account.extra.get("warm_failures", 0)) + 1
        account.extra["warm_failures"] = failures
        result.reason = WARM_REASON_FAILED
        result.level = "L3" if failures >= 2 else "L2"
        result.note = scrub(str(exc))
        account.note = f"续期确认失效 {failures} 次"
        if failures >= 2:
            account.mark_needs_login("续期连续2次确认登录失效，重新扫码绑定")
            result.notify = not account.extra.get("warm_notified", False)
    except reader_mod.ReadError as exc:
        account.extra["warm_failures"] = 0
        result.reason = WARM_REASON_FAILED
        result.level = "L1" if isinstance(exc, reader_mod.TransportError) else "L2"
        result.note = scrub(str(exc))
    except Exception as exc:
        result.reason = WARM_REASON_FAILED
        result.level = "L2"
        result.note = scrub(str(exc))
    try:
        saved = store.save_account_runtime(account.to_dict(), baseline, f"chore(state): warm {account.id}")
        latest = Account.from_dict(saved, account.id) if saved is not None else None
        different_session = latest is not None and (any(getattr(latest, name) != getattr(account, name) for name in ("cookies", "headers", "read_payload")) or latest.extra.get("credential_revision") != account.extra.get("credential_revision"))
        if saved is None or different_session:
            result.ok = False
            result.reason = WARM_REASON_SKIPPED
            result.notify = False
        elif saved.get("status") != "needs_login":
            result.notify = False
    except StateError as exc:
        result.ok = False
        result.reason = WARM_REASON_STATE
        result.notify = False
        result.note = scrub(str(exc))
    return result


def warm_all(
    store: StateStore,
    *,
    transport_factory: Callable[[], Any] = reader_mod.RequestsTransport,
    now_iso: Callable[[], str],
    accounts: Iterable[Account] | None = None,
) -> WarmSummary:
    summary = WarmSummary()
    if accounts is None:
        items = []
        for account_id in store.list_account_ids():
            try:
                payload = store.load_account(account_id)
                if payload is not None:
                    items.append(Account.from_dict(payload, account_id))
            except (StateError, ValueError, TypeError) as exc:
                summary.results.append(WarmResult(account_id, reason=WARM_REASON_STATE, level="L2", note=scrub(str(exc))))
    else:
        items = list(accounts)
    for account in items:
        result = warm_account(store, account, transport_factory=transport_factory, now_iso=now_iso)
        log.info("保活 %s：%s", account.id, result.reason)
        summary.results.append(result)
    return summary


def render_summary(summary: WarmSummary) -> str:
    if not summary.results:
        return "没有可保活的账户。"
    lines = [f"保活完成：成功 {summary.warmed}，失败 {summary.failed}，跳过 {summary.skipped}"]
    for result in summary.results:
        if not result.ok:
            lines.append(f"- {result.label or result.account_id}: {result.reason}（{result.note}）")
    return "\n".join(lines)


def mapping_of(summary: WarmSummary) -> Mapping[str, Any]:
    return summary.as_dict()
