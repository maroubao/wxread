# 当前架构与行为

更新：2026-11-24。使用步骤见 [RUNBOOK.md](RUNBOOK.md)，修改、验证与升级见 [MAINTENANCE.md](MAINTENANCE.md)。[PLAN.md](PLAN.md) 是历史设计来源；这里描述实际实现。

## 1. 托管方式

唯一日常执行环境为 GitHub Actions，控制台由 GitHub Pages 发布。没有常驻后端、前端构建或本机定时器。浏览器连接可以关闭，云端阅读和续期继续运行。

~~~mermaid
flowchart LR
  U[静态控制台] -->|公钥加密指令| C[control.yml]
  C --> S[wxread-state 密文状态]
  R[read.yml: plan → 分片 read → report] --> S
  W[warm.yml] --> S
  S -->|密文读取 / 浏览器解密| U
  R --> F[飞书批次摘要]
  C --> Q[微信读书扫码与自动取参]
~~~

| 工作流 | 触发与职责 |
| --- | --- |
| read.yml | 每天 UTC 17:07，即北京次日 01:07；手动可指定账户/间隔/截止 |
| warm.yml | UTC 每 6 小时，即北京 02/08/14/20；只续期 |
| control.yml | 页面或本地脚本 dispatch：init、qr、capture、update、delete、stop、schedule、notifications、test-mail/test-push；后端保留旧 import 兼容 |
| tests.yml | main 推送、PR 和手动；Python/Node/静态/密钥扫描 |
| pages.yml | main 的页面/视觉资源变化及手动；发布静态仓库内容，根 index.html 转到 web/ |

read/warm 共享 wxread-session 并发组。control 按 session 分组；不同管理动作可并发，状态层负责保留双方修改。控制台发起的工作流使用 main；修复分支验收通过 GitHub dispatch 的 ref 显式选择分支。

## 2. 模块职责

依赖主方向 worker → jobs → reader；state → crypto。业务配置由 worker 注入，reader 不读取环境变量。

| 文件 | 职责 |
| --- | --- |
| wxread/crypto.py | SecretBox 状态加密、上下文绑定；X25519/NaCl box 指令和回执 |
| wxread/state.py | Contents API、分支准备、CAS 变更、运行幂等追加、密文读写 |
| wxread/accounts.py | 账户模型、目标 1～200 次、数值兼容、Cookie/字段归一化、旧配置读取 |
| wxread/capture.py | 监听浏览器请求和成功响应，只提升必要阅读字段 |
| wxread/qr.py | Chrome/Chromium 登录页、干净二维码、换码、正文导航/下一页/自动取参 |
| wxread/control.py | 指令验证、防重放、账户/设置变更、扫码与已有 Cookie 取参、加密回执 |
| wxread/reader.py | renewal/read 协议、签名、错误 Cookie、有界恢复、读取预算 |
| wxread/jobs.py | 每片串行账户执行、进度/取消、异常隔离、finally 落盘 |
| wxread/warm.py | 独立续期、暂时故障重试、连续明确失效计数 |
| wxread/notify.py | 四渠道发送，飞书签名/返回码检查，脱敏摘要 |
| wxread/worker.py | 环境变量、命令组装、plan/read/report/warm/control/migrate 入口和退出码 |
| web/gh.js | GitHub API、15 秒请求超时、取消信号、24 小时可选连接保存 |
| web/crypto.js | 与 Python 互通的加解密 |
| web/view-model.js | 北京日期/月份、近七日统计、排序、同 run 进度去重 |
| web/app.js | 连接生命周期、局部行更新、扫码回执、任务跟踪、配置弹窗 |
| web/style.css | 引用 docs/design/tokens.css；桌面右栏和手机卡片布局 |

## 3. 状态与密钥

默认分支 main 放程序，wxread-state 放状态。敏感内容在 *.enc.json 中加密，明文上下文绑定 owner/repo 与 key_version。

| 路径 | 内容 |
| --- | --- |
| control.json | 公钥与 key_version；公钥可公开 |
| accounts/<id>.enc.json | 完整 cookies、headers、read_payload、目标、人工开关/备注、健康状态、progress |
| settings.enc.json | 固定计划开关、通知选择和真实渠道口令，全部加密 |
| runs/<UTC yyyy-mm>.enc.json | 月度运行记录，每月文件保留最新 200 条 |
| sessions/<session>.enc.json | 已领取指令的 action、issued_at、claimed_at，用于防重放 |
| responses/<session>.json | 封给发起者临时公钥的回执，包括 pending/capturing 和最终结果 |
| responses/qr-<session>-<seq>-<fingerprint>.png | 短时二维码，页面只取本次会话；新扫码清理旧帧，保护半小时内的已领取会话 |

账户 id 从稳定 wr_vid 派生。同号重绑更新凭据并保留人工设置；停用账户重绑后仍需手动启用。

旧配置只在首次空状态时迁移，完成后在加密设置写 legacy_import_disabled。显式删除账户前也设置此标记，避免最后一个账户删除后被旧 Secret 重建；worker 的运行时回写只接受仍在状态分支的账户。

WXREAD_STATE_KEY 是随机 32 字节对称密钥；WXREAD_CONTROL_SK 是独立指令私钥；control.json 存对应公钥。页面只需要 token 和状态密钥。正常升级保持密钥/版本不变，当前没有全量密钥轮换或跨仓库重新加密工具。

