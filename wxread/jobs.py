"""串行阅读队列，运行时字段局部回写，异常与取消均保存结果。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from uuid import uuid4

from .accounts import STATUS_OK, Account
from .logsafe import scrub
from .reader import DEFAULT_PACE, Outcome, Reader
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
        deadline: float | None = None,
        on_event: Callable[[JobEvent], None] | None = None,
        batch_id: str = "",
        existing_only: bool = False,
    ) -> None:
        self.store = store
        self.transport_factory = transport_factory
        self.clock = clock
        self.sleep = sleep
        self.pace = pace
        self.deadline = deadline
        self.on_event = on_event
        self.batch_id = batch_id
        self.existing_only = existing_only
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
        baseline = self._baselines.get(account.id) or {}
        changed = any(current.get(name) != baseline.get(name) for name in ("cookies", "headers", "read_payload", "credential_revision"))
        progress = current.get("progress") or {}
        own_run = account.extra["progress"]["run_id"]
        replaced = bool(progress.get("run_id") and progress["run_id"] != own_run)
        cancelled = progress.get("run_id") == own_run and progress.get("cancel_requested")
        return changed or replaced or current.get("enabled") is False or current.get("status") != STATUS_OK or bool(cancelled)

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
            "cancel_requested": False,
            "batch_id": self.batch_id,
        }
        try:
            loaded = self.store.load_account(account.id)
            if loaded is not None or not self.existing_only:
                self._baselines[account.id] = loaded
            if loaded is not None:
                latest = Account.from_dict(loaded, account.id)
                for name in ("cookies", "headers", "read_payload", "status", "note", "renewed_at", "last_ok_at", "label", "enabled"):
                    setattr(account, name, getattr(latest, name))
                account.extra.update({key: value for key, value in latest.extra.items() if key != "progress"})
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
            if outcome.read_count < account.target_count and self._stop_account(account):
                outcome.reason = "cancelled"
                outcome.exit_code = 2 if outcome.read_count else 3
            finished_at = utcnow_iso()
            record = outcome.as_run(account.id, started_at, finished_at)
            record.update({"run_id": run_id, "label": account.label})
            if self.batch_id:
                record["batch_id"] = self.batch_id
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
            for name in ("cookies", "headers", "read_payload", "credential_revision", "renewed_at", "last_ok_at", "status", "note", "warm_failures", "warm_notified"):
                fresh[name] = baseline.get(name)
        try:
            saved = self.store.save_account_runtime(fresh, baseline, f"chore(state): progress {account.id}")
            if saved is None:
                self._superseded.add(account.id)
                return
            self._baselines[account.id] = saved
            latest = Account.from_dict(saved, account.id)
            different_session = any(getattr(latest, name) != getattr(account, name) for name in ("cookies", "headers", "read_payload")) or latest.extra.get("credential_revision") != account.extra.get("credential_revision")
            different_run = (saved.get("progress") or {}).get("run_id") != account.extra["progress"]["run_id"]
            if different_session or different_run or saved.get("enabled") is False or saved.get("status") != STATUS_OK:
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
            if record["level"] in {"L0", "L1"}:
                record.update({"level": "L2", "reason": "state_error"})
            log.error("账户 %s 最终写回失败：%s", account.id, scrub(str(exc)))
        try:
            self.store.append_run(record)
        except StateError as exc:
            record.update({"state_error": scrub(str(exc)), "exit_code": 4})
            if record["level"] in {"L0", "L1"}:
                record.update({"level": "L2", "reason": "state_error"})
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
