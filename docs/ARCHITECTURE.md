# ARCHITECTURE — wxread v4 架构与数据流

> 状态：G1 待审核 · 2026-11-23 · 对应 PLAN v4 与 DESIGN v1.0（A 钢蓝 #2563EB）
> 本文只描述**结构与契约**，不含实现代码。凡本文写死的字段名、状态值、退出码，实现期不得改名。
> 视觉与组件规范见 [DESIGN.md](DESIGN.md)，安全与信任边界见 [THREAT-MODEL.md](THREAT-MODEL.md)。

---

## 0. 一句话

把"单账号 + 人工重抓 curl + 过期靠人发现"的脚本，换成"多账号 + 网页扫码绑定 + 续期结果持久化 + 每日自动跑 + 只有真被踢下线才提醒"的 GitHub 原生控制台。运行环境仍然只有 GitHub：Actions 干活，Pages 出界面，`wxread-state` 分支存密文状态。

**非目标**：多租户 SaaS、对外开放注册、手机 App、把 Cookie 存到第三方云、本地/Docker 部署。

---

## 1. 现状（v3）代码地图

仓库：`maroubao/wxread`，分支 `main`，审计基线 `7341b53`（2026-05-09）。

```
main.py          260 行   请求拼接、加密签名、阅读循环、失败分类
config.py        146 行   READ_NUM / curl 解析 / cookies / headers / data / book / chapter
push.py          190 行   PushPlus / Telegram / WxPusher / ServerChan / 飞书
log_utils.py      35 行   带进度行的 logging 封装
Dockerfile        28 行   本地 cron 容器（本期决策删除）
.github/workflows/deploy.yml  67 行  唯一工作流
```

### 1.1 运行期数据流

```
Secrets(WXREAD_CURL_BASH) ──▶ config.py:30
                                │ convert() config.py:106-138
                                ▼
                     headers / cookies / data（模块级全局，可被 config.py:141-146 覆盖）
                                │
main.py:250-260 ──▶ run() main.py:199-247
                      │
                      ├─ refresh_cookie() main.py:116-126
                      │    └─ get_wr_skey() main.py:96-106 → POST /web/login/renewal
                      │         └─ 返回 wr_skey 前 8 位，写回 cookies["wr_skey"] main.py:120
                      │
                      └─ while index <= READ_NUM:
                           build_read_payload() main.py:129-144
                             ├─ b = random.choice(book)      main.py:132
                             ├─ c = random.choice(chapter)   main.py:133
                             ├─ ts/rn/sg/s 重算            main.py:135-143
                           read_once() main.py:147-152 → POST /web/book/read
                             └─ succ? → 有 synckey 则 index+1、sleep 30
                                 └─ 无 synckey → fix_no_synckey() main.py:109-113
                             └─ 非 succ → 疑似参数失效则 ReadPayloadError，否则 refresh_cookie()
                                        main.py:235-244
```

### 1.2 状态生命周期（问题的机械原因）

| 事实 | 位置 | 后果 |
|---|---|---|
| `cookies` 是模块级 dict，续期只改内存 | `config.py:33-41`、`main.py:120` | 进程结束即丢，下一次从 Secrets 里那份旧 curl 重新起步 |
| `post_json()` 只 `return response` | `main.py:84-93` | 服务端 `Set-Cookie` 轮换被整份丢弃 |
| `refresh_cookie()` 只取 `wr_skey` | `main.py:96-106` | `wr_vid` 等其余轮换 cookie 永不更新 |
| 请求体模板写死 `appId/ps/pc/ci/co/sm/pr` | `config.py:69-85` | 抓包过久后报参数失效，只能重抓 |
| 书籍与章节各自独立随机 | `main.py:132-133` | 章节不属于该书，服务端可能不计时长 |
| 修复接口写死一本书 | `main.py:111`（`bookIds:["3300060341"]`） | 非三体账户修复无效 |
| 失败重试无计数、无 sleep、`index` 不增 | `main.py:212-220` | 抖动时可能空转烧满额度 |
| 推送分支 5 个渠道全在 | `push.py:164-190` | 与 §9 决策（只留 4 个）冲突，需在 P6 收口 |

结论沿用 PLAN §1.2：**"过期"不是 cookie 活得短，而是刷新结果被扔掉**。v4 的核心是把"刷新 → 合并 → 持久化 → 保鲜"做成闭环。

