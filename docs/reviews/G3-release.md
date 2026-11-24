# 独立复核与发布记录

## REVIEW-01：固定 0e88c74

同模型独立子代理核对原始 PLAN/DESIGN、当前核心要求、代码 diff、CSV、手册和已有验证，结论为 gaps_found。独立重跑 202 Python、15 Node 测试通过，但额外内存/模块故障注入复现了未覆盖的竞态：

- G1：排队旧快照能覆盖重绑凭据；Cookie 不变的自动取参未停止旧 Reader，旧健康结论可覆盖新成功状态。
- G2：L2 恢复后停止保留旧错误原因；初始化 CAS 时迟到的旧 stop 能取消新 run。
- G3：扫码 dispatch 尚未返回时关闭，晚回调重新激活会话。
- G4：main/Pages 尚未发布，修复分支验收不能代替正式交付。

前三项转为代码 follow-up，发布为独立交付项；修复后继续同模型 REVIEW-02。完整本机 review log 为 .mission/20261123-wxread-repair.review.md，任务状态源为同目录 CSV。

独立代理不能访问浏览器（inventory 无可用 browser），移动截图物理分辨率低；没有独立重复真人扫码、云端写入或接收端送达，未将替身包装为真实集成。已有真实云端/本机证据见 [G2-repair.md](G2-repair.md)。

## G1–G3 补修

读取启动时刷新最新会话而保留批次目标，保活重新检查最新启用/健康状态；Cookie、headers、参数及成功会话 revision 共同保护新登录。旧 Reader 结束时识别已被替代，取消明确写 cancelled。进度按 run_id 初始化，旧 stop 不进入新运行，新 stop 仍保留。

扫码在 HTTP 提交前登记本次意图，关闭/Esc 可取消正在等待的请求；晚响应不会再次激活，取消只查对应 session 的 control。新增本机 browser_fixture 供后续维护重复验证慢响应。

八项针对性回归在0e88c74原始源码副本上为7失败/1通过，补修后全通过；其中一项确认新 run 的合法 stop 不被误清除。真实浏览器的本机替身已验证8秒延迟提交时关闭、Esc及晚响应：仅本次control取消，账户仍3个，二维码面板不恢复。此结果不冒充真实微信取消验收。

## REVIEW-02 与正式交付

同模型独立复核结论为 **vision_met**。代理重放原有竞态并另外注入真实 CAS Conflict，确认新 stop 保留；独立 210 Python、15 Node 检查通过。没有新增可执行代码缺口。代理通过只读 API 和正式资源逐字节比对确认发布，未使用 root 的结论代替这些证据。

| 项目 | 结果 / 证据 |
| --- | --- |
| 主功能合并 | [PR #1](https://github.com/maroubao/wxread/pull/1) 已合并 |
| 发布配置 | [PR #2](https://github.com/maroubao/wxread/pull/2) 已合并，明确制品元数据读取权限 |
| 验收时 main | 0b125d1d807cdacb5413fca3bc0ad126b65fce21；相对8652d7b产品代码相同，仅Pages权限补充 |
| 主分支 CI | [45402519102](https://github.com/maroubao/wxread/actions/runs/45402519102)，pytest/node全成功 |
| 最终 Pages | [45403235941](https://github.com/maroubao/wxread/actions/runs/45403235941)，deploy成功 |
| 正式页面 | [根地址](https://maroubao.github.io/wxread/)与[/web/](https://maroubao.github.io/wxread/web/)均200；v5资源与审核源码匹配 |
| 自动计划 | 加密回执确认enabled=true、北京01:07、warm_hours=6；飞书enabled=true/channels=[feishu] |
| 主分支保活 | [45404821508](https://github.com/maroubao/wxread/actions/runs/45404821508)，ok=1/failed=0 |
| 主分支自动取参 | [45406557885](https://github.com/maroubao/wxread/actions/runs/45406557885)，成功阅读响应确认，回执payload_captured=true并写回GitHub |

Pages曾遇到制品列表临时失败，最终重跑成功，失败记录保留。主分支取参此前一次未获成功阅读确认，保留原参数；保活后重试成功，未把此前失败算作通过。现有原账号密文文件仍为同一份，本机已扫码号与托管对象的派生id比对一致，没有打印凭据。

root另外实际打开正式根页面，自动转到/web/，检查连接弹窗与Esc、1280和390布局均无横向溢出，确认无cURL输入且手册链接正确；正式页面截图为未连接预览。用户连接自己的凭据后显示云端账户，日常读取无需保持页面或本机打开。

验证边界仍保留：本机真人扫码与1204秒读取、旧修复分支1235秒云端40/40分别按版本记录；本轮没有再重复完整20分钟读取，没有伪称云端真人扫码、多真人账户分片、手机实际收到消息或下次自然cron已经观察。飞书接口接受已验证，接收端提示需用户在群中自行确认。当前主功能、自动化配置、正式页面和独立使用/维护资料已交付。
