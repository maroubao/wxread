# G1 设计门 — wxread v4

- 日期：2026-11-23
- 被审对象：`docs/ARCHITECTURE.md`、`docs/THREAT-MODEL.md`、`docs/DESIGN.md`（v1.0 A 钢蓝）、`docs/design/tokens.css`、`docs/REVIEW.md`
- 审核人：用户（裁决）/ 执行者（整理）；建议由独立 reviewer 只看文档复核一遍
- 结论：**待用户裁决**（含 1 项必须批准的偏离，见 §3）
- G1 的过关标准是一条：**实现期不需要再做设计决策**。下面逐项给出"已经写死在哪"。

---

## 1. 已写死的决策（实现期不得再议）

| 类别 | 决策 | 出处 |
|---|---|---|
| 运行环境 | 只用 GitHub Actions + Pages；不引入服务器、Docker、本地调度 | ARCHITECTURE §0、§7 |
| 数据落点 | 独立分支 `wxread-state`；`accounts/<id>.enc.json` + `settings.enc.json` + `runs/<yyyy-mm>.enc.json` + 明文 `control.json` | ARCHITECTURE §3 |
| 账户主键 | `id = sha256(account_uid)[:12]`（`account_uid` 由 `wr_vid` 派生），文件名与 `id` 一致，创建后不可变 | ARCHITECTURE §3.2 |
| 字段默认值 | `label=账户-<id前4位>`、`enabled=true`、`target_count=40`、`status=ok`、`note=""` | ARCHITECTURE §3.2 表 |
| 不变量 | `wr_skey` 与 `renewed_at` 同一 mutate 写入；`needs_login` 跳过但不断批；`read_payload.b/c` 不入库 | ARCHITECTURE §3.2 |
| 写回时机 | 每 5 分钟或每 40 次 CAS 写一次，`finally` 强制写；CAS 冲突有界重试 | ARCHITECTURE §4.4、THREAT-MODEL T-04 |
| 错误分级 | L0 继续 / L1 退避 5·15·45 秒 / L2 renewal 补救 1 轮 / L3 停该账户 + 通知 | ARCHITECTURE §5.1 |
| 退出码 | 全 L0=0；部分失败=0；全失败=1；状态写回失败=4 | ARCHITECTURE §5.3 |
| 限额 | `REQUEST_TIMEOUT=10s`、单账户 20 分钟 deadline、单请求重试 3 次、响应体 64KB、`runs` 保留 200 条 | ARCHITECTURE §5.2、THREAT-MODEL §7 |
| 密码学 | 指令/回执 sealed box；状态 AEAD + AAD `wxread-state\|<owner>/<repo>\|key_version`；`key_version` 单调 | THREAT-MODEL §5 |
| 重放防护 | 指令含 `session_id`/`issued_at`(60s)/`nonce`；以 `sessions/<id>.enc.json` 用 `If-None-Match: *` 创建成功作为"已消费" | THREAT-MODEL §4 T-02、§6.4 |
| 指令白名单 | `init`/`qr`/`import`/`update`/`delete`/`schedule`/`notifications`/`test-mail`/`test-push` | THREAT-MODEL §6.3 |
| 通知渠道 | 只 4 个：邮件、PushPlus、飞书、Telegram | PLAN §9、ARCHITECTURE §2.1 |
| 依赖方向 | `worker → jobs → reader → state → crypto`，禁止反向；`reader` 不读环境变量 | ARCHITECTURE §2.1 |
| 视觉 | A 钢蓝 #2563EB；字阶 28/30/15/13/12/11；间距 4–56；圆角 6/10/14；12 组件状态齐 | DESIGN §2–§5；`docs/design/` |
| 禁用 | 构建工具、UI 框架、`innerHTML` 拼外部数据、`templates/`/`static/`/`web.py`/`scheduler.py` | ARCHITECTURE §7、THREAT-MODEL T-14 |

## 2. "无待定"自查（逐项确认已给出确定答案）

