# docs/design — 视觉定稿包（只读参考）

本目录是 DESIGN v1.0（A 钢蓝）的**可执行参考**，不是待改稿。实现期按此逐项对照，不得改配色、字阶、间距。

| 文件 | 说明 |
|---|---|
| `tokens.css` | 唯一令牌来源（CSS 变量 + `.num` 等宽数字 + `:focus-visible`）。组件只许引用变量，禁止颜色字面量 |
| `variant-steel.html` | 样张（A 钢蓝 #2563EB）：顶栏、指标卡、账户行与 4 种 badge、图表条、图例等真实排版 |
| `final-A-1280.png` | 桌面 1280 定稿截图 |
| `final-A-390.png` | 移动 390 定稿截图 |

## 使用方式

1. 浏览器直接打开 `variant-steel.html`，DevTools 取算好的尺寸与间距，照抄到 `web/`。
2. `web/assets/style.css` 开头 `@import` 或内联 `tokens.css` 内容（Pages 下同源，二者皆可；内联可少一次请求，推荐内联）。
3. 新增组件时先在 `tokens.css` 里找到对应变量；找不到就说明该样式不该存在（DESIGN §10）。

## 常用令牌速查（与 `tokens.css` 一一对应）

- 中性：`--bg` `--surface` `--surface-2` `--border` `--border-strong` `--text` `--text-2` `--text-3`
- 主色：`--accent`(#2563EB) `--accent-hover` `--accent-weak` `--accent-text`
- 语义：`--ok-text/--ok-bg`、`--warn-text/--warn-bg/--warn-border`、`--danger-text/--danger-bg/--danger-solid`
- 图表：`--chart-1` `--chart-2` `--chart-grid` `--chart-empty`
- 字号：`--fs-display`(28) `--fs-metric`(30) `--fs-h2`(15) `--fs-body`(13) `--fs-sm`(12) `--fs-xs`(11)
- 间距：`--sp-1..--sp-8` = 4/8/12/16/24/32/40/56
- 圆角：`--r-sm`(6) `--r-md`(10) `--r-lg`(14) `--r-full`
- 版式：`--content-max`(1180) `--rail-w`(320) `--dur`(160ms，`prefers-reduced-motion` 下为 0)
- 阴影：`--shadow-1` `--shadow-2`

说明：`tokens.css` 没有单独的"运行中"色板；DESIGN §5 的 `run` 状态用 `--accent` / `--accent-weak` 表达（进度、进行中），`ok|warn|bad` 对应上面三组语义色。数值类文本必须加 `.num`（等宽数字），焦点态用 `:focus-visible` 的全局规则，组件内不得再写 `outline`。

## 禁止

- 引入第二套颜色（包括"更好看的蓝"）。
- 用行内 `style="color:#..."` 绕开令牌。
- 改 `tokens.css` 里的值来适配组件；要改就回 DESIGN 走版本。