---

## 2. 目标架构（v4）

```
┌──────────────────────────── 浏览器（GitHub Pages，零构建） ────────────────────────────┐
│  web/index.html + assets/*.js|css   ← 只有 <template> + 原生 Web Components            │
│  1. 填 owner/repo + fine-grained PAT                                                   │
│  2. 生成临时 X25519 密钥对，用仓库公钥 seal 指令                                        │
│  3. 轮询回执（seal 到浏览器临时公钥），本地解密渲染                                      │
└───────────────┬───────────────────────────────────────────────┬───────────────────────┘
                │ POST /actions/workflows/control.yml/dispatches │ GET /contents/wxread-state/*
                ▼                                                ▼
┌──────────────────────────── GitHub Actions（唯一执行环境） ───────────────────────────┐
│ read.yml     每天 01:07（北京时间）  逐账户阅读，逐次回写 cookie                        │
│ warm.yml     每 6 小时              只做 renewal + 回写（≈40 秒/账户）                 │
│ control.yml  按需                    qr / import / update / delete / schedule / test-* │
│ pages.yml    推送 main               发布 web/                                          │
│ tests.yml    PR / push 门禁          pytest + node 测试 + compileall + 明文密钥扫描     │
└───────────────┬────────────────────────────────────────────────────────────────────────┘
                │ CAS 写（带 sha PUT，失败回读比对再重试）
                ▼
┌──────────────────── 状态分支 wxread-state（只有密文） ────────────────────────────────┐
│ control.json              { public_key, key_version, updated_at }                       │
│ accounts/<id>.enc.json    每账户一份，含 cookie/headers/read_payload/状态               │
│ settings.enc.json         计划、通知渠道、SMTP（密钥字段只存"已保存"标记）              │
│ runs/<yyyy-mm>.enc.json   月度分片，保留 200 条                                          │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 模块边界与依赖方向

依赖只允许向右：`worker → jobs → reader → state → crypto`，`accounts` 与 `notify` 为旁路；禁止反向 import，禁止 `reader` 直接读环境变量（配置由 `worker` 组装后注入）。

| 模块 | 职责 | 明确不做 |
|---|---|---|
| `wxread/crypto.py` | X25519 sealed box（指令/回执）、SecretBox（状态）、AAD 绑定、key_version | 不生成业务密钥、不落盘 |
| `wxread/state.py` | 分支读写、CAS（带 sha PUT + 回读比对）、分账户文件、月度 runs 裁剪到 200 | 不认识"账户"语义，只做字节与版本 |
| `wxread/accounts.py` | 账户模型、字段校验、`needs_login`/`stopped` 迁移、旧 `WXREAD_CURL_BASH` / `WXREAD_ACCOUNTS_JSON` 导入 | 不发网络请求 |
| `wxread/reader.py` | 单账户客户端：renewal、read、`Set-Cookie` 合并回调、L0–L3 判定 | 不写状态分支、不决定流程顺序 |
| `wxread/jobs.py` | 串行队列、进度事件、取消、单账户 deadline | 不做重试策略（在 reader） |
| `wxread/worker.py` | 子命令 `read` / `warm` / `migrate`；读 env、组装依赖、finally 落盘、决定退出码 | 不含协议细节 |
| `wxread/qr.py` | 复用 runner 自带 Chrome 打开登录页、截图二维码、指纹去重；登录后让浏览器读一段书并抓 `web/book/read` | 不解析 cookie（交给 control 流程落盘） |
| `wxread/capture.py` | 从浏览器发出的请求里挑出 `web/book/read`，解析成账户的 `read_payload` | 不启动浏览器、不发请求 |
| `wxread/warm.py` | 只跑 renewal + 写回，连续 2 次 L3 才通知 | 不阅读 |
| `wxread/notify.py` | 邮件 / PushPlus / 飞书 / Telegram；失败重试有上限 | 不含其它渠道 |
| `web/` | 控制台（12 个组件，见 DESIGN §5） | 不内联令牌、不引构建 |

---

## 3. 状态分支与数据模型

分支 `wxread-state` 与 `main` 分离；写入只走 `state.py`。所有 `*.enc.json` 为 SecretBox 密文，明文结构只在本文件定义。

### 3.1 `control.json`（明文，仅公钥）

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `public_key` | base64(32B) | ✔ | X25519 公钥；替换需走 `key_version+1` |
| `key_version` | int | ✔ | 从 1 起单调递增，新旧并存一轮 |
| `updated_at` | ISO8601 | ✔ | UTC |

### 3.2 `accounts/<id>.enc.json`

`<id>` = `sha256(account_uid)[:12]`，不含手机号/昵称；`account_uid` 取 `wr_vid` 派生的稳定值。

| 字段 | 类型 | 必填 | 默认 | 说明 / 迁移 |
|---|---|---|---|---|
| `schema_version` | int | ✔ | 1 | 缺省视为 1；升级只增字段不改语义 |
| `id` | string(12) | ✔ | — | 与文件名一致，创建后不可变 |
| `label` | string | ✔ | `账户-<id前4位>` | 页面展示名，≤ 24 字 |
| `enabled` | bool | ✔ | `true` | `false` 不参与 read/warm |
| `target_count` | int | ✔ | `40` | 每次阅读请求数；1 次 = 30 秒 |
| `cookies` | object | ✔ | — | `wr_vid`、`wr_skey` 及其余登录 cookie；每次响应后合并 |
| `headers` | object | ✔ | — | 抓取时的 UA 等；不含 `Cookie` 头 |
| `read_payload` | object | ✔ | — | `appId`、`ps`、`pc`、`ci`、`co`、`sm`、`pr` —— 账户+设备维度，扫码/导入时写入 |
| `renewed_at` | ISO8601 | ✔ | 导入时间 | 最近一次 renewal 成功时间 |
| `last_ok_at` | ISO8601\|null | ✔ | `null` | 最近一次 `succ=1` 且含 `synckey` 的时间 |
| `status` | enum | ✔ | `ok` | `ok` \| `needs_login` \| `stopped`；仅 L3 置 `needs_login`，人工或扫码成功置回 `ok` |
| `note` | string | ✖ | `""` | 人工备注，不参与逻辑 |

**不变量**

1. `cookies.wr_skey` 与 `renewed_at` 必须同一次写入（同一次 mutate 内），不允许只更新其一。
2. `status=needs_login` 的账户在 `read` 中被跳过，但**不影响**其它账户。
3. `read_payload` 里的 `b`/`c` 不入库；每次请求按该书真实章节列表抽取（修复 P5）。
4. 任何字段缺失时按上表默认值补齐并在 `runs` 记录 `schema_filled`。

### 3.3 `settings.enc.json`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schedule` | object | `{read_cron: "0 7 1 * * "(北京 01:07 由 workflow 固定), warm_every_hours: 6}`；本字段只做页面展示与校验 |
| `channels` | array | `["email","pushplus","feishu","telegram"]` 子集 |
| `smtp` | object | `{host, port, user, from, to[]}`；**密码字段不落分支** |
| `secrets_saved` | object | `{smtp_password: true, pushplus_token: false, ...}` 仅布尔，用于页面显示"已保存" |

