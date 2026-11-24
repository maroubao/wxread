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
