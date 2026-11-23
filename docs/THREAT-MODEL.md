# 威胁模型 — wxread v4

> 对应 `docs/PLAN.md` §4（L0–L3 与四层防护）。本文件是 v4 的**唯一**安全决策记录：任何实现偏离必须回到这里改，不允许在代码注释里"顺手放宽"。

- 日期：2026-11-23
- 范围：无服务器架构下的微信读书自动阅读（GitHub Actions + Pages + 仓库状态分支）

---

## 1. 资产

| # | 资产 | 泄露后果 | 存放位置 |
|---|---|---|---|
| A1 | 微信读书 `cookies`（`wr_vid`/`wr_skey`/`pac_uid`） | 账号被完全接管；可读、可改书架 | `accounts/<id>.enc.json`（密文）；运行时仅内存 |
| A2 | `read_payload`（`appId`/`ps`/`pc`/`ci`/`co`/`sm`/`pr`） | 与 A1 组合可伪造阅读请求 | 同上（密文） |
| A3 | 通知凭据（SMTP 密码、PushPlus token、飞书 webhook、Telegram bot token） | 向用户推送钓鱼内容；token 滥用 | `settings.enc.json`（密文）+ Actions Secrets（仅入口） |
| A4 | `WXREAD_STATE_KEY`（32B 对称钥） | 解开全部 A1–A3 | Actions Secret；页面内仅内存 |
| A5 | 仓库公钥 / 页面私钥（X25519） | 伪造指令或读取回执 | `control.json`（公钥明文）/ 页面内存（私钥） |
| A6 | `GITHUB_TOKEN`（workflow 默认） | 改 `wxread-state` 分支、dispatch workflow | Actions 运行时注入 |
| A7 | 用户自建 PAT（可写 Contents/Actions） | 同上，且可删状态 | 页面 localStorage（可选，24h 过期） |
| A8 | 运行日志（含失败原因、脱敏后的 id） | 泄露"谁在读什么书" | Actions 日志（平台留 90 天） |
| A9 | 二维码截图（登录用） | 他人扫码后账户被绑定到别人微信 | 运行期临时文件，会话结束即删 |

**不在资产清单里**：书架内容正文、阅读时长统计（腾讯侧数据，本方案不落地）。

---

## 2. 信任边界（5 层）

```
L1 浏览器页面（Pages，公开静态）
   │  ① 只读状态分支的密文；私钥、明文状态仅在内存
   ▼
L2 GitHub API（Contents / Actions dispatch）
   │  ② token 决定一切：能写状态分支 = 能改所有账户
   ▼
L3 Actions 运行容器
   │  ③ Secrets 在此解密，出容器只剩密文与脱敏日志
   ▼
L4 微信读书服务端（weread.qq.com）
   │  ④ 返回 200 + JSON，不代表业务成功（要看 succ/errCode）
   ▼
L5 第三方通知通道（SMTP / PushPlus / 飞书 / Telegram）
      ⑤ 内容一经发出不可撤回，必须发前脱敏
```

| 边界 | 信任假设 | 打破假设的后果 |
|---|---|---|
| ① 页面 ↔ 状态分支 | 页面**不**信任分支内容（全密文 + 上下文校验） | 篡改密文 → 解密失败 → 页面报"状态损坏"，不降级执行 |
| ② API ↔ token | 有写权限者即管理员 | 可删账户、改调度（残留风险，见 §8） |
| ③ 容器 ↔ Secrets | 只有 `control.yml`/`read.yml`/`warm.yml` 的 job 能读到 | fork PR 不触发带 Secrets 的 job（T-07） |
| ④ 服务端 | 任何字段都可能是错误值；不因 200 就当成功 | 误判成功 → 假进度（T-13） |
| ⑤ 通知 | 通道方会留存全文 | 含 cookie/token 的告警等于把凭据发给第三方（T-09） |

---

## 3. 对手

| # | 对手 | 能力 | 动机 |
|---|---|---|---|
| O1 | 路过者 | 看得到 Pages 源码、仓库公开历史 | 好奇、找 cookie |
| O2 | 拿到 PAT 的攻击者 | 读写 Contents、dispatch | 批量接管账户、挖矿 |
| O3 | 恶意 fork / PR 作者 | 提 PR、改 workflow | 偷 Secrets |
| O4 | 通知通道运营方 | 看到全部推送正文 | 数据变现 |
| O5 | 同机恶意扩展/脚本 | 读 localStorage、读页面内存 | 偷 PAT、偷明文状态 |
| O6 | 网络中间人 | 改 HTTP 响应 | 伪造成功、注入脚本 |
| O7 | 平台自身（GitHub/腾讯） | 备份、日志、审计 | 合规调取 |
| O8 | 用户自己（误操作） | 贴截图、贴 curl、复用密码 | —— |

