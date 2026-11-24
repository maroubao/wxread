# 文档索引

更新：2026-11-24。首次配置和日常操作用使用手册，后续维护用维护手册；历史计划和阶段评审保留决策来源。

| 目的 | 文档 |
| --- | --- |
| 不用逐项问助手，独立配置和使用每个页面功能 | [RUNBOOK.md](RUNBOOK.md) |
| 升级、测试、排错、备份、回滚、依赖更新、交接大肥鱼 | [MAINTENANCE.md](MAINTENANCE.md) |
| 进入仓库后的维护约束 | [AGENTS.md](../AGENTS.md) |
| 当前模块、状态、自动化、预算、通知和页面数据 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 视觉依据、无障碍与桌面/手机布局 | [DESIGN.md](DESIGN.md)、[design/](design/) |
| 加密和信任边界 | [THREAT-MODEL.md](THREAT-MODEL.md)；当前连接保存行为同时见 ARCHITECTURE |
| 本轮具体修复与真实/替身验证范围 | [reviews/G2-repair.md](reviews/G2-repair.md) |
| 独立审核、补修和正式发布结果 | [reviews/G3-release.md](reviews/G3-release.md) |
| 原始计划及旧实施门 | [PLAN.md](PLAN.md)、[REVIEW.md](REVIEW.md)、[reviews/](reviews/) |

当前三个核心要求是 GitHub 全托管、扫码自动取参无需 cURL、每日读后飞书摘要。页面目标显示分钟，底层目标仍为请求次数，1 次按 30 秒折算；默认 40 次约 20 分钟。计划由工作流固定为北京 01:07 和每 6 小时保活，页面只控制开关。

旧文档中「L0 不通知」「页面导入 cURL」「通知口令只用脚本」「1200 秒固定截止」「单次不到 2 分钟」不再描述当前实现。正常升级不重新生成密钥；没有一键全量密钥轮换，见维护手册。

docs/PLAN.md、docs/DESIGN.md 和定稿图由 wxread-plan 原件保留。运行时颜色/间距继续引用 tokens.css，新增令牌会如实记录；当前不是逐字节未修改的整包。
