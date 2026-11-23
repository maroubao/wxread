"""会话保活：每 6 小时 renew 一次，避免 cookie 长期不动被服务端判为冷会话。

只做 renewal，不读书、不写阅读进度：
- 成功：wr_skey 与 renewed_at 在同一次 `save_account` 里写回（不变量见 docs/ARCHITECTURE.md §4）；
- 失败：置 `needs_login` 并留给通知层，阅读任务随后会跳过该账户。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import reader as reader_mod
from .accounts import Account, AccountError
from .logsafe import scrub
from .state import StateStore

log = logging.getLogger("wxread.warm")

WARM_REASON_OK = "renewed"
WARM_REASON_SKIPPED = "skipped"
WARM_REASON_FAILED = "renew_failed"


@dataclass
class WarmResult:
    account_id: str
    label: str = ""
    ok: bool = False
    reason: str = WARM_REASON_OK
    renewed_at: str = ""
    note: str = ""

    def as_run(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "label": self.label,
            "ok": self.ok,
            "reason": self.reason,
            "renewed_at": self.renewed_at,
            **({"note": self.note} if self.note else {}),
        }


@dataclass
class WarmSummary:
    results: list[WarmResult] = field(default_factory=list)

    @property
    def warmed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.reason == WARM_REASON_FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.reason == WARM_REASON_SKIPPED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "warmed": self.warmed,
            "failed": self.failed,
            "skipped": self.skipped,
            "accounts": [r.as_run() for r in self.results],
        }


def warm_account(
    store: StateStore,
    account,
    *,
    transport_factory: Callable[[], Any] = reader_mod.RequestsTransport,
    now_iso: Callable[[], str],
) -> WarmResult:
    """对单个账户做一次 renewal，成功即写回新 skey。"""
    result = WarmResult(account_id=account.id, label=account.label)
    transport = transport_factory()
    try:
        skey = reader_mod.Reader(account, transport).renew()
    except reader_mod.ReadError as exc:
        result.ok = False
        result.reason = WARM_REASON_FAILED
        result.note = scrub(str(exc))[:200]
        account.mark_needs_login(result.note)
        try:
            store.save_account(account)
        except Exception as save_exc:  # 写不进去也要把失败带回去
            log.warning("保活失败后写回状态也失败：%s", scrub(str(save_exc)))
        return result

    when = now_iso()
    account.mark_renewed(skey, when)  # 不变量：skey 与 renewed_at 同一次写入
    store.save_account(account)
    result.ok = True
    result.renewed_at = when
    return result


def warm_all(
    store: StateStore,
    *,
    transport_factory: Callable[[], Any] = reader_mod.RequestsTransport,
    now_iso: Callable[[], str],
    accounts: Iterable[Any] | None = None,
) -> WarmSummary:
    """遍历启用中的账户逐个保活；只保 `ok`，`needs_login` 等人来扫码。"""
    summary = WarmSummary()
    if accounts is None:
        items = []
        for account_id, payload in store.load_accounts().items():
            try:
                items.append(Account.from_dict(payload, account_id))
            except (AccountError, ValueError) as exc:  # 坏账户不阻塞整轮保活
                log.error("账户 %s 无法解析：%s", account_id, scrub(str(exc)))
    else:
        items = list(accounts)
    for account in items:
        if not account.enabled:
            summary.results.append(WarmResult(account_id=account.id, label=account.label, reason=WARM_REASON_SKIPPED, note="已停用"))
            continue
        if account.status != "ok":
            summary.results.append(
                WarmResult(account_id=account.id, label=account.label, reason=WARM_REASON_SKIPPED, note=f"状态 {account.status}")
            )
            continue
        result = warm_account(store, account, transport_factory=transport_factory, now_iso=now_iso)
        log.info("保活 %s：%s", account.label or account.id, result.reason)
        summary.results.append(result)
    return summary


def render_summary(summary: WarmSummary) -> str:
    if not summary.results:
        return "没有可保活的账户。"
    lines = [f"保活完成：成功 {summary.warmed}，失败 {summary.failed}，跳过 {summary.skipped}"]
    for result in summary.results:
        if result.ok:
            continue
        detail = f"（{result.note}）" if result.note else ""
        lines.append(f"- {result.label or result.account_id}: {result.reason}{detail}")
    return "\n".join(lines)


def mapping_of(summary: WarmSummary) -> Mapping[str, Any]:
    return summary.as_dict()