---

## 4. 威胁与缓解（T-01 … T-14）

| # | 威胁 | 缓解 | 验证 |
|---|---|---|---|
| T-01 | cookie/token 进入日志或 commit | 出容器前 `logsafe.scrub()`；`logging` 里禁止打印响应体与 headers；CI 扫明文密钥 | `test_redact.py`；`tests.yml` 明文扫描 |
| T-02 | 指令重放（同一份密文被重复 dispatch） | 指令含 `session_id`/`issued_at`(60s 窗口)/`nonce`；`sessions/<id>.enc.json` 用 `If-None-Match: *` 创建成功即"已消费" | `test_control_replay.py` |
| T-03 | 回执被旁路读取 | 回执用**页面临时公钥**加密，写入 `responses/<session>.enc.json`，10 分钟清理 | `test_crypto.py` 往返 + 错钥失败 |
| T-04 | 状态文件被篡改/降级 | AEAD（见 §5）；上下文字节比对；CAS 写入；有写权限者可删文件（残留风险） | `test_state_roundtrip.py` 篡改 1 字节 |
| T-05 | 页面连到伪造的 `control.json` | 页面显示公钥指纹，首次使用需人工核对；指纹变化即告警 | 手工（G3 清单） |
| T-06 | 用户自建 PAT 权限过大 | 文档限定最小 scope（Contents: RW、Actions: RW）；不做 fine-grained 之外的引导；24h 过期 | `docs/RUNBOOK.md` |
| T-07 | `pull_request_target` + Secrets | 所有带 Secrets 的 workflow 只允许 `workflow_dispatch`/`schedule`；PR 触发的一律无 Secrets | `tests.yml` 扫描 workflow 触发器 |
| T-08 | 明文状态进入页面或 localStorage | 状态永不明文落盘；页面只解自己需要的字段；断开即清内存 | 代码评审 + node 测试扫 `localStorage` 写入 |
| T-09 | 通知正文含凭据 | 通知模板只允许：账户 label、状态、读书数、失败原因码；`scrub()` 兜底 | `test_notify_template.py` |
| T-10 | 二维码被他人扫码 | 二维码只在用户本人触发的 run 中出现，截图随会话删除；页面提示"仅本人扫描" | 手工（G3 清单） |
| T-11 | 依赖被投毒 | 依赖锁定精确版本，禁 `>=`；CI 校验 | `tests.yml` |
| T-12 | 二维码/临时文件残留 | 会话结束删 `qr-*.png`；不写 `responses/` 之外的位置 | 代码评审 |
| T-13 | 服务端假成功 | 只看 `succ==1` **且** 拿到新 `synckey`；错误码分层（`-2012/-2013` 视为会话失效） | `test_reader.py` |
| T-14 | XSS（渲染外部数据） | 前端零构建、只用 `textContent`/`createElement`；禁 `innerHTML` 拼外部数据 | node 测试扫描 `innerHTML` |

---

## 5. 密码学参数（写死）

| 用途 | 原语 | 参数 | 说明 |
|---|---|---|---|
| 指令加密 | X25519 + XSalsa20-Poly1305（`crypto_box`） | 每条指令新建临时密钥对；24B 随机 nonce 随封套明文携带（`epk`/`n`） | 语义等同 sealed box，但 nonce 显式，浏览器（tweetnacl）可复算 |
| 回执加密 | 同上 | 接收方 = 浏览器临时公钥 | 回执只有发起页面能解 |
| 状态加密 | XSalsa20-Poly1305（`crypto_secretbox`） | 密钥 = `WXREAD_STATE_KEY`（32B 随机）；nonce 24B 随机 | 见 §5.1 |
| 上下文绑定 | 明文首行固定 `wxread-state\|<owner>/<repo>\|v<key_version>` | 解密后逐字节比对，不符即失败 | 前缀在 MAC 覆盖范围内，等价 AEAD 的 AAD |
| 随机数 | `os.urandom` | nonce 24B、session_id 16B、状态密钥 32B | 禁止 `random` 模块做密码学用途 |
| 密钥版本 | `key_version` 整数 | 从 1 起；新旧并存一轮 | 轮换期两把密钥可解，写入只用新钥 |

### 5.1 与 PLAN §3.1 的偏离（P1 实现后定案，**需用户确认**）