| 常被留成待定的问题 | 本文档给的答案 |
|---|---|
| 账户 id 怎么生成 | `sha256(account_uid)[:12]`，`account_uid` 由 `wr_vid` 派生 |
| 扫码截图怎么拿 | runner 自带 Chrome（Playwright 已装）打开 `weread.qq.com` 登录页截图，指纹变化即去重 |
| 回执怎么不被别人看到 | seal 到浏览器**进程内临时** X25519 公钥，10 分钟后 workflow 删文件 |
| 公钥怎么第一次给进去 | 用户从仓库 Secrets 页复制 Actions 公钥 → 页面用它 seal `WXREAD_CONTROL_KEY` → PAT 写入 |
| 对称密钥从哪来 | 页面生成 32B 随机数，直接 PAT 写 `WXREAD_STATE_KEY`，不从私钥派生 |
| nonce 表放哪 | 不建表，用 `sessions/<id>` 创建成功表示已消费 |
| 状态冲突怎么办 | `expected_sha` PUT；失败回读比对 `updated_at`，只重试 3 次，超限退出码 4 |
| 账户多到超 Actions 时长怎么办 | > 5 账户由 `control.yml` 分片 dispatch，每片 ≤ 5，各自 `session_id` |
| 页面怎么不打断用户输入 | 轮询只 patch `data-key` 文本/属性；编辑态暂停重绘；保持焦点/滚动/IME |
| 连接状态怎么不恒绿 | 30s 内 `--ok-*`，30s–2min `--warn-*`，>2min `--danger-*` |
| 旧 secret 怎么办 | 只读兼容，日志 `deprecated`，P6 文档给清理步骤，不自动删 |
| Dockerfile 怎么办 | P6 删除，README「方法二」同步删 |

## 3. 必须批准的偏离（唯一一条）

**THREAT-MODEL §5.1**：PLAN 写"SecretBox + AAD"，但 `crypto_secretbox` 无 AAD 参数。本文改为 **XChaCha20-Poly1305-IETF**（真 AEAD，PyNaCl bindings 提供），AAD 绑定 `repo|key_version`；若环境不可用则退化为 secretbox + 明文上下文前缀。

- 影响：`state.py` 的 API 与测试用例；不影响数据模型与页面。
- 需要用户一句"批准"。不批则按 §5.1 的退化实现，测试同样覆盖"AAD 变则失败"。

## 4. 交叉核对：DESIGN 的 12 组件 ↔ 实现组件名

| # | DESIGN §5 组件 | 元素/类名 | 必做状态 |
|---|---|---|---|
| 1 | 顶栏 | `.bar` + `.brand` + `.dot` | 正常 / 连接 3 档 / 移动折叠 |
| 2 | 指标卡 | `.grid > .card.metric` | 无数据 `—` / 加载骨架 |
| 3 | 账户列表 | 行 + `.badge ok\|warn\|run\|bad` | 4 种状态 + hover + 禁用 |
| 4 | 账户详情抽屉 | `.drawer` | 打开/关闭/滚动锁定/Esc |
| 5 | 添加账户对话框 | `.dialog` | 空/校验错/提交中/成功 |
| 6 | 扫码面板 | `.qr`（含倒计时） | 等待/已扫/超时/失败 |
| 7 | 运行记录表 | `.table` | 空/加载/超 200 条分页感 |
| 8 | 删除确认 | `.dialog.danger` | 显示对象名 + 二次输入 |
| 9 | 通知设置 | form + `.badge` 已保存 | 未配/已配/测试发送中/失败 |
| 10 | 计划设置 | 数字输入 + 折算分钟 | 越界提示（1–200 次） |
| 11 | 空状态 | `.empty` | 首次使用引导 |
| 12 | Toast | `.toast` | 成功/警告/错误 + 自动消失 |

CSS 变量**只能**来自 `docs/design/tokens.css`；组件不得自带颜色字面量（DESIGN §10 禁止清单）。

## 5. 残余设计风险

| 风险 | 说明 | 处理 |
|---|---|---|
| 扫码截图依赖 Playwright + Chrome | 首次运行下载浏览器可能超 2 分钟 | `control.yml` 里 `playwright install --with-deps chromium` 并缓存 |
| Pages 是公共的 | 任何人能看到界面骨架 | 界面不含数据；无 PAT 时全部显示空/引导态 |
| 密文分支可见 | 有仓库读权限者可下载历史密文 | 靠密钥不落仓库；密钥轮换文档化 |
| `runs` 200 条裁剪 | 丢失更早历史 | 页面只承诺近 7 日 + 当月 |
| token 存 localStorage 选填 | 共享电脑残留 | 24 小时过期 + 断开即清（DESIGN/PLAN 一致） |

## 6. 判定与下一步

- **G1 结论等待用户**：§1 决策表、§2 无待定自查、§3 偏离批准。
- 用户批准后进入 **P1**（`crypto.py` + `state.py` + 测试），不做页面。
- 若 §3 被驳回，执行者按退化方案改 `THREAT-MODEL §5` 与本文 §3，再回 G1。
