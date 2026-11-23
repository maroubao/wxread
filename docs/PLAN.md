# wxread 重构方案（v4 控制台版）— 供确认后执行

> 目标仓库：`maroubao/wxread`（你的 fork）
> 参考项目：`wbshm/wxread`（阅伴，Pages 控制台）
> 本文件是**方案与计划**，不含实现。确认后在新对话执行。

---

## 0. 一句话目标

把"单账号 + 手动重抓 curl + 每次过期要人管"的脚本，升级为：

**多账号 · 网页扫码绑定 · 加密状态自动回写 · 每日自动跑 · 过期只在真被踢下线时才提醒**。

非目标（本期不做）：多租户 SaaS、对外开放注册、手机 App、把 Cookie 存到第三方云。

---

## 1. 现状审核：你的项目（v3）

### 1.1 已确认的根因

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1 | 续期结果只存在内存 | `main.py:96-126`，`refresh_cookie()` 只改 `cookies["wr_skey"]`，进程结束即丢 | **每次运行都从 Secrets 里那份旧 curl 起步 → 必然周期性"过期"**，这是你要反复重抓的直接原因 |
| P2 | read 响应里的 `Set-Cookie` 被整份丢弃 | `main.py:84-93` 只返回 `response`，从不读取 `response.cookies` | 服务端轮换的 cookie 全部丢失，等于每次都在用最老的凭证 |
| P3 | 失败重试无上限 | `main.py:212-220`：请求异常 → `refresh_cookie()` → `continue`，`index` 不增、无 `sleep`、无计数 | 网络抖动或凭证半失效时可能空转烧满 6 小时额度 |
| P4 | 请求体字段无法自愈 | `config.py:69-85` 的 `appId/ps/pc` 是写死模板 | 抓包太久后报参数失效，只能重抓（这是"过期"的第二种形态） |
| P5 | 书籍与章节随机错配 | `main.py:132-133`：`b` 从 `book[]`、`c` 从 `chapter[]` 各自随机 | 章节不属于该书，服务端可能不计时长（隐蔽的"跑了但没加分"） |
| P6 | 修复接口写死一本书 | `main.py:111` `bookIds: ["3300060341"]` | 非三体账户的 synckey 修复无效 |
| P7 | 单账号硬编码 | `config.py` 全局唯一 `cookies/headers/data` | 无法多账号 |
| P8 | 失败信息靠人读日志 | `main.py:155-188` 只做文案分类 | 没有状态、没有历史、没有"该做什么"的结构化输出 |
| P9 | 工作流过时 | `deploy.yml:32,35` 用 `checkout@v2`/`setup-python@v2` | Node20 弃用告警，随时可能失效；`cancel-in-progress: true` 会让同组任务互相取消 |
| P10 | 无自动化测试 | 仓库无 `tests/` | 任何改动都只能靠"明天看有没有加时长"验证 |

### 1.2 结论

P1+P2 是"过期"的**机械原因**：不是 cookie 活得短，而是**你把刷新出来的新 cookie 扔了**。
所以修复的核心不是"延长 cookie 寿命"，而是**把每次刷新的结果持久化，并在两次运行之间保持活跃**。

---

## 2. 参考项目审核：`wbshm/wxread`

### 2.1 它解决对了什么

- **状态存 Git 分支** `wxread-state`：`control.json`（公钥）+ `state.enc.json`（SecretBox 密文，含 cookie/headers/通知密钥）+ `responses/<session>.json`（回执）。
- **浏览器生成 X25519 密钥对**，私钥用仓库公钥 sealed-box 加密后写入 secret `WXREAD_CONTROL_KEY`；Actions 用私钥解密指令、加密状态。页面永远拿不到明文状态。
- **扫码绑定**：`control.yml` 在 runner 上用 Chrome 打开登录页截图二维码 → 页面显示 → 手机确认 → 抓 cookie 加密入库。**这才是"再也不用手动抓包"的正解。**
- **续期回写**：`reader.py:54-63` 每次响应都 `response.cookies.get_dict()` 合并并回调 `on_cookies`，`jobs.py` 存 sqlite，`github_worker.py:243-255` 定期 + finally 同步到分支。
- **CAS 写入**：`github_state.py:113-133` 带 sha PUT，失败先回读比对再重试，解决"响应丢了但实际写成功"。
- **单 worker 串行 + 每账号独立 Session**：账户间不串 cookie，一个失败不影响其他。

