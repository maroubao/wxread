# RUNBOOK：部署、日常运维与故障处置

面向"自己用"的运维手册。所有命令都在仓库根目录执行；状态数据在 `wxread-state` 分支，不在 `main`。

## 0. 系统长什么样

```
浏览器(web/) --密文指令--> control.yml --> worker control
                                            ├─ 写状态分支 accounts/*.enc.json / settings.enc.json
                                            └─ 写回执 responses/<session>.json
read.yml  (每天 01:07 北京) --> worker read  --> 状态分支 runs/*.enc.json + accounts/*.enc.json
warm.yml  (每 6 小时)       --> worker warm  --> 只更新 accounts/*.enc.json 的 wr_skey
```

- 明文 cookie **只在** worker 进程内存里出现；仓库里只有 `crypto_secretbox` 密文。
- 所有写入都是 Contents API + `sha`/`If-None-Match` CAS，冲突自动重试 3 次。

## 1. 首次部署（约 10 分钟）

1. **建状态密钥**（本机执行一次，结果只存 Secret）：

   ```bash
   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
   ```

2. **配置 Secrets**（仓库 Settings → Secrets and variables → Actions）：

   | Secret | 说明 |
   | --- | --- |
   | `WXREAD_STATE_KEY` | 上一步的 base64，32 字节 |
   | `WXREAD_CONTROL_SK` | 下一步 `init` 打印出来的私钥（base64） |

3. **开 Pages**：Settings → Pages → Source 选 **GitHub Actions**（`pages.yml` 会部署 `web/`）。

4. **生成指令密钥对**：Actions → `control` → Run workflow，`control = init`。跑完在日志里看：

   ```text
   ::notice::control public_key=<base64> key_version=1
   private_key=<base64>          ← 只显示这一次，立刻存进 WXREAD_CONTROL_SK
   ```

   `public_key` 会写到状态分支 `control.json`（明文，公钥本就该公开）。

5. **绑定第一个账户**（二选一）：

   - **扫码**：打开 Pages 站点 → 填仓库、token、状态密钥 → 点"连接并保存" → "生成二维码" → 微信扫。
   - **导入 curl**：浏览器 F12 → Network → 复制 `web/book/read` 请求为 cURL → 控制台"导入"。

   扫码拿到的账户只有 cookie，缺 `appId/ps/pc`（**必须**再导入一次 curl 才有阅读参数），控制台会把 `note` 标成"扫码账户未带 ps/pc"。

6. **第一条真跑**（见 §5 G3 清单）。

## 2. 日常

| 想做什么 | 怎么做 |
| --- | --- |
| 改每日目标次数 | 控制台账户行"编辑" → `update` 指令 |
| 临时补读 | 控制台"立即运行"（`read.yml`） |
| 关掉自动运行 | 控制台"计划"取消勾选，或直接 `schedule` 指令 `enabled: false`（cron 仍在，但 worker 开头就退出 0） |
| 改保活间隔 | `schedule` 指令 `warm_hours: 0/1/3/6/12`；`0` 等于关闭（workflow 仍每 6 小时跑一次，只是空转） |
| 加账户 | 再来一次扫码或导入 |
| 停用某账户 | 账户行"停用"（`update enabled: false`），该账户不再参与 read/warm |

## 3. 故障处置

### 3.1 账户变成 `needs_login`

含义：renewal 拿不到新 `wr_skey`，或读接口连续报会话错误。

处置：控制台重新扫码（同 `wr_vid` 会合并到同一账户 id）；或重新导入 curl。

```bash
# 只看状态、不落盘（本机有 .qr-cookies.json 时）
python scripts/live_check.py
```

### 3.2 `read.yml` 报 `state_error`（退出码 4）

状态分支写不进去：token 权限不足、分支保护、或并发冲突耗尽重试。

1. 确认 `read.yml` 的 `permissions: contents: write` 没被组织策略降级；
2. 确认 `wxread-state` 分支没有开分支保护；
3. 手动重跑一次；仍是 4 就查 `runs/<月>.enc.json` 里该条的 `exit_code`。

### 3.3 阅读数为 0 但没有报错

