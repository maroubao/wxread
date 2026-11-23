"""worker：GitHub Actions 里的唯一入口。

子命令：
    read      读今天的量（P2）
    warm      只续期（P4 补齐）
    migrate   把旧 secret 迁成账户文件（P1/P2 共用）

环境变量（全部在此读取，其它模块不得碰 os.environ）：
    GITHUB_REPOSITORY        owner/repo（Actions 自带）
    GITHUB_TOKEN             状态分支写入令牌
    WXREAD_STATE_KEY         base64 的 32 字节状态密钥
    WXREAD_STATE_BRANCH      默认 wxread-state
    WXREAD_MODE              legacy|v4（默认 legacy：只迁移不跑新流程）
    WXREAD_CURL_BASH         旧配置，迁移用
    WXREAD_ACCOUNTS_JSON     旧配置（多账户数组），迁移用
    WXREAD_TARGET_COUNT      覆盖账户默认目标数
    WXREAD_PACE              两次阅读间隔秒数（测试用，默认 30）
    WXREAD_DEADLINE          单账户上限秒数（默认 1200）
    WXREAD_KEYS              轮换期旧密钥，形如 "1:<b64>,2:<b64>"
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
from typing import Mapping

from .crypto import CryptoFailure, b64d
from .accounts import Account, AccountError, MissingFieldsError, clamp_target_count, from_legacy
from .jobs import JobEvent, JobRunner
from .logsafe import scrub
from .reader import DEFAULT_DEADLINE, DEFAULT_PACE
from .state import DEFAULT_BRANCH, GitHubContents, StateError, StateStore, utcnow_iso

log = logging.getLogger("wxread.worker")

EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_CONFIG = 2
EXIT_STATE = 4


class ConfigError(RuntimeError):
    """环境变量缺失或格式错误。"""


def _env(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is None or value == "":
        return default
    return value


def decode_state_key(raw: str) -> bytes:
    text = raw.strip()
    if len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text):
        key = bytes.fromhex(text)  # 纯 hex 一定是 hex（64 个 hex 字符也是合法 base64，不能先试 base64）
    else:
        try:
            key = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            try:
                key = bytes.fromhex(text)
            except ValueError as exc:
                raise ConfigError("WXREAD_STATE_KEY 既不是 base64 也不是 hex") from exc
    if len(key) != 32:
        raise ConfigError(f"WXREAD_STATE_KEY 必须是 32 字节，实际 {len(key)}")
    return key


def parse_old_keys(raw: str | None) -> dict[int, bytes]:
    result: dict[int, bytes] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        version, _, value = chunk.partition(":")
        try:
            result[int(version)] = decode_state_key(value)
        except (ValueError, ConfigError) as exc:
            raise ConfigError(f"WXREAD_KEYS 解析失败：{chunk}") from exc
    return result


def build_backend(env: Mapping[str, str]) -> GitHubContents:
    repo = _env(env, "GITHUB_REPOSITORY")
    token = _env(env, "GITHUB_TOKEN")
    if not repo or not token:
        raise ConfigError("缺少 GITHUB_REPOSITORY 或 GITHUB_TOKEN")
    branch = _env(env, "WXREAD_STATE_BRANCH", DEFAULT_BRANCH) or DEFAULT_BRANCH
    return GitHubContents(repo, token, branch=branch)


def build_store(env: Mapping[str, str], backend=None) -> StateStore:
    repo = _env(env, "GITHUB_REPOSITORY")
    if not repo:
        raise ConfigError("缺少 GITHUB_REPOSITORY")
    raw_key = _env(env, "WXREAD_STATE_KEY")
    if not raw_key:
        raise ConfigError("缺少 WXREAD_STATE_KEY")
    store = StateStore(
        backend or build_backend(env),
        decode_state_key(raw_key),
        repo,
        key_version=1,
        old_keys=parse_old_keys(_env(env, "WXREAD_KEYS")),
    )
    control = store.load_control()
    if control:
        try:
            store.key_version = int(control.get("key_version", 1))
        except (TypeError, ValueError):
            raise ConfigError("control.json 的 key_version 不是整数")
    return store


def load_accounts(store: StateStore, env: Mapping[str, str]) -> list[Account]:
    """状态分支里已有的账户优先；为空时才读旧 secret（并迁移）。"""
    accounts: list[Account] = []
    for account_id, payload in store.load_accounts().items():
        try:
            accounts.append(Account.from_dict(payload, account_id))
        except AccountError as exc:
            log.error("账户 %s 数据损坏：%s", account_id, scrub(str(exc)))
    if accounts:
        return sorted(accounts, key=lambda item: item.id)

    legacy_curl = _env(env, "WXREAD_CURL_BASH")
    legacy_json = _env(env, "WXREAD_ACCOUNTS_JSON")
    if not legacy_curl and not legacy_json:
        return []
    try:
        migrated = from_legacy(legacy_curl, legacy_json)
    except (AccountError, MissingFieldsError) as exc:
        raise ConfigError(f"旧配置无法迁移：{scrub(str(exc))}") from exc
    for account in migrated:
        store.save_account(account.to_dict(), f"chore(state): migrate {account.id}")
        log.info("已迁移账户 %s（%s）", account.id, account.label)
    return sorted(migrated, key=lambda item: item.id)


def apply_env_overrides(accounts: list[Account], env: Mapping[str, str]) -> None:
    target = _env(env, "WXREAD_TARGET_COUNT")
    if target:
        try:
            value = clamp_target_count(target)
        except AccountError as exc:
            raise ConfigError(str(exc)) from exc
        for account in accounts:
            account.target_count = value


def _print_event(event: JobEvent) -> None:
    if event.kind == "progress":
        done = event.data.get("read_count")
        total = event.data.get("target_count")
        log.info("[%s] %s/%s", event.account_id, done, total)
        if done is not None and total is not None:
            print(f"::notice::read {done}/{total}", flush=True)
        return
    log.info("事件 %s %s %s", event.kind, event.account_id, scrub(str(event.data)))


def cmd_migrate(store: StateStore, env: Mapping[str, str]) -> int:
    accounts = load_accounts(store, env)
    if not accounts:
        log.warning("没有可迁移的账户")
        return EXIT_OK
    for account in accounts:
        store.save_account(account.to_dict(), f"chore(state): migrate {account.id}")
    print(f"::notice::migrated {len(accounts)} account(s)", flush=True)
    return EXIT_OK


def schedule_gate(store: StateStore, mode: str) -> str:
    """`settings.schedule` 的运行时开关：cron 写死在 workflow 里，只能在这里拦。

    返回空串表示放行，否则返回拦截原因（调用方打 notice 后正常退出）。
    """
    try:
        settings = store.load_settings()
    except StateError as exc:  # 设置读失败不该让定时任务全挂
        log.warning("读取 settings 失败，按放行处理：%s", scrub(str(exc)))
        return ""
    schedule = (settings or {}).get("schedule") or {}
    if schedule.get("enabled") is False:
        return "自动运行已在控制台关闭"
    if mode == "warm" and int(schedule.get("warm_hours", 6) or 0) == 0:
        return "保活间隔设为 0（已关闭）"
    return ""


def cmd_read(store: StateStore, env: Mapping[str, str], transport_factory=None, sleep=None) -> int:
    blocked = schedule_gate(store, "read")
    if blocked:
        log.info("跳过本次阅读：%s", blocked)
        print(f"::notice::read skipped: {blocked}", flush=True)
        return EXIT_OK
    accounts = load_accounts(store, env)
    apply_env_overrides(accounts, env)
    if not accounts:
        log.error("没有任何账户：先扫码绑定或导入 curl")
        return EXIT_CONFIG
    pace = float(_env(env, "WXREAD_PACE", str(DEFAULT_PACE)) or DEFAULT_PACE)
    deadline = float(_env(env, "WXREAD_DEADLINE", str(DEFAULT_DEADLINE)) or DEFAULT_DEADLINE)
    runner = JobRunner(
        store,
        transport_factory=transport_factory,
        pace=pace,
        deadline=deadline,
        on_event=_print_event,
        **({"sleep": sleep} if sleep is not None else {}),
    )
    started = utcnow_iso()
    records = runner.run(accounts)
    total = sum(int(record.get("read_count", 0)) for record in records)
    failed = [record for record in records if int(record.get("exit_code", 0)) not in (0, 2)]
    state_errors = [record for record in records if int(record.get("exit_code", 0)) == EXIT_STATE]
    log.info("本次共读 %s 次，账户 %s 个，失败 %s 个", total, len(records), len(failed))
    print(f"::notice::session done reads={total} accounts={len(records)} failed={len(failed)} since={started}", flush=True)
    if state_errors:
        return EXIT_STATE
    if records and len(failed) == len(records):
        return EXIT_ALL_FAILED
    return EXIT_OK


def cmd_warm(store: StateStore, env: Mapping[str, str], transport_factory=None, now_iso=None) -> int:
    """只续期：每 6 小时跑一次，保持会话不过冷。"""
    from . import warm as warm_mod
    from .reader import RequestsTransport

    blocked = schedule_gate(store, "warm")
    if blocked:
        log.info("跳过本次保活：%s", blocked)
        print(f"::notice::warm skipped: {blocked}", flush=True)
        return EXIT_OK
    accounts = load_accounts(store, env)
    if not accounts:
        log.error("没有任何账户，先扫码绑定或导入 curl")
        return EXIT_CONFIG
    summary = warm_mod.warm_all(
        store,
        transport_factory=transport_factory or RequestsTransport,
        now_iso=now_iso or utcnow_iso,
        accounts=accounts,
    )
    text = warm_mod.render_summary(summary)
    log.info(text.replace("\n", " / "))
    print(f"::notice::warm done ok={summary.warmed} failed={summary.failed} skipped={summary.skipped}", flush=True)
    for result in summary.results:
        if result.reason == warm_mod.WARM_REASON_FAILED:
            print(f"::error title=wxread 保活失败::{result.label or result.account_id} {result.note}", flush=True)
    return EXIT_OK if summary.failed == 0 else EXIT_ALL_FAILED


def cmd_control(store: StateStore, env: Mapping[str, str], *, qr_page_factory=None, notify_send=None, sleep=None) -> int:
    """`control.yml` 入口：init 建密钥对，或用密文指令执行管理动作。"""
    from . import control as control_mod

    action = (_env(env, "WXREAD_CONTROL") or "instruction").strip().lower()
    qr_seconds = float(_env(env, "WXREAD_QR_SECONDS", str(control_mod.qr.DEFAULT_TIMEOUT)))

    qr_dir = _env(env, "WXREAD_QR_DIR")

    def on_qr(shot) -> None:
        path = f"responses/qr-{shot.fingerprint}.png"
        try:
            store.put_raw(path, shot.png, f"chore(state): qr {shot.fingerprint}")
            raw = f"{store.backend.repo}/blob/{store.backend.branch}/{path}"
            log.info("二维码已更新：%s", raw)
            print(f"::notice::qr updated {raw}", flush=True)
        except StateError as exc:  # 写二维码失败不致命，页面还能靠回执轮询
            log.warning("二维码落盘失败：%s", scrub(str(exc)))
        if qr_dir:  # 同时落一份到 runner 本地，供 artifact 兜底（私有仓库的 blob 链接不好点）
            try:
                os.makedirs(qr_dir, exist_ok=True)
                with open(os.path.join(qr_dir, f"qr-{shot.fingerprint}.png"), "wb") as fh:
                    fh.write(shot.png)
            except OSError as exc:
                log.warning("二维码本地落盘失败：%s", scrub(str(exc)))

    ctx = control_mod.ControlContext(
        store=store,
        qr_page_factory=qr_page_factory,
        qr_timeout=qr_seconds,
        qr_out=on_qr if _env(env, "WXREAD_QR_UPLOAD", "1") != "0" else None,
        notify_send=notify_send,
    )
    if action == "init":
        if _env(env, "WXREAD_CONTROL_SK"):
            log.warning("init 会生成新密钥对；已有 WXREAD_CONTROL_SK 时请直接用 instruction")
        result = control_mod.handle_init(ctx, {})
        print(f"::notice::control public_key={result['public_key']} key_version={result['key_version']}", flush=True)
        if not result.get("reused"):
            print(f"::add-mask::{result['server_secret_key']}", flush=True)
            print(f"private_key={result['server_secret_key']}")
            log.warning("把上面的 private_key 存进 Secret WXREAD_CONTROL_SK（只显示这一次）")
        return EXIT_OK

    instruction_text = _env(env, "WXREAD_INSTRUCTION")
    secret_text = _env(env, "WXREAD_CONTROL_SK")
    if not instruction_text or not secret_text:
        raise ConfigError("instruction 模式需要 WXREAD_INSTRUCTION 与 WXREAD_CONTROL_SK")
    try:
        envelope = json.loads(instruction_text)
        secret_key = b64d(secret_text)
        instruction = control_mod.decode(envelope, secret_key)
    except (ValueError, CryptoFailure) as exc:
        raise ConfigError(f"指令无法解封：{scrub(str(exc))}") from exc
    receipt = control_mod.execute(ctx, instruction, skew=int(_env(env, "WXREAD_SKEW", str(control_mod.DEFAULT_SKEW))))
    log.info("回执：%s", control_mod.summary(receipt))
    print(f"::notice::control {control_mod.summary(receipt)}", flush=True)
    return EXIT_OK if receipt.get("ok") else EXIT_ALL_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxread.worker")
    parser.add_argument("command", choices=("read", "warm", "migrate", "control"))
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = env or os.environ
    logging.basicConfig(
        level=logging.DEBUG if args.verbose or _env(env, "WXREAD_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        store = build_store(env)
    except (ConfigError, StateError) as exc:
        log.error("初始化失败：%s", scrub(str(exc)))
        return EXIT_CONFIG
    try:
        if args.command == "migrate":
            return cmd_migrate(store, env)
        if args.command == "read":
            return cmd_read(store, env)
        if args.command == "control":
            return cmd_control(store, env)
        return cmd_warm(store, env)
    except (ConfigError, StateError) as exc:
        log.error("执行失败：%s", scrub(str(exc)))
        return EXIT_CONFIG


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