### 2.2 它的坑（我们不照抄）

| 问题 | 位置 | 我们的处理 |
|---|---|---|
| 双份前端（`pages/` 与 `templates/+static/`）已分叉 | `web.py:81` | 只保留一套，删掉 Flask 版 |
| 状态靠 git commit，运行期约每分钟一个 commit，分支历史永久留旧密文 | `github_worker.py:243` | 分账户拆文件 + 变更才提交 + 进度 5 分钟一次 + runs 保留 200 条 |
| `appId/ps/pc` 仍是固定模板，过期只靠 chapterInfos 兜 | `config.py:66-82` | 显式"凭证健康检查"+ 明确降级路径（见 §4.3） |
| 只要有账户 `needs_login`，整次 workflow exit 1 → 每天红灯 | `github_worker.py:258` | 区分"部分成功"，退出码按"是否全部失败"决定 |
| `timeout-minutes: 360` 与 20 分钟/账户冲突，17 个账户必超时 | `deploy.yml:33` | 分片 dispatch，每片 ≤ 5 账户；超时前主动落盘 |
| 授权等价于"能写仓库 Actions"，指令无 nonce/防重放 | `control.yml:25-27` | 指令绑定 session + 60 秒过期 + `session_version` 校验 |
| 私钥可存 localStorage 7 天 | `github.js:20-22` | 默认只存内存，勾选才存，且 24 小时过期 |
| SecretBox 密钥由静态私钥派生，不可轮换 | `github_state.py:147` | 独立随机密钥 + AAD 绑 `repo|version`，预留轮换字段 |
| 视觉：9–11px 灰字（对比 <4.5:1）、整页 `innerHTML` 重建（每 60s 丢焦点/滚动）、无深色模式、危险操作无区分 | `style.css:1`、`app.js:38-96` | 整套重做（见 §5） |

### 2.3 三方对比

| 能力 | 你的 v3 | wbshm | 本方案 v4 |
|---|---|---|---|
| 每日自动跑 | ✅ | ✅ | ✅ |
| 续期结果持久化 | ❌ | ✅（每分钟 commit） | ✅（变更才提交，分账户） |
| 免抓包加账号 | ❌ | ✅ 扫码 | ✅ 扫码 + cURL 导入兜底 |
| 多账号 | ❌ | ✅ | ✅（含并行分片） |
| 过期自动降级/告警 | ❌ | 部分 | ✅ 三级：续期→重试→仅提醒 |
| 可视化 | ❌ | ✅ 但界面弱 | ✅ 重做（DESIGN.md 驱动） |
| 测试 | ❌ | ✅ 有 | ✅ 有 + 新增 3 类关键用例 |
| 运维依赖 | 无 | 无 | 无（仍只用 GitHub） |

---

## 3. 目标架构

```
浏览器（GitHub Pages 静态站，无构建）
  │  1. 用户填 owner/repo + fine-grained PAT
  │  2. 生成临时密钥对 → 用仓库公钥 sealed-box 封指令
  ├──► POST /actions/workflows/control.yml/dispatches  （扫码/导入/改配置/测试通知）
  └──► GET  /contents/wxread-state/...                 （读加密状态，本地解密显示）

GitHub Actions
  ├── read.yml   每天 01:07（北京时间）→ 逐账户阅读，逐次回写 cookie
  ├── warm.yml   每 6 小时 → 仅 renewal + 回写（约 40 秒，不阅读）
  ├── control.yml 扫码/配置/测试（按 session 并发，10 分钟超时）
  ├── pages.yml  发布 web/
  └── tests.yml  PR/推送门禁（pytest + node + 静态检查）

状态（分支 wxread-state）
  ├── control.json                  {public_key, key_version}
  ├── accounts/<id>.enc.json        cookie/headers/目标次数/邮箱/enabled/status
  ├── settings.enc.json             计划/通知/SMTP（密钥字段只存"已保存"标记）
  └── runs/<yyyy-mm>.enc.json       月度分片，保留 200 条
```

### 3.1 密钥与信任边界

- 页面首次连接：生成 X25519 密钥对 → 用 `secrets` 公钥封私钥 → 写 `WXREAD_CONTROL_KEY`（需要 PAT 的 Secrets 写权限，仅首次）。
- 状态对称密钥：**独立随机 32 字节**，存 `WXREAD_STATE_KEY`；不再从私钥派生。
- 指令：`seal(payload, repo_public_key)` + `{session_id, issued_at, nonce}`，worker 校验 60 秒窗口与 nonce 未用过。
- 回执：`seal(payload, browser_ephemeral_public_key)`，只有发起页面能解。
- 明文 Cookie **永不**进入 workflow input、日志、commit message。

