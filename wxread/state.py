"""状态层：GitHub Contents 读写 + CAS 写回 + 分账户文件 + 月度 runs。

分支布局见 ARCHITECTURE §3：
    control.json              明文 {public_key, key_version, updated_at}
    settings.enc.json         密文
    accounts/<id>.enc.json    密文，每账户一份
    runs/<yyyy-mm>.enc.json   密文，月度分片，保留 200 条
    sessions/<id>.enc.json    密文，一次性消费凭证（防重放）
    responses/<id>.json       已加密的回执封套（明文容器，内容不可读）

写入一律 CAS：先取 sha，再带 sha PUT；冲突则回读比对内容（响应丢失但实际写成功的情形），
不同才重试，最多 `retries` 次，超限抛 StateWriteError。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

import requests

from . import crypto
from .logsafe import scrub

log = logging.getLogger("wxread.state")

CONTROL_PATH = "control.json"
SETTINGS_PATH = "settings.enc.json"
ACCOUNTS_DIR = "accounts"
RUNS_DIR = "runs"
SESSIONS_DIR = "sessions"
RESPONSES_DIR = "responses"
RUNS_KEEP = 200
DEFAULT_BRANCH = "wxread-state"


class StateError(RuntimeError):
    """状态层错误基类。"""


class StateWriteError(StateError):
    """CAS 冲突超限或写入失败。"""


class StateReadError(StateError):
    """读取或解码失败。"""


class Conflict(StateError):
    """后端报告的版本冲突（sha 不匹配 / 创建已存在）。"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 后端


@runtime_checkable
class ContentsBackend(Protocol):
    """最小后端接口；GitHub 与测试内存实现都满足它。"""

    def get(self, path: str) -> tuple[bytes | None, str | None]: ...

    def put(
        self,
        path: str,
        data: bytes,
        message: str,
        sha: str | None = None,
        create_only: bool = False,
    ) -> str: ...

    def delete(self, path: str, message: str, sha: str | None = None) -> None: ...

    def list(self, directory: str) -> list[str]: ...