PLAN 写"SecretBox 密文 + AAD 绑 `repo|version`"；G1 原定升级为 XChaCha20-Poly1305-IETF。P1 落地时改为 **`crypto_secretbox` + 明文首行上下文前缀**，原因：

1. 浏览器端无法引 libsodium wasm（0.7.x bundle 路径 404、0.8.4 无 bundle），只能 vendor `tweetnacl@1.0.3`（`web/assets/vendor/nacl-fast.min.js` 已就位）。tweetnacl 提供 `secretbox`/`box`，**不提供** XChaCha20-Poly1305。
2. 两端必须共用同一套原语；`secretbox` 在 Python（PyNaCl）与 JS（tweetnacl）都可用。

安全等价性：前缀 `wxread-state|<repo>|v<kv>` 位于明文头部、被 Poly1305 MAC 覆盖，改仓库名或版本号必然解密失败——与 AEAD AAD 的绑定强度一致。`tests/test_crypto.py::test_state_context_is_authenticated` 与 `tests/test_state_roundtrip.py::test_context_binds_repo` 锁定该性质。

指令封套同样落地为"临时密钥 + 显式 24B nonce 的 `crypto_box`"，而非 `crypto_box_seal` 的 blake2b 派生 nonce——后者在 tweetnacl 侧无对应实现。

---

## 6. 授权与指令模型

1. **授权来源只有一个**：GitHub token 的能力。能 dispatch `control.yml` 即视为对"本仓库被管理账户"有操作权。
2. `control.yml` 只接受 `workflow_dispatch` 输入中的密文指令，**不接受明文参数**（workflow input 会进日志与 API 响应）。
3. 指令类型白名单：`init`、`qr`、`import`、`update`、`delete`、`schedule`、`notifications`、`test-mail`、`test-push`。未知 `action` 直接拒绝并记 `runs`。
4. 无 nonce 表：以 `sessions/<session_id>.enc.json` 的**创建成功**作为"已消费"证据（GitHub Contents API 的 `If-None-Match: *` 语义），避免额外状态。
5. `delete` 与 `schedule` 属危险动作，页面必须弹确认框并显示对象名（DESIGN §5 第 8/12 组件）。

---

## 7. 数据最小化与留存

| 数据 | 留存 | 理由 |
|---|---|---|
| 账户密文 | 直到用户删除 | 功能必需 |
| `runs` | 200 条（月度分片内裁剪） | 够画近 7 日与排障 |
| `responses/<session>.enc.json` | 10 分钟 | 回执窗口 |
| 二维码截图 | 会话结束即删 | T-12 |
| 日志 | Actions 默认 90 天 | 平台行为，无法缩短到更短需企业版；因此必须靠脱敏 |
| PAT | 默认内存；勾选存 localStorage，24 小时过期，断开即清 | DESIGN 与 PLAN §3.2 一致 |

---

## 8. 接受与不接受

**接受的残留风险**：平台侧留存与备份泄露（X-7）；用户自建 PAT 权限过大（T-06）；用户不核对公钥指纹（T-05）；有写权限者删文件（T-04 末条）；默认弱配置被 fork 复制（X-2）。

**不接受（必须阻断）**：明文 cookie 进日志或 commit（T-01）；`pull_request_target` 携带 Secrets（T-07）；回执广播到非发起页面（T-03）；通知内容含 cookie/token；页面用 `innerHTML` 渲染外部数据（T-14）。

**复审触发条件**（任一发生即重开本文件）：新增数据字段（尤其身份类）、新增第三方服务、开放非本人账户绑定、把明文状态引入页面、`key_version` 轮换、Actions 权限模型变更。

---

## 9. 与实现的对应关系（G2 检查点）

| 本文条款 | 实现落点 | 测试 |
|---|---|---|
| T-01 脱敏 | `wxread/logsafe.py` 的 `scrub()` / `mask()` | `test_redact.py`：`wr_skey=abcdef` → `ab***` |
| T-02 重放 | `wxread/control.py` 校验 + `sessions/` 创建 | `test_control_replay.py`：同 session 二跑必失败 |
| T-03 回执 | `crypto.seal_to(ephemeral_pub)` | `test_crypto.py` 往返 + 错钥失败 |
| T-04 AEAD | `state.py` 解密失败抛 `StateReadError` | `test_state_roundtrip.py` 篡改 1 字节 |
| T-11 依赖 | `requirements.txt` 固定 | `tests.yml` 校验无 `>=` |
| T-14 XSS | `web/assets/*.js` 只用 `textContent` | node 测试扫描 `innerHTML` |
