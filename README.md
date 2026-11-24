# wxread · 微信读书自动阅读

GitHub Actions 每天北京时间 **01:07** 阅读，每 6 小时续期；GitHub Pages 提供控制台。首次配置并扫码后可以关闭浏览器和本机，阅读结束通过飞书发送每日摘要。

- **无需 cURL**：微信扫码确认后自动进入正文并获取已验证的阅读参数；已有登录态支持自动重新取参。
- **每日飞书摘要**：成功和未完成都汇总，多账户分片结束后发送一次；支持机器人签名。邮件、PushPlus、Telegram 可作异常提醒。
- **可用控制台**：桌面总览/账户/记录/右栏，手机卡片；扫码、编辑、停用、运行、停止、四渠道配置均在页面操作。
- **多账户和加密状态**：独立 wxread-state 分支，CAS 保留并发编辑，记录幂等、异常隔离、进度保存。

默认 40 次成功请求按 30 秒折算为目标 20 分钟；云端执行需要约 20 分钟加启动和网络时间。微信读书要求重新确认登录时仍需手机扫码，GitHub 调度可能延迟。

## 开始使用

完整的自助说明是 **[使用手册](docs/RUNBOOK.md)**，按「GitHub 配置 → 扫码绑定 → 飞书测试 → 阅读验收」顺序操作，包含页面所有功能、报错处理、升级和回滚。

已经部署的用户升级代码时保留账户、状态分支和原密钥，按 **[维护手册](docs/MAINTENANCE.md)** 操作，不重新生成密钥或初始化账户。

本仓库站点：[阅读控制台](https://maroubao.github.io/wxread/)。Fork 后用自己 Settings → Pages 中的地址，并连接自己的 owner/repo。

## 给维护者

先读 **[AGENTS.md](AGENTS.md)** 与 [文档索引](docs/README.md)。当前架构见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)，页面依据见 [DESIGN.md](docs/DESIGN.md)。原始 PLAN 和旧阶段评审保留为历史来源，当前三个核心要求及使用手册优先。

~~~powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python -m pyflakes wxread scripts tests conftest.py
python scripts/scan_secrets.py
node --test tests/js/*.test.mjs
git diff --check
~~~

具体环境、完整检查、真实浏览器/微信/GitHub 验收、依赖升级和回滚见维护手册。验证范围与本轮修复记录见 [G2-repair.md](docs/reviews/G2-repair.md)；替身测试不能代替真实外部服务验收。

## 目录

| 路径 | 用途 |
| --- | --- |
| wxread/ | 协议、账户、加密状态、控制、作业、通知与 worker |
| web/ | 原生 HTML/CSS/ES module 控制台，无构建 |
| .github/workflows/ | read、warm、control、tests、pages |
| docs/ | 使用、维护、架构、视觉、历史方案与验收资料 |
| tests/ | Python 回归、Node 加密互通/配置/统计回归 |
| requirements*.txt | 固定运行、浏览器和开发依赖 |

只操作自己拥有的微信读书账号。项目依赖网页端接口，维护者应关注真实接口与登录流程变更。
