# 维护手册：升级、验证、回滚与交接

更新：2026-11-24。日常使用见 [RUNBOOK.md](RUNBOOK.md)。本文件给维护者，包括大肥鱼等代码助手；根目录 [AGENTS.md](../AGENTS.md) 是进入仓库后的第一份约束。

## 1. 哪些文档为准

优先级：用户当前明确要求 → 当前实现与本轮验收 → 本维护手册/使用手册 → ARCHITECTURE 和 DESIGN → 历史计划/阶段报告。

三个稳定的交付目标：

1. GitHub Actions 执行、GitHub Pages 控制台，不要求用户每天开电脑或启动服务。
2. 微信扫码后自动取参，参数失效可用已保存 Cookie 自动重试；日常路径无需 cURL。
3. 每个正常结束的每日阅读批次给飞书摘要，包括成功和未完成结果。

[PLAN.md](PLAN.md) 保留原始需求来源；[DESIGN.md](DESIGN.md) 和 [design/tokens.css](design/tokens.css) 是视觉依据。[reviews/G2-repair.md](reviews/G2-repair.md) 记录本轮验证范围。旧 G0/G1/G2 文档中的「页面导入 cURL」「只能脚本填通知」「固定 1200 秒截止」「L0 不通知」不再是当前行为。

修改功能后同步 RUNBOOK 与 ARCHITECTURE；修改维护流程后同步本文件与 AGENTS；修改可见页面后对照 1280/390 定稿图验收。不要把历史文档整批改成假装从未发生过的记录。

## 2. 一次普通升级怎么做

### 2.1 用户在 GitHub 上操作

1. 在本仓库确认准备合并的 PR 内容、测试结果和迁移说明。
2. 保存现有密钥原件，备份 wxread-state；重要使用环境可先在控制台关闭自动运行，等已有任务结束。
3. 合并经过检查的 PR 到 main。正常代码升级不需要更改 Secrets、control.json 或账户。
4. 等 tests 和 pages 成功。如果仅修改了文档，pages 的路径过滤可能不自动触发；需要更新站点上的文档时手动运行 pages。
5. 打开正确 Pages 地址并强制刷新。检查账户、目标、开关和四渠道设置。
6. 运行保活一次、测试飞书；涉及阅读/取参的更改再做真实阅读或扫码验收。
7. 重新开启自动运行，核对下一次 schedule 阅读的记录和摘要。

旧版本已经建好 wxread-state 时，不重新初始化仓库，不重新生成状态密钥，不重新迁移/导入同一账户。

### 2.2 维护者的本地 Git 流程

在仓库根目录执行。开始先确认分支与未提交改动，保留用户已有修改；有未提交内容时先由维护者辨认，不执行 reset --hard、clean -fd 或覆盖式同步。

~~~powershell
git status --short --branch
git fetch origin
git switch -c maintenance/my-change origin/main
~~~

实现、测试、提交后：

~~~powershell
git add <本次修改的具体文件>
git commit -m "fix(scope): describe the resulting behavior"
git push -u origin maintenance/my-change
~~~

在 GitHub 创建 PR，描述具体问题、最终行为、验证和限制。绿色测试通过且审核完成后合并。不要用 main 的代码覆盖 wxread-state，也不要把私有调试文件加入 PR。

若从本项目 Fork：Sync fork / 合并上游 PR 前先对比上游来源。旧版单文件架构的仓库更新不一定适合当前 v4。选择与当前模块结构兼容的提交，保留自己的定时计划与状态分支；有冲突则在修复分支解决并重新验证。

## 3. 备份和恢复

### 3.1 备份什么

- 原始 WXREAD_STATE_KEY；没有它，密文历史无法恢复。
- WXREAD_CONTROL_SK 与对应公钥；控制指令需匹配。
- wxread-state 的 Git 历史，包括账户、设置、运行记录和 control.json。
- 机器人等口令的安全原件；代码仓库只保存加密配置。