class GitHubContents:
    """GitHub Contents API 后端。只碰一个分支，不做别的事。"""

    API = "https://api.github.com"

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = DEFAULT_BRANCH,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        api: str = API,
    ) -> None:
        if "/" not in repo:
            raise StateError("repo 必须是 owner/name")
        if not token:
            raise StateError("缺少 GITHUB_TOKEN")
        self.repo = repo
        self.branch = branch
        self._session = session or requests.Session()
        self._timeout = timeout
        self._api = api.rstrip("/")
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # -- 基础设施

    def _url(self, path: str) -> str:
        return f"{self._api}/repos/{self.repo}/contents/{path.lstrip('/')}"

    def run_created_at(self, run_id: str) -> int:
        """GitHub 接收本次运行的时间，包含 runner 排队之前的时刻。"""
        if not str(run_id).isdigit():
            raise StateReadError("GITHUB_RUN_ID 不是合法运行编号")
        try:
            response = self._session.get(
                f"{self._api}/repos/{self.repo}/actions/runs/{run_id}", timeout=self._timeout
            )
            if response.status_code != 200:
                raise StateReadError(f"读取运行时间失败：HTTP {response.status_code}")
            created = datetime.fromisoformat(response.json()["created_at"].replace("Z", "+00:00"))
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            raise StateReadError("无法读取 GitHub 接收运行的时间") from exc
        if created.tzinfo is None:
            raise StateReadError("GitHub 运行时间缺少时区")
        return int(created.timestamp())

    def ensure_branch(self, source: str = "main") -> None:
        """分支不存在时从 source 创建（空仓库则无从创建，抛 StateError）。"""
        ref_url = f"{self._api}/repos/{self.repo}/git/ref/heads/{self.branch}"
        resp = self._session.get(ref_url, timeout=self._timeout)
        if resp.status_code == 200:
            return
        if resp.status_code not in (404, 409):
            raise StateError(f"查询分支失败：HTTP {resp.status_code}")
        src = self._session.get(f"{self._api}/repos/{self.repo}/git/ref/heads/{source}", timeout=self._timeout)
        if src.status_code != 200:
            raise StateError(f"源分支 {source} 不存在：HTTP {src.status_code}")
        sha = src.json()["object"]["sha"]
        created = self._session.post(
            f"{self._api}/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{self.branch}", "sha": sha},
            timeout=self._timeout,
        )
        if created.status_code not in (201, 422):
            raise StateError(f"创建分支失败：HTTP {created.status_code}")

    # -- 接口

    def get(self, path: str) -> tuple[bytes | None, str | None]:
        try:
            resp = self._session.get(self._url(path), params={"ref": self.branch}, timeout=self._timeout)
        except requests.RequestException as exc:
            raise StateReadError(f"读取 {path} 网络失败：{type(exc).__name__}") from exc
        if resp.status_code == 404:
            return None, None
        if resp.status_code != 200:
            raise StateReadError(f"读取 {path} 失败：HTTP {resp.status_code}")
        payload = resp.json()
        if isinstance(payload, list):
            raise StateReadError(f"{path} 是目录，请用 list()")
        content = payload.get("content", "")
        try:
            raw = base64.b64decode(content, validate=False) if content else b""
        except ValueError as exc:
            raise StateReadError(f"{path} 内容不是合法 base64") from exc
        return raw, payload.get("sha")

    def list(self, directory: str) -> list[str]:
        try:
            resp = self._session.get(self._url(directory), params={"ref": self.branch}, timeout=self._timeout)
        except requests.RequestException as exc:
            raise StateReadError(f"列目录 {directory} 网络失败：{type(exc).__name__}") from exc
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise StateReadError(f"列目录 {directory} 失败：HTTP {resp.status_code}")
        payload = resp.json()
        if not isinstance(payload, list):
            raise StateReadError(f"{directory} 不是目录")
        return sorted(item["name"] for item in payload if item.get("type") == "file")

    def put(
        self,
        path: str,
        data: bytes,
        message: str,
        sha: str | None = None,
        create_only: bool = False,
    ) -> str:
        body: dict[str, Any] = {
            "message": scrub(message) or "chore(state): update",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        headers = {}
        if sha:
            body["sha"] = sha
        elif create_only:
            headers["If-None-Match"] = "*"
        try:
            resp = self._session.put(self._url(path), json=body, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            raise Conflict(f"写入 {path} 响应中断：{type(exc).__name__}") from exc
        if resp.status_code in (409, 412, 422):
            raise Conflict(f"写入 {path} 冲突：HTTP {resp.status_code}")
        if resp.status_code not in (200, 201):
            hint = "（分支不存在，或 token 没有 contents: write）" if resp.status_code == 404 else ""
            raise StateWriteError(f"写入 {path} 失败：HTTP {resp.status_code}{hint}")
        return resp.json()["content"]["sha"]

    def delete(self, path: str, message: str, sha: str | None = None) -> None:
        if sha is None:
            _, sha = self.get(path)
        if sha is None:
            return
        resp = self._session.request(
            "DELETE",
            self._url(path),
            json={"message": scrub(message) or "chore(state): delete", "sha": sha, "branch": self.branch},
            timeout=self._timeout,
        )
        if resp.status_code in (409, 422):
            raise Conflict(f"删除 {path} 冲突：HTTP {resp.status_code}")
        if resp.status_code not in (200, 204):
            raise StateWriteError(f"删除 {path} 失败：HTTP {resp.status_code}")


# ---------------------------------------------------------------- 存储


class StateStore:
    """把后端 + 密钥包成"读明文 / 写密文"的高层接口。"""

    def __init__(
        self,
        backend: ContentsBackend,
        state_key: bytes,
        owner_repo: str,
        key_version: int = 1,
        old_keys: Mapping[int, bytes] | None = None,
        retries: int = 3,
    ) -> None:
        self.backend = backend
        self.owner_repo = owner_repo
        self.key_version = int(key_version)
        self._keys: dict[int, bytes] = {self.key_version: state_key}
        for version, key in (old_keys or {}).items():
            self._keys[int(version)] = key
        self.retries = max(1, int(retries))
        if len(state_key) != crypto.STATE_KEY_LEN:
            raise StateError("状态密钥必须是 32 字节")
        self._branch_ready = False

    # -- 基础设施

    def ensure_ready(self) -> None:
        """首次写入前确认状态分支存在（GitHub 上首次写文件会因为分支不存在返回 404）。"""
        if self._branch_ready:
            return
        ensure = getattr(self.backend, "ensure_branch", None)
        if callable(ensure):
            try:
                ensure()
            except StateError as exc:  # 建不出来就让写入自己报错，别掩盖真实原因
                log.warning("状态分支自动创建失败：%s", exc)
        self._branch_ready = True

    # -- 基础

    def _ctx(self, key_version: int) -> bytes:
        return crypto.context(self.owner_repo, key_version)

    def _decrypt(self, envelope: Mapping[str, Any], path: str) -> dict:
        version = int(envelope.get("kv", self.key_version))
        key = self._keys.get(version)
        if key is None:
            raise StateReadError(f"{path} 使用了未知的 key_version={version}")
        try:
            return crypto.decrypt_state(envelope, key, self._ctx(version))
        except crypto.CryptoFailure as exc:
            raise StateReadError(f"{path} 解密失败：{exc}") from exc

    def read_plain(self, path: str) -> tuple[dict | None, str | None]:
        raw, sha = self.backend.get(path)
        if raw is None:
            return None, sha
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateReadError(f"{path} 不是合法 JSON") from exc
        return value, sha

    def read_encrypted(self, path: str) -> dict | None:
        raw, _ = self.backend.get(path)
        if raw is None:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateReadError(f"{path} 密文容器不是合法 JSON") from exc
        return self._decrypt(envelope, path)

    def write_plain(self, path: str, payload: Mapping[str, Any], message: str) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        self._cas_write(path, data, message, create_only=False)

    def write_encrypted(self, path: str, payload: Mapping[str, Any], message: str, create_only: bool = False) -> None:
        envelope = crypto.encrypt_state(payload, self._keys[self.key_version], self._ctx(self.key_version), self.key_version)
        data = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._cas_write(path, data, message, create_only=create_only, sealed=True)

    def mutate_encrypted(
        self,
        path: str,
        update: Callable[[dict | None], Mapping[str, Any] | None],
        message: str,
    ) -> dict | None:
        """在与 sha 对应的最新明文上变更，冲突后重新计算变更。"""
        self.ensure_ready()
        for _attempt in range(self.retries):
            raw, sha = self.backend.get(path)
            try:
                current = self._decrypt(json.loads(raw.decode("utf-8")), path) if raw is not None else None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StateReadError(f"{path} 密文容器不是合法 JSON") from exc
            updated = update(deepcopy(current))
            if updated is None or updated == current:
                return updated
            envelope = crypto.encrypt_state(updated, self._keys[self.key_version], self._ctx(self.key_version), self.key_version)
            data = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
            try:
                self.backend.put(path, data, message, sha=sha, create_only=sha is None)
                return dict(updated)
            except Conflict:
                saved, _ = self.backend.get(path)
                if saved == data:
                    return dict(updated)
        raise StateWriteError(f"{path} 变更失败（{self.retries} 次并发重试）")

    def _cas_write(self, path: str, data: bytes, message: str, create_only: bool, sealed: bool = False) -> None:
        self.ensure_ready()
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            _, sha = self.backend.get(path)
            if create_only and sha is not None:
                raise Conflict(f"{path} 已存在（create_only）")
            try:
                self.backend.put(path, data, message, sha=sha, create_only=create_only and sha is None)
                return
            except Conflict as exc:
                last_error = exc
                current, _ = self.backend.get(path)
                if current == data:
                    log.debug("%s 冲突但内容一致，视为写成功（第 %s 次）", path, attempt)
                    return
                if sealed and current is not None:
                    log.warning("%s 第 %s 次 CAS 冲突，重试", path, attempt)
                continue
        raise StateWriteError(f"{path} 写入失败（{self.retries} 次重试后仍冲突）：{last_error}")

    # -- control.json

    def load_control(self) -> dict | None:
        payload, _ = self.read_plain(CONTROL_PATH)
        return payload

    def save_control(self, public_key: bytes, key_version: int | None = None) -> dict:
        payload = {
            "public_key": crypto.b64e(public_key),
            "key_version": int(key_version or self.key_version),
            "updated_at": utcnow_iso(),
        }
        self.write_plain(CONTROL_PATH, payload, "chore(state): control key")
        return payload

    def public_key(self) -> tuple[bytes, int] | None:
        payload = self.load_control()
        if not payload:
            return None
        try:
            return crypto.b64d(payload["public_key"]), int(payload.get("key_version", 1))
        except (KeyError, crypto.CryptoFailure) as exc:
            raise StateReadError(f"control.json 内容异常：{exc}") from exc

    # -- settings

    def load_settings(self) -> dict:
        return self.read_encrypted(SETTINGS_PATH) or {}

    def save_settings(self, settings: Mapping[str, Any]) -> None:
        self.write_encrypted(SETTINGS_PATH, settings, "chore(state): settings")

    def mutate_settings(self, update: Callable[[dict], dict]) -> dict:
        return self.mutate_encrypted(SETTINGS_PATH, lambda current: update(current or {}), "chore(state): settings") or {}

    # -- accounts

    @staticmethod
    def account_path(account_id: str) -> str:
        return f"{ACCOUNTS_DIR}/{account_id}.enc.json"

    def list_account_ids(self) -> list[str]:
        suffix = ".enc.json"
        return [name[: -len(suffix)] for name in self.backend.list(ACCOUNTS_DIR) if name.endswith(suffix)]

    def load_account(self, account_id: str) -> dict | None:
        return self.read_encrypted(self.account_path(account_id))

    def mutate_account(self, account_id: str, update: Callable[[dict | None], Mapping | None], message: str) -> dict | None:
        return self.mutate_encrypted(self.account_path(account_id), update, message)

    def load_accounts(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for account_id in self.list_account_ids():
            payload = self.load_account(account_id)
            if payload is None:
                continue
            payload.setdefault("id", account_id)
            result[account_id] = payload
        return result

    def save_account(self, account: Mapping[str, Any] | Any, message: str | None = None) -> None:
        if not isinstance(account, Mapping):  # 也接受 Account 对象，调用方少一次 .to_dict()
            to_dict = getattr(account, "to_dict", None)
            if to_dict is None:
                raise StateError("save_account 需要 Mapping 或带 to_dict() 的账户对象")
            account = to_dict()
        account_id = account.get("id")
        if not account_id:
            raise StateError("账户缺少 id")
        text = message or f"chore(state): account {account_id}"
        self.write_encrypted(self.account_path(str(account_id)), account, text)

    def save_account_runtime(self, account: Mapping[str, Any], baseline: Mapping | None, message: str) -> dict | None:
        """合并运行时字段，保留同期控制指令的修改与重新绑定的凭据。"""
        previous = baseline or {}
        fields = ("cookies", "renewed_at", "last_ok_at", "status", "note", "progress", "warm_failures", "warm_notified")

        def update(current: dict | None) -> dict | None:
            if current is None:
                return dict(account) if baseline is None else None
            credential_changed = any(current.get(name) != previous.get(name) and current.get(name) != account.get(name) for name in ("cookies", "headers", "read_payload", "credential_revision"))
            for name in fields:
                if name not in account or account.get(name) == previous.get(name):
                    continue
                if credential_changed and name in {"cookies", "renewed_at", "last_ok_at", "status", "note", "warm_failures", "warm_notified"}:
                    continue
                value = account[name]
                old = previous.get(name)
                if isinstance(value, dict):
                    existing = dict(current.get(name) or {})
                    old = old if isinstance(old, dict) else {}
                    if name == "progress" and value.get("run_id") != old.get("run_id"):
                        if existing.get("run_id") == old.get("run_id"):
                            current[name] = dict(value)
                        continue
                    for key, item in value.items():
                        if item != old.get(key) and existing.get(key) == old.get(key):
                            existing[key] = item
                    current[name] = existing
                elif current.get(name) == old:
                    current[name] = value
            return current

        return self.mutate_account(str(account["id"]), update, message)

    def delete_account(self, account_id: str, message: str | None = None) -> None:
        path = self.account_path(account_id)
        self.ensure_ready()
        try:
            self.backend.delete(path, message or f"chore(state): delete account {account_id}")
        except Conflict as exc:
            raise StateWriteError(f"删除账户 {account_id} 失败：{exc}") from exc

    # -- runs

    def _runs_path(self, month: str) -> str:
        return f"{RUNS_DIR}/{month}.enc.json"

    def load_runs(self, month: str) -> list[dict]:
        payload = self.read_encrypted(self._runs_path(month))
        if payload is None:
            return []
        records = payload.get("runs")
        if not isinstance(records, list):
            raise StateReadError(f"runs/{month} 结构异常")
        return records

    def append_run(self, record: Mapping[str, Any], keep: int = RUNS_KEEP) -> None:
        started_at = str(record.get("started_at") or utcnow_iso())
        month = started_at[:7]
        saved_record = dict(record)
        saved_record.setdefault("started_at", started_at)
        identity = saved_record.get("run_id") or hashlib.sha256(
            json.dumps(saved_record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        saved_record["run_id"] = identity

        def append(current: dict | None) -> dict:
            current = current or {"month": month, "runs": []}
            records = current.get("runs")
            if not isinstance(records, list):
                raise StateReadError(f"runs/{month} 结构异常")
            if any(item.get("run_id") == identity for item in records):
                return current
            records.insert(0, saved_record)
            records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
            del records[max(keep, 1) :]
            return {"month": month, "runs": records}

        self.mutate_encrypted(self._runs_path(month), append, "chore(state): runs")

    def recent_runs(self, limit: int = 20, months: Iterable[str] | None = None) -> list[dict]:
        if months is None:
            months = [name[:7] for name in self.backend.list(RUNS_DIR) if name.endswith(".enc.json")]
        collected: list[dict] = []
        for month in sorted(set(months), reverse=True):
            collected.extend(self.load_runs(month))
        collected.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return collected[:limit]

    # -- sessions（防重放）

    @staticmethod
    def session_path(session_id: str) -> str:
        return f"{SESSIONS_DIR}/{session_id}.enc.json"

    def claim_session(self, session_id: str, payload: Mapping[str, Any]) -> None:
        """以 create_only 创建会话文件；已存在即视为重放。"""
        path = self.session_path(session_id)
        envelope = crypto.encrypt_state(payload, self._keys[self.key_version], self._ctx(self.key_version), self.key_version)
        data = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            self._cas_write(path, data, f"chore(state): session {session_id}", create_only=True, sealed=True)
        except Conflict as exc:
            raise StateWriteError(f"指令已被执行过（session={session_id}）") from exc

    # -- responses（回执）

    def put_raw(self, path: str, data: bytes, message: str) -> None:
        """写任意字节（如二维码 PNG）；只用于 `responses/` 下的短命文件。"""
        self.ensure_ready()
        _, sha = self.backend.get(path)
        self.backend.put(path, data, message, sha=sha, create_only=False)

    @staticmethod
    def response_path(session_id: str) -> str:
        return f"{RESPONSES_DIR}/{session_id}.json"

    def write_response(self, session_id: str, envelope: Mapping[str, Any]) -> None:
        data = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._cas_write(self.response_path(session_id), data, f"chore(state): response {session_id}", create_only=False)

    def read_response(self, session_id: str) -> dict | None:
        payload, _ = self.read_plain(self.response_path(session_id))
        return payload