### 3.4 `runs/<yyyy-mm>.enc.json`

数组，按 `started_at` 倒序；超过 200 条从尾部裁剪（同月分片内裁剪，不跨文件搬移）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 一次 workflow 运行 = 一个 session |
| `account_id` | string | 对应 `accounts/<id>` |
| `started_at` / `finished_at` | ISO8601 | 账户级起止 |
| `read_count` / `target_count` | int | 完成次数 / 目标次数 |
| `level` | enum | `L0` \| `L1` \| `L2` \| `L3` |
| `reason` | string | 结构化原因码（`ok`、`net_timeout`、`err_-2012`、`no_synckey`、`renew_failed`、`payload_invalid`） |
| `exit_code` | int | 账户级：0 成功、2 需重绑、3 失败 |

---

## 4. 关键流程

### 4.1 首次连接（每个仓库一次）

1. 页面 GET `wxread-state/control.json`；不存在 → 展示"初始化"。
2. 页面生成 X25519 密钥对 → 用 `secrets` 公钥（Actions 侧公钥，用户从仓库设置页复制）封私钥 → 用户点"写入密钥"，页面用 PAT 写 `WXREAD_CONTROL_KEY`（需 `secrets:write`，仅此一次）。
3. 页面用 PAT 写 `WXREAD_STATE_KEY`（32B 随机，页面生成）—— 状态对称密钥不再从私钥派生。
4. 本机 `scripts/keygen.py` 生成密钥对；页面或 workflow 只把**公钥**交给 worker，动作 `init` 写 `control.json`（公钥 + `key_version=1`）。私钥永不进日志。
5. 页面轮询 `runs` 最新回执，解出 `ready`。