GitHub Secrets 的名字可见，值不能取回。备份存放在自己的安全位置，不上传 PR、公开 issue 或页面截图。

有本地 Git 时，可在仓库根目录保存一份密文分支 bundle：

~~~powershell
New-Item -ItemType Directory -Path .secrets/backups -Force
git fetch origin wxread-state
git bundle create .secrets/backups/wxread-state.bundle origin/wxread-state
git bundle verify .secrets/backups/wxread-state.bundle
~~~

文件名相同时下一次备份应改名，保留需要的旧备份。bundle 是密文状态的历史，必须同时保留原密钥。跨 owner/repo 搬迁会改变加密上下文，不能把旧密文直接复制到新仓库冒充完成迁移。

### 3.2 代码回滚

优先在 GitHub 已合并 PR 页用 Revert，生成并合并回滚 PR，等 tests/pages 完成。或者在新分支：

~~~powershell
git revert <需要回滚的普通提交>
~~~

如果回滚的是合并提交，先确认它的父提交，再使用 git revert -m 1 <merge-commit>。不强推默认分支。代码回滚保留状态分支，但涉及 schema/加密版本的变更必须先确认旧代码能读新数据。

### 3.3 状态恢复

先关闭自动运行、取消/等待全部 read/warm/control 结束，再备份当前状态。修复少数错误设置时优先在控制台重新保存，避免回退整份分支。

确需回退密文状态时，在独立 checkout 检查 wxread-state 的历史，只 revert 明确错误的提交，推回同一状态分支。不要删除并重建状态分支；不要在程序仍写入时回滚。旧 Cookie 可能已经失效，恢复后保活/扫码验收，成功后再开自动运行。

## 4. 必需检查与证据