### 3.2 数据最小化

- 状态分支只放密文；`control.json` 只有公钥。
- 页面默认不落盘 Token；勾选"记住"写 `localStorage`，24 小时过期，断开即清。
- 日志脱敏：cookie 值、token、`wr_vid` 只打前 2 位 + `***`。

---

## 4. 关键策略：让"过期"变成罕见事件

### 4.1 四层防护

1. **每次运行前**：`renewal`（`strict=False`），拿不到新 `wr_skey` 也继续尝试，记录健康度。
2. **每次响应后**：合并 `Set-Cookie` → 回调 → 内存态更新。
3. **运行结束（finally）**：强制写回状态分支，**即使中途异常**。写失败先回读比对再重试（CAS）。
4. **每 6 小时 warm**：只调 `renewal` 并回写。让 `wr_vid`/`wr_skey` 长期处于活跃态，而不是放一天再动。

### 4.2 失效分级（决定要不要打扰你）

| 级别 | 判据 | 动作 | 是否通知 |
|---|---|---|---|
| L0 正常 | `succ=1` 且有 `synckey` | 继续 | 否 |
| L1 瞬时 | 网络错误/5xx，<3 次 | 退避 5/15/45s 重试 | 否 |
| L2 凭证可疑 | `errCode ∈ {-2012,-2013}` 或连续 3 次失败 | 强制 renewal → 重试 1 轮 | 是（"已自愈/未自愈"） |
| L3 需重绑 | renewal 拿不到 `wr_skey`，或账户状态 `needs_login` | 停止该账户，保留其他账户继续 | 是（明确"请扫码重新绑定"） |

配套：`warm.yml` 连续 2 次 L3 → 立即推送，而不是等你发现没加时长。

### 4.3 `appId / ps / pc` 的处理（诚实说明）

这三个字段是**账号+设备**维度、抓包时固化。没有免费接口能重新生成，所以策略是：

- 扫码绑定时从**登录后的真实会话**抓取一次，写入该账户记录（不再用全局模板）。
- 运行中若报"参数失效"（`ReadPayloadError`），**先**用 `chapterInfos` + 重新 renewal 重试；仍失败才判定为 L3，提示"重新扫码"（扫码会自动刷新这三个字段）。
- 页面显示每个账户的"凭证年龄"与"上次成功时间"，超过 30 天标黄提示。

### 4.4 时长与成本

- 默认 40 次 × 30s ≈ 20 分钟/账户/天；公开仓库 Actions 免费，私有仓库约 600 分钟/月/账户（免费额度 2000）。
- `warm.yml`：4 次/天 × 约 1 分钟 ≈ 120 分钟/月。
- 分片策略：账户数 > 5 时按 5 个一片拆多次 dispatch，避免 6 小时上限。

---

## 5. 界面重做（用 DESIGN.md 驱动）

`getdesign.md` 提供 550+ 个网站的 DESIGN.md（颜色、字号、间距、组件规则与理由），可请求私有定制。我们的做法：

1. 先写 `docs/DESIGN.md`：定位"工具型控制台"，浅色为默认 + 深色跟随系统；主色单一强调；字号 12/13/15/20/28；间距 4/8/12/16/24/32；圆角 6/10/14；对比度 ≥4.5:1；状态色仅用于状态。
2. 参考审美方向从 catalog 里选（候选：Linear / Cal.com / ClickHouse 类"克制工具风"），只借规则，不抄品牌。
3. 组件：`AppShell` `MetricCard` `AccountRow` `StatusBadge` `RunTable` `ChannelCard` `ConfirmDialog` `EmptyState` `Field` `Toast`——用 `<template>` + 原生 Web Components，**零构建**，Pages 直接托管。
4. 渲染：改为**局部 patch**（按 `data-key` 更新文本/属性），只在结构变化时重建；轮询中暂停编辑区重绘，保留焦点、滚动位置、IME 输入。
5. 移动端：≤800px 表格转卡片；操作列定宽；进度条固定行高，消除行高抖动。
6. 可访问性：`aria-live` 只包 toast 区；焦点陷阱只在真模态；31px 图标按钮 → 40px；危险操作弹确认并显示对象名。

