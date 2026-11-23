"""串行阅读队列，运行时字段局部回写，异常与取消均保存结果。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from uuid import uuid4

from .accounts import STATUS_OK, Account
from .logsafe import scrub
from .reader import DEFAULT_DEADLINE, DEFAULT_PACE, Outcome, Reader
from .state import StateError, StateStore, utcnow_iso

log = logging.getLogger("wxread.jobs")
SAVE_EVERY = 10


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
        self._baselines: dict[str, dict | None] = {}
        self._superseded: set[str] = set()

    def cancel(self) -> None:
        self._cancelled = True

    def should_stop(self) -> bool:
        return self._cancelled

    def _stop_account(self, account: Account) -> bool:
        if self._cancelled or account.id in self._superseded:
            return True
        try:
            current = self.store.load_account(account.id)
        except StateError as exc:
            log.warning("检查账户控制状态失败：%s", scrub(str(exc)))
            return False
        if current is None:
            return self._baselines.get(account.id) is not None
        return current.get("enabled") is False or current.get("status") != STATUS_OK

    def _sleep(self, seconds: float) -> None:
        if self.sleep is not time.sleep:
            self.sleep(seconds)
            return
        until = time.monotonic() + seconds
        while not self._cancelled:
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self.sleep(min(1.0, remaining))

    def _emit(self, kind: str, account_id: str, **data) -> None:
        data.pop("account_id", None)
        if self.on_event:
            try:
                self.on_event(JobEvent(kind, account_id, data))
            except Exception as exc:
                log.warning("事件回调失败：%s", scrub(str(exc)))

    def run_account(self, account: Account) -> dict:
        started_at = utcnow_iso()
        run_id = uuid4().hex
        outcome = Outcome(target_count=account.target_count)
        interrupted: BaseException | None = None
        self._baselines[account.id] = account.to_dict()
        account.extra["progress"] = {
            "run_id": run_id, "status": "running", "read_count": 0,
            "target_count": account.target_count, "started_at": started_at, "updated_at": started_at,
        }
        try:
            self._baselines[account.id] = self.store.load_account(account.id)
            self._save(account)
            reader = Reader(
                account,
                self.transport_factory() if self.transport_factory else None,
                pace=self.pace,
                deadline=self.deadline,
                sleep=self._sleep,
                clock=self.clock,
                on_progress=lambda info: self._progress(account, info),
                on_cookies=lambda _account: self._save(account),
                should_stop=lambda: self._stop_account(account),
            )
            self._emit("account_start", account.id, label=account.label, target=account.target_count)
            outcome = reader.run()
        except Exception as exc:
            count = int(account.extra["progress"].get("read_count", 0))
            outcome = Outcome(
                level="L2", reason="state_error" if isinstance(exc, StateError) else "internal_error",
                read_count=count, target_count=account.target_count,
                exit_code=4 if isinstance(exc, StateError) else (2 if count else 3),
                notes=[scrub(str(exc))],
            )
            log.error("账户 %s 执行异常：%s", account.id, scrub(str(exc)))
        except BaseException as exc:
            interrupted = exc
            count = int(account.extra["progress"].get("read_count", 0))
            outcome = Outcome("L2" if count else "L1", "cancelled", count, account.target_count, exit_code=2 if count else 3)
        finally:
            finished_at = utcnow_iso()
            record = outcome.as_run(account.id, started_at, finished_at)
            record.update({"run_id": run_id, "label": account.label})
            account.extra["progress"].update({
                "status": "cancelled" if outcome.reason == "cancelled" else "completed",
                "read_count": outcome.read_count, "updated_at": finished_at,
            })
            self._persist(account, record)
        self._emit("account_done", account.id, **{key: record[key] for key in ("level", "reason", "read_count", "target_count", "exit_code")})
        if interrupted is not None:
            raise interrupted
        return record

    def _progress(self, account: Account, info: dict) -> None:
        account.extra["progress"].update(info)
        account.extra["progress"]["updated_at"] = utcnow_iso()
        self._emit("progress", account.id, **{key: value for key, value in info.items() if key != "account_id"})
        count = int(info.get("read_count", 0))
        if count and count % SAVE_EVERY == 0:
            self._save(account)

    def _save(self, account: Account, *, strict: bool = False) -> None:
        baseline = self._baselines.get(account.id)
        fresh = account.to_dict()
        if account.id in self._superseded and baseline is not None:
            for name in ("cookies", "renewed_at", "status", "note"):
                fresh[name] = baseline.get(name)
        try:
            saved = self.store.save_account_runtime(fresh, baseline, f"chore(state): progress {account.id}")
            if saved is None:
                self._superseded.add(account.id)
                return
            self._baselines[account.id] = saved
            if saved.get("cookies") != account.cookies or saved.get("enabled") is False or saved.get("status") != STATUS_OK:
                self._superseded.add(account.id)
        except StateError as exc:
            if strict:
                raise
            log.warning("账户 %s 进度写回失败：%s", account.id, scrub(str(exc)))

    def _persist(self, account: Account, record: dict) -> None:
        try:
            self._save(account, strict=True)
        except StateError as exc:
            record.update({"state_error": scrub(str(exc)), "exit_code": 4})
            log.error("账户 %s 最终写回失败：%s", account.id, scrub(str(exc)))
        try:
            self.store.append_run(record)
        except StateError as exc:
            record.update({"state_error": scrub(str(exc)), "exit_code": 4})
            log.error("账户 %s 记录写回失败：%s", account.id, scrub(str(exc)))

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
