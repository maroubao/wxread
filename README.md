# wxread · 微信读书自动阅读（多账户 · 加密状态 · 零服务器）

每天北京时间 **01:07** 自动读一轮，每 6 小时保活一次会话。所有 cookie 与口令以密文存放在独立的
`wxread-state` 分支，仓库里没有明文凭据。控制台是纯静态页面，不需要服务器、不需要构建。

```
read.yml   每天 01:07  ─┐
warm.yml   每 6 小时   ─┼─> worker ──> wxread-state 分支（密文）
control.yml  手动/页面 ─┘                 accounts/ settings/ runs/ responses/
```

## 特性

- **多账户**：每个微信读书账号一份 `accounts/<sha256(uid)[:12]>.enc.json`，互不影响；一个号过期只跳它自己。
- **加密状态**：`crypto_secretbox`（XSalsa20-Poly1305）+ 明文上下文前缀 `wxread-state|<owner>/<repo>|v<kv>`，跨仓库/跨版本搬运直接解密失败。
- **密文指令**：浏览器里用公钥封指令（`nacl.box`，临时密钥 + 显式 nonce），worker 解封后执行；回执用一次性 `reply_key` 加密回传。
- **四级失败处理**：L1 退避重试（5/15/45s）→ L2 强制 renewal 补救 → L3 置 `needs_login` + 通知；不会静默失败。
- **扫码登录**：不用手抄 cookie。控制台点"生成二维码"，Actions 里用 Playwright 打开微信读书登录浮层，二维码写入状态分支后页面轮询显示（每 85 秒自动换码）。
- **通知四选**：邮件（SMTP）、PushPlus、飞书 webhook、Telegram；口令写在加密的 `settings.enc.json` 里。
- **零构建前端**：`web/` 只有 HTML + CSS + ES module，vendored tweetnacl，离线可用，Lighthouse a11y ≥ 90。

## 快速开始

1. 建状态密钥并写进 Secret `WXREAD_STATE_KEY`：

   ```bash
   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
   ```

2. 跑一次 `control.yml`，`control = init`；把打印的 `private_key` 存进 Secret `WXREAD_CONTROL_SK`。
3. Settings → Pages → Source 选 **GitHub Actions**；打开部署好的页面，填仓库 / token / 状态密钥 → 连接。
4. 点"生成二维码"→ 微信扫码；再导入一次 curl 补全 `appId/ps/pc`（扫码只给 cookie）。
5. 点"立即运行"验证一次，之后交给定时任务。

完整步骤、Secrets 清单、故障处置与上线验收清单见 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**。

## 目录

| 路径 | 内容 |
| --- | --- |
| `wxread/` | 运行时：`crypto` `state` `accounts` `reader` `jobs` `worker` `qr` `control` `notify` `warm` `logsafe` |
| `web/` | 控制台（零构建）：`index.html` `app.js` `crypto.js` `gh.js` `style.css` |
| `docs/` | `ARCHITECTURE.md` `THREAT-MODEL.md` `DESIGN.md` `RUNBOOK.md` `REVIEW.md` `design/` `reviews/` |
| `tests/` | pytest（104 个）+ `tests/js/` 跨语言加密互通测试（4 个） |
| `.github/workflows/` | `read.yml` `warm.yml` `control.yml` `tests.yml` `pages.yml` |

## 本地开发

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q                 # 104 passed
node --test tests/js/crypto.test.mjs      # 4 passed
python scripts/scan_secrets.py            # 明文密钥扫描（CI 同款）
python scripts/qr_local.py --wait 300     # 本机真扫码，写 .qr-cookies.json（已 gitignore）
python scripts/control_local.py --help    # 本机下发密文指令：import curl / 通知口令
```

## 与旧版（v3）的差异

| 旧 | 新 |
| --- | --- |
| `main.py` 单文件 + `config.py` 硬编码 | `wxread/` 包，依赖方向 `worker → jobs → reader → state → crypto` |
| cookie 明文在 Secret 里，`wr_skey[:8]` 截断 | cookie 只在密文状态里，完整 `wr_skey` |
| 通知走 `push.py`（含 WxPusher / ServerChan） | 只留邮件 / PushPlus / 飞书 / Telegram |
| 每天 01:00 一次，失败了只发消息 | 01:07 + 每 6 小时保活；L1→L2→L3 分级处置 |
| Docker / 本地 crontab 也能跑 | 只支持 GitHub Actions（Dockerfile 已删除） |

## 免责

本项目只操作自己的账号，用于在阅读挑战赛中保持天数。接口是微信读书网页端的公开接口，行为等同于浏览器里手动翻页。请自行评估账号风险。