---

## 6. 分期计划

> 每期都有"审核 → 实现 → 复审"三段，见 §7。工期按"一次新对话内可完成"切分。

### P0 基线与文档（不改业务代码）
- 产出：`docs/ARCHITECTURE.md`、`docs/THREAT-MODEL.md`、`docs/DESIGN.md`、`docs/REVIEW.md`、`docs/PLAN.md`（本文件迁入仓库）
- 验收：四项文档能回答"数据在哪、谁能读、失败怎么办、怎么算完成"

### P1 状态层与加密（无 UI）
- 产出：`wxread/crypto.py`、`wxread/state.py`（CAS 写入、分账户文件、月度 runs）、`wxread/accounts.py`（模型、校验、从旧 `WXREAD_CURL_BASH`/`WXREAD_ACCOUNTS_JSON` 迁移）
- 验收：`pytest tests/test_state_roundtrip.py` 覆盖——并发写冲突重试、响应丢失后回读、密文篡改报错、旧格式迁移

### P2 阅读内核（多账号）
- 产出：`wxread/reader.py`（单账户客户端，cookie 回调、L0–L3 分级）、`wxread/jobs.py`（串行队列、进度、取消）、`wxread/worker.py`（`read` 子命令）
- 验收：`pytest tests/test_reader.py` 用假服务端模拟：`succ`/无 synckey/`errCode -2012`/超时/连续失败，断言重试次数与最终状态

### P3 扫码与配置（Actions 侧）
- 产出：`wxread/qr.py`（复用 runner 自带 Chrome，二维码指纹去重）、`control.yml` 的 `qr/import/update/delete/schedule/notifications/test-*`
- 验收：真机扫一次码能把账户写进状态分支；重复绑定同账号走"更新"不新增

### P4 续期保鲜
- 产出：`warm.yml` + `wxread/warm.py`；`read.yml`/`warm.yml` 共用 `state.mutate` 的 cookie 写回
- 验收：手动触发 warm 后 `accounts/<id>.enc.json` 的 `wr_skey` 与 `renewed_at` 变化；连跑 3 天不出现 L2

### P5 前端重建
- 产出：`web/`（`index.html` + `assets/`），删除 `templates/`、`static/`、`web.py`、`scheduler.py`
- 验收：Lighthouse 可访问性 ≥90；800px 宽无横向滚动；键盘可完成"运行/停止/编辑/删除"；`npm` 无关、零构建

### P6 门禁、文档、迁移
- 产出：`tests.yml`（pytest + 3 个 node 测试 + `python -m compileall` + 明文密钥扫描）、`docs/RUNBOOK.md`（每次失效该点哪里）、旧 secret 清理说明
- 验收：故意提交一个明文 token，门禁必须失败

---

## 7. 审核机制（你要的"审核 → 写码 → 审核"闭环）

### 7.1 四道门

| 门 | 时机 | 谁审 | 通过标准 |
|---|---|---|---|
| **G0 需求门** | 动工前 | 你 + 我 | 每条需求有编号与可验证的验收标准；歧义清零 |
| **G1 设计门** | 写代码前 | 我出文档，独立 reviewer 审 | 数据结构、错误路径、威胁模型、界面令牌都写死；无"待定" |
| **G2 实现门** | 每个 PR | 实现者自查 + 独立 reviewer（独立上下文，不看我的解释只看 diff） | 清单 12 条全过 + 测试绿 |
| **G3 验收门** | 合并前 | 真跑一次 Actions | 状态分支只增预期 commit；cookie 确实变了；页面 200；通知到达 |

### 7.2 G2 固定清单（每次提交都要过）

1. 是否只做本 PR 声明的事（无顺手重构）
2. 错误路径是否都有明确类型与消息（不吞异常）
3. 是否有无上限重试/无超时请求
4. 日志/异常/commit message 是否泄露 cookie、token、手机号
5. 新增状态字段是否有迁移与默认值
6. 并发写是否走 CAS，冲突是否有界重试
7. 是否有对应测试；测试是否会在实现错时失败（反向验证）
8. 删除的代码是否真的无人引用（`grep` 证明）
9. 界面改动是否保留焦点/滚动/输入
10. 是否有硬编码的时间、路径、账号、书籍 ID
11. 依赖是否新增；新增是否必要（优先标准库）
12. 回滚是否可行（能否一键 revert 而不丢状态）