Cookie/口令在 worker 内存和持有状态密钥的浏览器解密内存中使用；可选记住会在浏览器保存 token/状态密钥 24 小时。日志与通知不应含 Cookie 或渠道口令。本机真扫码工具可写忽略的凭据文件，维护者必须保密。

## 4. 自动取参与控制链路

1. 浏览器生成 session、nonce 和临时回执密钥，用仓库公钥封指令，再 dispatch control。
2. worker 解封并校验 action、session、nonce。60 秒新鲜度按 GitHub 接收该 run 的 created_at 计算，排队和安装耗时不使已接收指令过期。
3. 以 create_only 领取 session 防重放，重复请求不覆盖原回执。
4. qr 打开登录页并输出本次帧；扫描成功、等待手机确认时不主动重开页面。
5. 确认登录后先回 pending/capturing，再进入真实 /web/reader/ 正文并主动翻页。
6. 只有匹配阅读请求收到成功 succ/synckey 响应，才采用 appId/ps/pc/b/c/ci/co/sm/pr 等参数。保留数字、co=0、Origin/Referer，捕获结束再读取最新 Cookie。
7. 通过 CAS 写账户，发最终加密回执。页面关闭二维码、刷新账户；取参未完成会自动再发 capture，用已有 Cookie 重试。
8. capture 启动时记录旧凭据；期间已发生重新登录/参数更新时不覆盖新凭据，要求刷新后重试。

后端 import 仅为旧配置/维护兼容，页面没有 cURL 导入路径。取参失败给用户重试、确认正文可读或重新扫码的操作。

## 5. 阅读、分片与落盘

plan 加载账户和开关，按账户目标、间隔与网络余量计算预算。每片最多 5 账户，预算不超过约 330 分钟加启动余量；最多 3 片并行。每片内部串行，一个账户失败继续后续账户。

手动 workflow_dispatch 绕过自动总开关。可用 account_ids 指定账户；为空运行全部。正常跳过停用或 needs_login 账户，汇总仍说明启用但需处理的账户。

Reader 的第一次读取也等待 pace（默认 30 秒）。默认目标 40 次不能使用 1200 秒固定 deadline；自动预算还包含请求及恢复余量。用户正数 deadline 为刻意提前停止的上限。

每次响应合并 Cookie，包括非 2xx 或非法 JSON；变化即时尝试回写。每 10 次读取保存进度，finally 保存最终账户与记录；SIGINT/SIGTERM 请求有界停止。强制杀死或 GitHub 不可写时只能保留已写入内容。

运行字段只与最新状态按字段合并，保留并发备注、目标、启用设置和重新绑定凭据。删除不复活。run_id 防同次记录重复，batch_id 关联矩阵分片；report 在分片结束后汇总一批。没有最终记录但已有当前 batch 进度时，按中断结果纳入摘要。

单账户 stop 指令必须匹配 account_id + run_id，写当前 progress 的 cancel_requested。新运行初始化取消标志，旧 stop 不会取消新任务。「全部停止」取消 read 工作流，不取消其他 control。

## 6. 失败与通知

| 级别 / 情况 | 行为 |
| --- | --- |
| L0 | 正常读取 |
| L1 | 暂时网络/服务故障，退避重试，有次数限制；不把网络故障永久标为需要登录 |
| L2 | 章节/会话恢复等有界补救；记录是否完成目标 |
| L3 | 明确登录失效或参数无法恢复；该账户待处理，不影响其他账户 |
| warm | 暂时失败继续下次尝试；连续两次明确登录失效才通知一次；成功清零 |
| 通知发送失败 | 不删除阅读结果；report 报告发送失败，需修正渠道配置 |

飞书在每天读后发送一条批次摘要，L0/L1 成功也发，L2 恢复完成目标算成功。其他渠道保持 L2/L3 异常提醒，避免给飞书再发同批重复异常消息。停用自动计划时不发正常日常摘要；用户重跑整个 workflow 是新的发送，当前没有跨重跑的通知去重。

FEISHU_WEBHOOK 兼容迁入加密设置；已保存的用户渠道开关不被自动覆盖。飞书可配置 secret 做 HMAC 签名，并检查 JSON 成功码；HTTP 失败只输出脱敏信息。

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常、明确跳过或部分成功；必须结合账户结果判断 |
| 1 | 全部读取失败 / 汇总发送失败 |
| 2 | 配置缺失 |
| 4 | 状态持久化失败 |

## 7. 页面数据与容量

北京日期用 UTC+8 归属；七日跨月时读取需要的 UTC 月份文件。当前只展示读取到的最近记录，最多 100 条；每月底层文件上限为 200 条。按 run_id 去重最终记录与正在执行的 progress。

连接默认内存保存，记住 24 小时才写本机。断开递增连接 epoch 并 AbortController 中止请求、轮询与旧回调。扫码会话的临时回执钥可在当前 tab 会话中恢复，不公开到仓库。

默认一个账户约 20 分钟/天，保活另用少量 runner 时间。公开/私有仓库的额度与计费以 GitHub 账户实际政策为准；不能把完整阅读算成每次不到 2 分钟。状态提交历史会累积，归档前需备份，当前不自动清空 Git 历史。

## 8. 维护边界

不引入 templates/static/web.py/scheduler.py、Docker 或第三方 UI 框架。改微信网页选择器时看真实页面；改协议时看真实请求与响应；改 GitHub/通知时跑真实云端验证。历史需求与本轮证据见 docs/reviews；外部服务的验证等级必须明确区分。