项目没有前端构建步骤。Python 的运行环境为 3.12；浏览器取参额外安装 requirements-browser.txt；Node 使用 24。本地推荐独立虚拟环境。

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pyflakes wxread scripts tests conftest.py
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall -q wxread scripts tests
.\.venv\Scripts\python.exe scripts/scan_secrets.py
$env:PYTHON = (Resolve-Path .venv/Scripts/python.exe).Path
node --test tests/js/*.test.mjs
node --check web/app.js
node --check web/gh.js
node --check web/crypto.js
node --check web/view-model.js
git diff --check
~~~

CI 的 tests.yml 执行同类检查。只跑与更改相关的回归用于实现迭代，提交前跑完整要求；已经通过后无需反复跑没有新变化的测试。

| 修改范围 | 除自动检查外需验证 |
| --- | --- |
| 页面 | 真实浏览器 1280 和 390，必要时 320；无横向溢出；键盘/焦点；刷新保留编辑；连接失败和断开 |
| 扫码/取参 | 微信扫码和确认、自动打开正文、成功 read 响应、Cookie 轮换；仅捕获请求字段不算有效取参 |
| 阅读协议 | 默认 30 秒节奏的一次完整目标，记录本次/目标、耗时、L0～L3 和结果保存 |
| 状态写入 | 冲突时保留双方改动、删除不复活、重绑优先于旧任务 Cookie、同 run 不重复追加 |
| 通知 | 成功也发飞书、分片只汇总一次、选定账户的摘要只含选定目标、失败不删除读取结果；真实机器人测试 |
| 工作流/依赖 | GitHub 云端 tests；read 的 plan/read/report；warm；Pages 地址与实际部署提交 |

对验证分级写清楚：

- 单元替身：证明函数和故障路径，不证明外部服务可用。
- 本机页面替身：证明真实表单/加密/回执/渲染，不证明 Actions 排队和真实微信登录。
- 本机真实微信：证明微信端扫码/请求；若写入内存后端，不能声称 GitHub 落盘通过。
- GitHub 云端：证明 Actions、实际状态写入和外部通知接口；用户实际收到消息仍是单独的结果。
- 被取消/强制杀死或 GitHub 无法写入时，只能保证已经持久化的内容；不要承诺 finally 在所有情况下运行。

本机真扫码脚本可用 scripts/qr_local.py、scripts/live_check.py。它们产生的 .qr-cookies.json 等文件含凭据，只留本机、已被 Git 忽略。不要打印内容或为了复现而上传。

## 5. 需要遵守的实现约定

### 5.1 账户与状态

- 数据只写 wxread-state；敏感数据使用现有 SecretBox 格式与 owner/repo/key_version 上下文。
- 基于同一次读取的最新内容和 sha 做 CAS；冲突后重新应用变更，不能拿旧整份 JSON 覆盖。
- 人工改动、运行时字段分开合并；后台运行不覆盖备注、目标、启用设置或新扫码的凭据。
- run_id 用于幂等追加；batch_id 用于本次工作流的分片汇总。
- 删除中的账户不重建；停止必须匹配账户和 run_id，不能复用旧取消标志。
- 保留 settings.legacy_import_disabled；旧 Secret 仅首次迁移，删除后的空状态不能再次自动导入。worker 启动后的回写也不能创建已删账户。
- 保留 read_payload 的数字类型和零值，尤其 co=0；保留完整 wr_skey。
- 每次响应含 Set-Cookie 时，即使 HTTP 或 JSON 有错误也合并；网络故障不能直接永久判为登录失效。

### 5.2 自动化和通知

- 日常固定北京 01:07 阅读、每 6 小时保活；页面只改开关。改 cron 时同时更新显示和文档。
- read 与 warm 共享 wxread-session 并发组，cancel-in-progress: false。
- 分片最多 5 账户、预算低于 Actions 单作业 6 小时上限；不要回到固定 45 分钟截断整个批次。
- 读取预算含首次等待、每次请求、网络和恢复余量；默认 40 次不能设 1200 秒硬截止。
- 正常成功/失败批次均给飞书摘要；其他渠道保持异常提醒。通知失败保留阅读结果，report 可报失败。
- 手动任务绕过自动开关；缩短间隔只是验证输入，不改变日常计划。
- 自动关闭/账户停用时跳过必须有可判断的状态，不能把「没执行」报成成功读取。

### 5.3 控制台

- 原生 HTML/CSS/ES module，现有 tweetnacl；不增加打包器、UI 框架或常驻后端。
- tokens.css 是颜色、间距和字体令牌源；参考 DESIGN 定稿。保持右栏、手机卡片和首屏异常提示。
- 默认不持久化 token/状态密钥；明确选择记住才保存 24 小时。断开中止旧异步回调。
- 账户行按 id 更新，保留编辑焦点、输入和 IME；不用整页 innerHTML 重建。
- 回执无法解密不宣布成功；请求/轮询有超时；二维码换图撤销旧 Blob URL。
- 扫码只挑本 session 的帧，取消只匹配本次 control；新扫码不删除其他仍在进行的扫码帧。
- 每个异步按钮/表单防重复提交；失败给用户能执行的下一步。
- 页面无需 cURL；不要把「自动取参失败」解决成让日常用户手工抓包。

## 6. 最可能需要维护的地方

| 变化来源 | 入口 | 判断与修复 |
| --- | --- | --- |
| 微信读书登录/书架/正文 DOM | wxread/qr.py | 观察真实页面链接、正文进入动作、二维码载体；更新选择器和对应导航回归 |
| read/renewal 返回、签名字段 | wxread/capture.py、accounts.py、reader.py | 对照真实成功请求/响应；保留数值、错误 Cookie 与有界恢复，不伪造成功 |
| 并发覆盖、取消、重绑 | state.py、jobs.py、control.py | 先复现冲突/旧任务回写，再修 CAS 变更函数 |
| GitHub 输入、权限、runner 变化 | .github/workflows/、worker.py、web/gh.js | 参数端到端一致；在修复分支跑真实 Actions |
| 飞书签名/接口 | notify.py、web/app.js | JSON 成功码与签名都验证；发送失败信息脱敏 |
| 日期/统计/页面 | web/view-model.js、app.js、style.css | 北京时间跨 UTC 日期、跨月七日数据、同 run 去重；真实浏览器验收 |

日常不需要人工清理账户数据。月度记录每文件保留 200 条；二维码在新扫码时清理旧帧，近期会话暂保留。Git 分支的提交历史会累积，未来如需归档要先备份并验证恢复，不能以自动优化为由清空分支。

接口变动、会话被撤销、GitHub 调度/额度、机器人配置变更属于外部依赖。程序可以自动重试与提示，无法自动完成手机验证码或用户确认。

## 7. 依赖升级

直接依赖按文件固定版本：

- requirements.txt：requests、PyNaCl。
- requirements-browser.txt：Playwright，按需安装。
- requirements-dev.txt：pytest、pyflakes 与运行依赖。
- Actions 主版本和 Python/Node 环境在工作流里。

一次只升级必要的依赖组；阅读发行说明，安装新版本，运行第 4 节检查，涉及 Playwright 还要检查 runner 的系统 Chrome 与 fallback Chromium。不要只在自己的 Python 3.14 上通过就跳过云端 Python 3.12 验证。

更新前端资源后统一提升 HTML/CSS/JS 的版本参数，或验证 Pages 缓存刷新，避免部署了新 HTML 却仍加载旧模块。不要误改加密算法或 key_version 来做普通依赖升级。

## 8. 特殊维护操作

### 8.1 清除旧通知字段

页面口令留空表示沿用。比如机器人关闭签名，需要明确清除保存的 secret 时，用现有本地指令脚本；这样不必让用户重新部署。

创建 .secrets/notify-clear.json：

~~~json
{"feishu":{"secret":""}}
~~~

用当前控制台 token 临时授权脚本，PowerShell 在仓库根目录执行：

~~~powershell
$taskToken = Read-Host "GitHub token" -AsSecureString
$env:GITHUB_TOKEN = [System.Net.NetworkCredential]::new('', $taskToken).Password
.\.venv\Scripts\python.exe scripts/control_local.py notifications --repo maroubao/wxread --payload-file .secrets/notify-clear.json --wait 900
Remove-Item Env:\GITHUB_TOKEN
~~~

Fork 后替换 repo。此操作只清除指定字段，不更改频道选择。接着在页面测试飞书。其他字段同样用明确的空字符串清除；不要公开 payload 文件里原来的真实值。

### 8.2 密钥轮换与仓库搬迁

当前没有通用的一键全量状态重新加密/跨仓库迁移工具。不要照旧 RUNBOOK 的「改 Secret 后 update 一个账户就会全部迁移」说法操作；那不会重写所有历史文件。

普通升级保持两把密钥不变。确需轮换/搬迁时，先暂停全部作业、备份原分支与密钥，设计对 accounts/settings/runs/sessions 全量读取与重写的迁移，用恢复副本验证旧/新数据后再切换。StateStore 的 old_keys 能读指定旧版本，但不是批量迁移脚本。没有完整迁移与恢复证据，不替换 WXREAD_STATE_KEY，也不随意重新 init 新公钥。

## 9. 给大肥鱼的任务模板

可直接把下面这段连同具体问题交给维护者：

> 请维护这个 wxread v4 项目。先阅读 AGENTS.md、docs/MAINTENANCE.md、docs/RUNBOOK.md、docs/ARCHITECTURE.md；页面修改再读 docs/DESIGN.md 和定稿图。保留 GitHub 全托管、扫码自动取参无需 cURL、每天读后飞书摘要这三个核心目标。先检查默认分支实际代码和已有修改，复现问题再修复，保留状态与密钥。提交前运行规定检查，涉及页面必须实际打开桌面/手机尺寸验收；涉及外部服务分开记录本机替身、真实微信和真实 GitHub 的证据。同步文档，给出变更、验证、限制和回滚方法。

交付时不要只给示意图、测试数量或「功能已完成」一句话。至少提供修复提交/PR、运行链接、实际页面结果、相应使用手册更新和未验证项。