### 4.2 扫码绑定

1. 页面 `seal({action:"qr", session_id, issued_at, nonce}, repo_public_key)` → dispatch `control.yml`。
2. worker 校验 60 秒窗口 + nonce 未用 → 启动 runner 自带 Chrome 打开 `weread.qq.com` 登录页 → 截图二维码 → 写 `responses/<session>.enc.json`（seal 到浏览器临时公钥）。
3. 页面显示二维码，轮询回执；手机确认后 worker 抓取会话 cookie、`appId/ps/pc`，`accounts/<id>.enc.json` 落盘（同 `account_uid` 走 update，不新增）。
4. 超时（默认 180 秒）→ 回执写 `qr_timeout`，页面提示"改用 cURL 导入"。

### 4.3 cURL 导入（逃生门）

页面粘贴 curl bash → 本地解析出 cookies/headers/data（复用 `config.py:106-138` 的规则）→ seal 后 dispatch `import` → worker 校验字段齐全（`appId/ps/pc` 必须存在）→ 写账户；缺字段则回执 `missing_fields:[...]`。

### 4.4 每日 read（read.yml）

1. checkout `main` → `wxread-state`（仅该分支，浅克隆）→ 解出全部账户。
2. 账户数 > 5 时由 `control.yml` 分片 dispatch，每片 ≤ 5 账户（见 §6）。
3. 逐账户串行：`renewal(strict=False)` → 循环 read → 每次响应合并 `Set-Cookie` → 每 5 分钟或每 40 次 CAS 写回一次 → finally 强制写回。
4. 单账户 deadline 20 分钟；到达即停，写 `reason=deadline`，继续下一账户。
5. 全部账户结束 → 汇总 `runs` → 按 L1–L3 规则决定是否通知 → 退出码见 §5.3。

### 4.5 warm（warm.yml，每 6 小时）

只对 `enabled && status=ok` 的账户调 `renewal`，写回 `cookies.wr_skey` + `renewed_at`；不做 read。连续 2 次 L3 才推送"请重新扫码"。

### 4.6 页面刷新

轮询 `runs`（30 秒）→ 只按 `data-key` patch 文本/属性；结构变化才重建。编辑区在轮询期间暂停重绘（保焦点、滚动、IME）。连接状态圆点：≤ 30 秒 `--ok-*`，> 30 秒 `--warn-*`，> 2 分钟 `--danger-*`，禁止恒绿。

---

## 5. 失败处理

### 5.1 分级（判据写死）

| 级别 | 判据 | 动作 | 通知 |
|---|---|---|---|
| L0 | `succ=1` 且含 `synckey` | 继续 | 否 |
| L1 | 网络异常 / 5xx，次数 < 3 | 退避 5/15/45 秒后重试 | 否 |
| L2 | `errCode ∈ {-2012,-2013}` 或连续 3 次失败 | 强制 renewal → 重试 1 轮 | 是（"已自愈/未自愈"） |
| L3 | renewal 拿不到 `wr_skey`，或 `ReadPayloadError` 经 chapterInfos 重试仍失败 | 该账户置 `needs_login` 并停止；其它账户继续 | 是（"重新扫码"） |

### 5.2 重试预算

- 单请求：`REQUEST_TIMEOUT = 10s`（沿用 `main.py:21`），只对幂等读取重试。
- 单账户：L1 退避 3 次封顶；L2 全流程最多 1 轮补救；总时长 ≤ 20 分钟。
- 全局：workflow `timeout-minutes` 与分片挂钩，超时前必须已落盘（finally）。

### 5.3 退出码（workflow 级）

| 情形 | 退出码 | 说明 |
|---|---|---|
| 全部账户 L0 | 0 | 绿 |
| 部分成功、部分 L2/L3 | 0 | 黄：通知说明，不红灯 |
| 全部账户失败 | 1 | 红 |
| 状态写回失败（CAS 冲突超限） | 4 | 红，优先排查 |

---

## 6. 容量与成本