- 检查 `schedule.enabled` 是否为 false（worker 会打印 `::notice::read skipped`）；
- 检查账户 `target_count` 与 `enabled`；
- 检查 `runs/` 里该次记录 `reason`：`no_synckey` 表示服务端没下发同步键（reader 会自动补救一轮）。

### 3.4 控制台"连接失败"

- 401：token 过期或没勾 `contents: write` / `actions: write`；
- 404：仓库名写错，或状态分支还没建（先跑 `control.yml` `init`）；
- 页面能连但账户显示"（解不开）"：状态密钥填错，或跨仓库搬运（上下文前缀会拒绝，这是设计行为）。

### 3.5 密钥轮换

1. 新状态密钥 `K2` 写进 Secret `WXREAD_STATE_KEY`；
2. 用旧密钥在 `WXREAD_KEYS="1:<旧b64>"` 下跑一次 `control`（`update` 任意账户即可），worker 会所有文件重写成 `K2`（`key_version` 递增）；
3. 确认 `runs/`、`accounts/`、`settings` 都能读后，删掉 `WXREAD_KEYS`。

> 当前实现：`StateStore(old_keys=...)` 已支持读旧版本，但**批量重写脚本尚未提供**（G3 待办）。轮换前先备份状态分支。

### 3.6 二维码不出现

1. Actions 里看 `control` 运行：`playwright install` 失败 → CI 网络问题，重跑；
2. 日志有 `::notice::qr updated ...` 但页面不显示 → 点"刷新二维码"（页面按 `responses` 路径的最近提交定位文件）；
3. 二维码出来了但扫码提示失效 → 点"刷新二维码"取最新一张（每 85 秒换码）。

## 4. 回滚

- **代码**：`git revert <commit>`（状态分支不受影响，数据在 `wxread-state`）。
- **状态**：状态分支是普通 git 分支，`git checkout wxread-state && git revert <bad-commit>` 即可回到上一份密文。
- **彻底停用**：删掉 `read.yml`/`warm.yml` 的 schedule 触发器，或把 `WXREAD_STATE_KEY` 换成一个新随机值（旧密文立刻解不开）。

## 5. G3 真跑清单（上线验收）

按顺序做，每步都有明确的通过标准：

| # | 动作 | 通过标准 |
| --- | --- | --- |
| 1 | `control.yml` `init` | 日志出现 `public_key`；状态分支出现 `control.json` |
| 2 | 控制台连接 | 头部 chip 变"已连接"，账户区显示空状态而不是报错 |
| 3 | 扫码（真实微信） | 账户出现且 `status=ok`；`renewed_at` 有值 |
| 4 | 导入 curl | 同一账户 `read_payload` 补齐 `appId/ps/pc/ci/co`，`note` 清空 |
| 5 | 手动 `read.yml`（`pace=3`，`target_count=3`） | `runs/` 出现记录，`read_count=3`，`succ` 分布在日志里，退出码 0 |
| 6 | 每日定时 | 次日 01:07 后 `runs/` 有当天记录 |
| 7 | `warm.yml` 手动 | 账户 `renewed_at` 前移，`wr_skey` 变化（8 位短码可能不变，看 `renewed_at`） |
| 8 | 通知 `test-mail` / `test-push` | 回执 `ok: true`，邮箱/群里收到消息 |
| 9 | 故意改坏 cookie 跑 `warm` | 该账户变 `needs_login`，其它账户不受影响 |
| 10 | 控制台"停用"再"立即运行" | 该账户被跳过，`runs` 里没有它的记录 |
| 11 | Pages 站点 800px 宽 + 键盘 Tab | 无横向滚动；Tab 能走到每个按钮并有可见焦点环 |
| 12 | `tests.yml` | pytest / node / compileall / 密钥扫描四步全绿 |

## 6. 本地开发

```bash
python -m pytest tests -q                 # 102 passed
node --test tests/js/crypto.test.mjs      # 4 passed（跨语言加密互通）
python scripts/scan_secrets.py            # 明文密钥扫描
python scripts/qr_local.py --wait 300     # 本机真扫码（写 .qr-cookies.json，已 gitignore）
python -m wxread.worker read --verbose    # 需要 GITHUB_* 与 WXREAD_STATE_KEY
```
