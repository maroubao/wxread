# REVIEW — 四道门与审核记录规范

> 状态：G0/G1 已通过 · G2 P1–P6 已自查（`docs/reviews/G2-P1..P6.md`）· G3 待真跑 · 2026-11-23
> 本文规定"什么时候谁审什么、什么算过、记录写在哪"。任何阶段跳门即视为流程违规，需在记录中说明原因。

---

## 1. 四道门

| 门 | 时机 | 谁审 | 通过标准 | 记录文件 |
|---|---|---|---|---|
| **G0 需求门** | 动工前 | 用户 + 执行者 | 每条需求有编号与可验证验收标准；歧义清零；非目标明确 | `docs/reviews/G0-requirements.md` |
| **G1 设计门** | 写业务代码前 | 执行者出文档，独立 reviewer（独立上下文，只看文档不看解释） | 数据结构、错误路径、威胁模型、界面令牌全部写死；无"待定" | `docs/reviews/G1-design.md` |
| **G2 实现门** | 每个 PR | 实现者自查 + 独立 reviewer（只看 diff） | 下方 12 条全过 + 测试绿 | `docs/reviews/G2-P{n}.md` |
| **G3 验收门** | 合并前 | 真跑一次 Actions | 状态分支只增预期 commit；cookie 确实变化；页面 200；通知到达 | `docs/reviews/G3-P{n}.md` |

补充规则：

- G0/G1 的结论只有三种：**通过 / 有条件通过 / 驳回**。"有条件通过"必须列出条件与复核人，条件未闭环不进入下一阶段。
- 独立 reviewer 的上下文不得包含实现者对本 PR 的解释（避免被叙事带走）。
- 用户是 G0 与 G3 的最终裁决人；执行者不得自行判定 G0 通过。

---

## 2. G2 固定清单（12 条，逐条核对）

1. 是否只做本 PR 声明的事（无顺手重构）
2. 错误路径是否都有明确类型与消息（不吞异常）
3. 是否有无上限重试 / 无超时请求
4. 日志、异常、commit message 是否泄露 cookie、token、手机号
5. 新增状态字段是否有迁移与默认值
6. 并发写是否走 CAS，冲突是否有界重试
7. 是否有对应测试；测试是否会在实现错时失败（反向验证：故意改坏一处，测试必须红）
8. 删除的代码是否真的无人引用（用 `grep` 证明）
9. 界面改动是否保留焦点、滚动位置、IME 输入
10. 是否有硬编码的时间、路径、账号、书籍 ID
11. 依赖是否新增；新增是否必要（优先标准库）
12. 回滚是否可行（`git revert` 单个 PR 不丢状态）

判定：任一条"必须修"未闭环 → 驳回；"建议"可延后但需登记。

---

## 3. 记录格式（每个门一份，文件名固定）

```markdown
# G{n}-{阶段}

- 日期 / 审核人 / 被审对象（commit 或文档版本）
- 结论：通过 | 有条件通过 | 驳回
- 问题分级：必须修（M）/ 建议（S）/ 可忽略（I）

## 问题
| # | 级别 | 位置 | 问题 | 期望 | 状态 |

## 阻断条件
- 必须修 = 0 才进入下一门
```

禁止写"再优化一下"这类无验收口径的驳回理由；必须写"重做什么、达到什么标准"。

---

## 4. 自动门禁

- `tests.yml` 红 → 不合。
- 门禁内容（P6 已落地）：`pytest`（102 个）、`node --test tests/js/crypto.test.mjs`（4 个跨语言加密互通用例）、`node --check web/*.js`、`python -m compileall -q wxread scripts tests`、明文密钥扫描（`scripts/scan_secrets.py`：`wr_skey`/`wr_vid` 赋值、`ghp_`、`github_pat_`、私钥块、Slack token，外加 `.qr-cookies.json`/`qr-*.png` 禁入）。
- 门禁不可通过注释掉用例绕过；绕过视为 G2 第 7 条不合格。

---

## 5. 已完成的轮次

- **G0/G1 轮**（2026-11-23）：只做文档，不做业务代码。产物 `docs/ARCHITECTURE.md`、`docs/THREAT-MODEL.md`、`docs/REVIEW.md`、`docs/reviews/G0-requirements.md`、`docs/reviews/G1-design.md`、`docs/DESIGN.md`、`docs/PLAN.md`、`docs/design/`。
- **G2 P1–P6 轮**（2026-11-23）：`wxread/**` 包、`web/` 控制台、5 个 workflow、`tests/`（102 pytest + 4 node）、`docs/RUNBOOK.md`；删除 `main.py`/`config.py`/`push.py`/`log_utils.py`/`Dockerfile`/`deploy.yml`。自查记录 `docs/reviews/G2-P1.md` … `G2-P6.md`。
- **G3**：未开始，清单见 `docs/RUNBOOK.md` §5（12 条）。

原 G0 待裁决项已全部落实，结论见 `docs/README.md` 末节。