### 7.3 记录与阻断

- 每次审核产出 `docs/reviews/G{0-3}-{phase}.md`：结论（通过/有条件/驳回）、问题分级（必须修/建议/可忽略）、负责人、状态。
- **必须修 = 0** 才允许进入下一门；驳回时写清"重做什么"，不写"再优化一下"。
- 自动门禁不可绕过：`tests.yml` 红则不合。

---

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 状态分支写坏/密钥丢 | 页面提供"导出加密备份"与"导出 cookie 明文（本机下载）"；`control.json` 保留 `key_version`，允许新旧密钥并存一轮 |
| 扫码被风控/需要验证 | 保留 cURL 导入作为逃生门；页面提示"扫码失败就用导入" |
| Actions 排队延迟 | 定时避开整点（01:07）；warm 每 6 小时兜底，不依赖单次准点 |
| 私有仓库分钟数超限 | 默认按公开仓库设计；若改私有，P6 提供"仅 warm + 每周一次完整阅读"的低耗模式 |
| 重构期间断更 | P1–P3 期间旧 `main.py` 保持可跑；切换用一个开关（`WXREAD_MODE=legacy|v4`），验证 3 天后再删旧码 |

---

## 9. 已定稿决策（2026-11-23）

| 决策 | 结论 | 影响 |
|---|---|---|
| 本地/Docker 版 | **删除**，只留 GitHub Action 一套 | P5 顺手删 `templates/`、`static/`、`web.py`、`scheduler.py`、`Dockerfile`、`docker-compose.yml`、`requirements.txt` 中仅本地用的依赖 |
| 视觉方向 | **A 浅色克制工具风**（默认浅色，不强制深色） | `docs/DESIGN.md` 只维护一套浅色令牌；不使用 `color-scheme` 自动切换，避免双套回归 |
| 主色 | **A 钢蓝 `#2563EB`**（浅阶 `#A8C3FA`，hover `#1D4ED8`，浅底 `#EAF0FE`） | 已写入 `D:\Projects\deepseek-harness\wxread-plan\DESIGN.md` §2 + `mock/tokens.css`；实现时令牌只许定义在这两个文件的落地副本 `docs/design/tokens.css` 里 |
| 交互与图表语义 | 颜色只表达四类状态：主色=满额/今日、浅阶=未满/进行中、琥珀=需重绑、红=失败；彩色面积 ≤ 8% | G2 清单逐条核对；禁止渐变/折线/饼图/双 Y 轴 |
| 通知渠道 | **邮件 + PushPlus + 飞书 + Telegram** | 不实现 WxPusher / Server酱 / 企微 / 钉钉 / 通用 Webhook；旧环境变量保留读取但标记 deprecated |
| 多账号 | 本期直接支持（P2/P3） | 账户数 > 5 走分片 dispatch |
| 保鲜节奏 | 每天 01:07 阅读 + 每 6 小时 warm | warm 失败连续 2 次才打扰用户 |

### 9.1 由上述决策带来的额外删减

- 删除 `notifications.py` 中企微/钉钉/通用 Webhook 分支与其签名逻辑（约 4 个类、2 组测试）。
- 删除 Docker 发布工作流 `docker-publish.yml`。
- `requirements.txt` 只保留 Actions 运行时需要的包（`requests`、`PyNaCl`、`playwright`），版本锁死。

---

## 10. 新对话怎么开始

新对话直接粘：

```
按 D:\Projects\deepseek-harness\wxread-plan\PLAN.md 执行。
设计已定稿：D:\Projects\deepseek-harness\wxread-plan\DESIGN.md（A 钢蓝 #2563EB）
与 mock/tokens.css，样张见 mock/variant-steel.html —— 直接落地，不要重新设计配色。
先做 G0/G1：把 PLAN + DESIGN 落成仓库里的 docs/（ARCHITECTURE / THREAT-MODEL / DESIGN / REVIEW），
DESIGN.md 原样搬成 docs/DESIGN.md，tokens.css 放 docs/design/tokens.css，
然后停下来等我审核文档，不要写业务代码。
```

**新对话禁止事项（防止走样）**：不得更换主色、不得引入 UI 框架或构建工具、不得把令牌分散硬编码进组件；一旦发现与 `DESIGN.md` 冲突，以 DESIGN.md 为准并记录到 `docs/reviews/G1-design.md`。

我会在 G1 通过后才动 P1 的代码。