- read：40 次 × 30 秒 ≈ 20 分钟/账户/天。
- warm：4 次/天 × ≈1 分钟/账户 ≈ 120 分钟/月/账户。
- 公开仓库 Actions 免费；私有仓库按 2000 分钟/月免费额度估算，> 2 账户需启用"仅 warm + 每周一次完整阅读"低耗模式（P6 提供）。
- 分片：账户数 > 5 → 每片 5 个 dispatch，规避 6 小时上限；每片独立 `session_id`。

---

## 7. 目录规划（P1 之后落地）

```
wxread/                 crypto.py  state.py  accounts.py  reader.py  jobs.py  worker.py  qr.py  capture.py  warm.py  notify.py
web/                    index.html  assets/app.js  assets/components.js  assets/style.css
docs/                   ARCHITECTURE.md  THREAT-MODEL.md  DESIGN.md  REVIEW.md  PLAN.md  RUNBOOK.md( P6 )
docs/design/            tokens.css  variant-steel.html  final-A-1280.png  final-A-390.png  README.md
docs/reviews/           G0-requirements.md  G1-design.md  （后续 G2/G3 记录）
tests/                  test_state_roundtrip.py  test_reader.py  test_accounts_migrate.py
.github/workflows/      read.yml  warm.yml  control.yml  pages.yml  tests.yml
requirements.txt        requests  PyNaCl  playwright（版本锁死，P6）
```

**禁止**：`templates/`、`static/`、`web.py`、`scheduler.py`（仓库本就不存在，禁止回填）、`docker-publish.yml`、任何 UI 框架或打包器。

---

## 8. 迁移与回滚

- 开关：`WXREAD_MODE=legacy|v4`（默认 `legacy` 直到 P4 验证完 3 天）。
- P1–P3 期间 `main.py` 保持可跑；v4 只新增代码与工作流，不改旧路径行为。
- 旧 secret `WXREAD_CURL_BASH` / `WXREAD_ACCOUNTS_JSON` 只读兼容，日志标注 `deprecated`；清理时点在 P6 文档中给出。
- 回滚：`git revert` 单个 PR 即可；状态分支保留，不随代码回滚删除（`key_version` 兼容一轮）。

---

## 9. 四问直答（G0 验收口径）

1. **数据在哪**：明文只在两处——微信读书会话（服务端）与用户浏览器内存；仓库分支只有密文与公钥；Secrets 只有两把密钥与通知 token。
2. **谁能读**：仓库写权限者能读密文（无密钥不可解）；页面持有 PAT 者可发指令；回执只有发起页面能解；日志只留前 2 位。
3. **失败怎么办**：L0 继续、L1 退避、L2 自愈一轮、L3 停该账户并通知；状态写回在 finally，CAS 冲突有界重试；部分成功不红灯。
4. **怎么算完成**：`runs` 出现当日 `L0` 且 `read_count == target_count`；`accounts/<id>.enc.json` 的 `wr_skey`/`renewed_at` 已变化；页面 200 且连接圆点为 `--ok-*`；通知在 L2/L3 时到达。

---

## 10. 附：PLAN 与仓库现状差异（G1 需裁决）

| PLAN 表述 | 仓库实际 | 处理建议 |
|---|---|---|
| §9 删除 `notifications.py` 的企微/钉钉/Webhook 分支 | 文件是 `push.py`，无企微/钉钉/Webhook；但 `push.py:74-115` 仍有 WxPusher / ServerChan | P6 删除这两个渠道，日志标 `deprecated` |
| §9 删除 `templates/`、`static/`、`web.py`、`scheduler.py` | 均不存在 | 仅作为"禁止回填"约束写入 §7 |
| §9 删除 `Dockerfile`、`docker-compose.yml` | `Dockerfile` 存在；`docker-compose.yml` 不存在 | P6 删 `Dockerfile`，同步删 README「方法二」 |
| §2.2 双份前端已分叉（`web.py:81`） | 属参考项目 `wbshm/wxread`，非本仓库 | 仅作反面案例，不产生删除项 |
| §9 删除 `docker-publish.yml` | 不存在 | 无需动作 |
| 每日 01:07 | `deploy.yml:10` 为 `0 17 * * *`（北京 01:00） | 改 `read.yml` 时统一 01:07，并核对 UTC |
| `checkout@v2` / `setup-python@v2` | 实际在 `deploy.yml:32`、`deploy.yml:35` | P6 升 v4/v5 |
| README 称"60 分钟 / 一次部署无需操作" | `config.py:12` 默认 40 次（20 分钟）；续期结果不落盘 | README 随 P6 重写 |
