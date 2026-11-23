# docs — wxread v4 文档索引

> 更新于 2026-11-23。文档与代码同步：G0/G1 是设计门，G2 是实施门（P1–P6 已完成代码），G3 待真跑验收。

## 读哪个

| 我想… | 读 |
| --- | --- |
| 把系统跑起来 | [RUNBOOK.md](RUNBOOK.md) |
| 了解模块怎么切、数据存在哪 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 知道密码学和信任边界 | [THREAT-MODEL.md](THREAT-MODEL.md) |
| 改界面 | [DESIGN.md](DESIGN.md) + [design/tokens.css](design/tokens.css) |
| 看某个阶段交付了什么、偏离了什么 | [reviews/](reviews/) |
| 看最初的计划与决策来源 | [PLAN.md](PLAN.md)、[REVIEW.md](REVIEW.md) |

## 文件

| 文件 | 内容 |
| --- | --- |
| [PLAN.md](PLAN.md) | v4 总体计划（P1–P10 审计、目标架构、分期、决策）。原样收录，不再改 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 现状代码地图、目标架构、模块边界、数据模型、关键流程、失败分级、迁移回滚 |
| [THREAT-MODEL.md](THREAT-MODEL.md) | 资产 A1–A9、五层信任边界、对手 O1–O8、T-01…T-14、密码学参数（已按实现更新）、留存 |
| [DESIGN.md](DESIGN.md) | 视觉与交互规范 v1.0（A 钢蓝 #2563EB）。原样搬运（SHA256 一致） |
| [RUNBOOK.md](RUNBOOK.md) | 部署、日常、六类故障处置、回滚、G3 十二条真跑清单、本地开发命令 |
| [REVIEW.md](REVIEW.md) | G0–G3 四道门、G2 固定 12 条、记录格式、自动门禁 |
| [reviews/G0-requirements.md](reviews/G0-requirements.md) | 19 条需求 + 6 条非目标 + 4 条待裁决 |
| [reviews/G1-design.md](reviews/G1-design.md) | 已写死决策表、"无待定"自查、1 条必须批准的偏离、组件对照 |
| [reviews/G2-P1.md](reviews/G2-P1.md) | crypto / state / accounts / logsafe：交付物、验收对照、4 条偏离 |
| [reviews/G2-P2.md](reviews/G2-P2.md) | reader / jobs / worker：五类场景对照、renewal 只认新 skey、反向验证 |
| [reviews/G2-P3.md](reviews/G2-P3.md) | qr / control / notify：真机三条探测结论、扫码续码策略 |
| [reviews/G2-P4.md](reviews/G2-P4.md) | warm + read.yml / warm.yml + `schedule_gate` |
| [reviews/G2-P5.md](reviews/G2-P5.md) | 零构建控制台：a11y 自查、跨语言加密互通、3 条偏离 |
| [reviews/G2-P6.md](reviews/G2-P6.md) | tests.yml、密钥扫描规则、RUNBOOK、旧部署清理 |
| [design/](design/) | `tokens.css`（唯一令牌源）、`variant-steel.html` 样张、1280/390 定稿图 |

## 与 `wxread-plan/` 的关系

`docs/PLAN.md`、`docs/DESIGN.md`、`docs/design/tokens.css`、`docs/design/variant-steel.html` 与 `D:\Projects\deepseek-harness\wxread-plan\` 下的原件逐字节一致（SHA256 已核对）。仓库内以此处为准，不再回改 `wxread-plan/`。

## G0/G1 待裁决项的落实结果

| 原问题 | 结论 |
| --- | --- |
| `target_count` 单位 | 统一为"次"（1 次 = 30 秒），页面只显示次数，不折算分钟（避免两套口径） |
| 旧 secret `WXREAD_CURL_BASH` / `WXREAD_ACCOUNTS_JSON` | 保留读取路径用于一次性迁移（`from_legacy`），迁移成功后由人工删 Secret（见 RUNBOOK §1） |
| 页面"轮换密钥"按钮 | 不做。只留 `control.yml` + `WXREAD_KEYS` 手工流程 |
| 私有仓库 Actions 额度 | 接受：每日 1 次 read + 每 6 小时 warm = 每天 5 次运行，单次 < 2 分钟 |
| AEAD 偏离 | 已改判：实现用 `crypto_secretbox` + 明文上下文前缀（tweetnacl 无 XChaCha），THREAT-MODEL §5 已同步 |
