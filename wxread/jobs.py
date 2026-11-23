"""串行作业队列：逐账户跑 Reader，边跑边落盘，产出 runs 记录。

串行的原因：同一账户并发会互相顶掉 wr_skey；不同账户并发价值也不大（额度按次计）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .accounts import STATUS_OK, Account
from .reader import DEFAULT_DEADLINE, DEFAULT_PACE, Reader
from .state import StateError, StateStore, utcnow_iso

log = logging.getLogger("wxread.jobs")

SAVE_EVERY = 40


@dataclass
class JobEvent:
    kind: str
    account_id: str
    data: dict

    def as_dict(self) -> dict:
        return {"kind": self.kind, "account_id": self.account_id, **self.data}


class JobRunner:
    def __init__(
        self,
        store: StateStore,
        *,
        transport_factory: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        pace: float = DEFAULT_PACE,
        deadline: float = DEFAULT_DEADLINE,
        on_event: Callable[[JobEvent], None] | None = None,
    ) -> None:
        self.store = store
        self.transport_factory = transport_factory
        self.clock = clock
        self.sleep = sleep
        self.pace = pace
        self.deadline = deadline
        self.on_event = on_event
        self._cancelled = False

    # -- 控制

    def cancel(self) -> None:
        self._cancelled = True

    def should_stop(self) -> bool:
        return self._cancelled

    def _emit(self, kind: str, account_id: str, **data) -> None:
        data.pop("account_id", None)  # 由参数给出，避免与进度回调里的同名字段冲突
        if self.on_event:
            self.on_event(JobEvent(kind, account_id, data))

    # -- 执行

    def run_account(self, account: Account) -> dict:
        started_at = utcnow_iso()
        reader = Reader(
            account,
            self.transport_factory() if self.transport_factory else None,
            pace=self.pace,
            deadline=self.deadline,
            sleep=self.sleep,
            clock=self.clock,
            on_progress=lambda info: self._progress(account, info),
            should_stop=self.should_stop,
        )
        self._emit("account_start", account.id, label=account.label, target=account.target_count)
        outcome = reader.run()
        record = outcome.as_run(account.id, started_at, utcnow_iso())
        record["label"] = account.label
        self._persist(account, record)
        self._emit(
            "account_done",
            account.id,
            level=outcome.level,
            reason=outcome.reason,
            read_count=outcome.read_count,
            target=account.target_count,
            exit_code=outcome.exit_code,
        )
        return record

    def _progress(self, account: Account, info: dict) -> None:
        self._emit("progress", account.id, **{k: v for k, v in info.items() if k != "account_id"})
        count = int(info.get("read_count", 0))
        if count and count % SAVE_EVERY == 0:
            self._save(account)

    def _save(self, account: Account) -> None:
        try:
            self.store.save_account(account.to_dict(), f"chore(state): progress {account.id}")
        except StateError as exc:  # 进度写不进去不该中断阅读，最终写回还会再试
            log.warning("账户 %s 进度写回失败：%s", account.id, exc)

    def _persist(self, account: Account, record: dict) -> None:
        try:
            self.store.save_account(account.to_dict(), f"chore(state): account {account.id}")
            self.store.append_run(record)
        except StateError as exc:
            log.error("账户 %s 结果写回失败：%s", account.id, exc)
            record["level"] = "L3" if record.get("level") == "L0" else record.get("level")
            record["state_error"] = str(exc)
            record["exit_code"] = 4

    def run(self, accounts: Iterable[Account]) -> list[dict]:
        records: list[dict] = []
        for account in accounts:
            if self.should_stop():
                self._emit("cancelled", account.id)
                break
            if not account.enabled or account.status != STATUS_OK:
                self._emit("skipped", account.id, status=account.status, enabled=account.enabled)
                continue
            records.append(self.run_account(account))
        return records
