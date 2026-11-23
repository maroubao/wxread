# G0 需求门 — wxread v4

- 日期：2026-11-23
- 依据：`docs/PLAN.md`（原 `wxread-plan/PLAN.md` v4）、仓库现状审计 `7341b53`
- 审核人：用户（最终裁决） / 执行者（整理）
- 结论：**待用户裁决**
- 说明：本文件把 PLAN 里散落的意图压成可验收条目。每条都必须能用"命令/页面/文件"验证，否则不允许进入实现。

---

## 1. 背景与要解决的问题

现状四件事导致"部署完还得管"：

| # | 症状 | 根因（行号） |
|---|---|---|
| 1 | 一两个月后失效，只能重抓 curl | 续期结果不落盘（`main.py:120` 写回内存字典）、`post_json` 丢弃 `Set-Cookie`（`main.py:84-93`） |
| 2 | 参数/书籍 ID 写死，换账户就报错 | `config.py:69-85`、`main.py:111`（`bookIds:["3300060341"]`） |
| 3 | 只能一个账户 | `WXREAD_CURL_BASH` 单值（`config.py:30`） |
| 4 | 配置要进 Secrets 手改，界面是黑的 | 无 UI，只有 workflow 日志 |

## 2. 需求清单（G0 编号，后续 PR 引用这些编号）

### A. 会话存活（最高优先）

| ID | 需求 | 验收标准（可验证） |
|---|---|---|
| R-01 | 每次 read 响应里的 `Set-Cookie` 必须合并进账户状态并在运行结束（含异常）时落盘 | 跑一次 read 后 `git log wxread-state` 有该账户 commit；`cookies.wr_skey` 与 `renewed_at` 同时变化 |
| R-02 | renewal 拿到的新 `wr_skey` 必须持久化，不再只活一次 | 连续两次 read，第二次开始时 renewal 次数为 0 且 `renewed_at` 为上次时间 |
| R-03 | 未登录/被踢（L3）必须单独标记该账户 `status=needs_login`，不影响其它账户 | 手工把某账户 `wr_skey` 改坏 → 该账户 `status=needs_login`，同批其它账户 `read_count == target_count` |
| R-04 | 每 6 小时 warm 只做 renewal + 落盘 | warm workflow 日志出现 renewal，无 `/web/book/read` 调用；状态分支有 commit |

### B. 多账户

| ID | 需求 | 验收标准 |
|---|---|---|
| R-05 | 支持 ≥ 2 个账户并行（串行执行，隔离状态） | `accounts/` 下有 2 份密文；一次 read 两份 `runs` 都有记录 |
| R-06 | 单账户失败不阻断其它账户 | 人为让账户 B 全部超时 → 账户 A 仍 `L0`，退出码 0 |
| R-07 | 账户增删改查可在页面完成，无需改 Secrets | 页面新增后 `accounts/` 多一份；删除后文件消失且 `runs` 保留历史 |

### C. 界面

| ID | 需求 | 验收标准 |
|---|---|---|
| R-08 | 页面可在 GitHub Pages 打开，无需构建 | `web/index.html` 直接可跑；`pages.yml` 部署成功；无 `node_modules`/打包产物 |
| R-09 | 扫码绑定账户（首选路径），cURL 导入为逃生门 | 扫码成功写入账户；扫码超时提示改用 cURL；cURL 缺 `appId` 时回执 `missing_fields` |
| R-10 | 状态可视化：账户状态、今日计数、最近运行、连接状态 | 五块信息齐全；连接状态圆点会随时间变 `--warn-*`/`--danger-*`（不恒绿） |
| R-11 | 视觉严格遵循 DESIGN（A 钢蓝 #2563EB），不重新设计 | 对照 `docs/design/variant-steel.html` 与 `final-A-1280.png` 逐项一致；组件状态齐全（DESIGN §5） |

### D. 安全与运维

| ID | 需求 | 验收标准 |
|---|---|---|
| R-12 | 仓库内只有密文；日志无明文凭据 | `git log -p wxread-state` 无 `wr_skey` 明文；日志中值只留前 2 位 |
| R-13 | 指令需授权且不可重放 | 无 PAT 无法 dispatch；同一 `session_id` 二次执行失败 |
| R-14 | 失败分级 L0–L3 与通知策略 | L2/L3 有通知、L0/L1 无；L3 文案含"重新扫码"；通知不含 cookie |
| R-15 | 依赖锁死、门禁可跑 | `requirements.txt` 全为 `==`；`tests.yml` 绿 |
| R-16 | 01:07（北京）每日一次 + 每 6 小时 warm | workflow cron 与文档一致，注释写清 UTC 换算 |

### E. 收口与清理

| ID | 需求 | 验收标准 |
|---|---|---|
| R-17 | 删除本地/Docker 部署路径 | `Dockerfile` 删除；README 无"方法二"；`workflow_dispatch` 仍可手动跑 |
| R-18 | 通知渠道只留邮件 / PushPlus / 飞书 / Telegram | `push.py`（或 `notify.py`）无 WxPusher/ServerChan 分支 |
| R-19 | 旧配置只读兼容并标注 deprecated | 旧 secret 存在时能跑，日志有 `deprecated` 提示；文档给出清理时点 |

## 3. 非目标（明确不做）

- 对外开放注册、多租户、给别人用的 SaaS。
- 手机 App / 浏览器扩展。
- 把 cookie 或密钥交给任何第三方服务器（含自建）。
- 本地/Docker/自托管 runner 部署方式。
- 修改视觉体系（配色、字阶、间距）——DESIGN 已定稿。
- 反检测、模拟真人行为等对抗性增强（PLAN 已判定不做）。

## 4. 待用户裁决（阻断 G0 通过）

1. **`target_count` 口径**：DESIGN/ARCHITECTURE 用"次"（1 次 = 30 秒），README 旧口径是"分钟"。建议统一为"次"，页面同时显示折算分钟（`次 × 0.5`）。是否同意？
2. **旧 secret 清理时点**：建议 P4 验证 3 天后在 P6 文档中列出删除步骤，但不自动删除。是否同意保留到 P6？
3. **`key_version` 轮换**：是否需要提供"手动轮换密钥"的页面按钮？还是只在文档写 `control.yml` 手工流程？（建议后者，少一条攻击面）
4. **私有仓库成本**：> 2 账户会吃掉免费额度，是否接受"低耗模式（仅 warm + 每周一次完整阅读）"作为 P6 可选项？

## 5. 判定

- 需求 19 条，全部有可执行验收标准；非目标 6 条；待裁决 4 条。
- **G0 结论等待用户**：确认 §2 清单与 §3 非目标，并对 §4 四条给出口径。任何一条改动都会回写 `ARCHITECTURE.md` 对应小节。
