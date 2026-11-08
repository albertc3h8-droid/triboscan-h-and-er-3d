# UI v2 设计规范（P6/P8 收尾）

状态：2026-11-08，P6 已验收并合并 `main`（`7990b06`）；P8 响应式布局（统一缩放、125%/720p 适配）已完成并进入验收。P5 切片插值 + P6 收尾（帮助、浅色标题栏、遗留用例迁移、尺寸/rc 矩阵）保持有效。
适用范围：`desktop_app/gui/` 的 v2 外壳（`ui_layout.py` / `ui_chrome.py` / `ui_widgets.py` / `viewer.py`），以及 `tools/ui_v2/` 的视觉探针。实现代码是规范的一部分；本文记录口径、原因和验收方式，避免下一位维护者把“看起来能跑”的旧断言改回来。

## 1. 设计框与缩放

- 设计坐标固定为 **1536 × 992**。所有矩形、字号、间距先在 `gui/ui_layout.py` 里按设计像素写死，再由 `Layout` 换算到实际 client。
- **两个轴共用一个比例** `s = min(width/1536, height/992)`；`_x/_y/_d` 都用 `s`，不按 `width/1536` 与 `height/992` 各算一套。`font_scale = clamp(s × ui_scale, 0.7, 2.0)`，只影响字号。
- 多出来的宽度（`width − 1536 × s`）全部给主区：两张 3D 卡片各分一半，直方图和导出目录卡继续吃掉剩余宽度；侧栏、深度卡保持设计宽度，工具栏/状态栏右侧控件靠右锚定。深度卡内部滑条、缩略图仍用设计偏移，因此不会被拉宽。
- 交互窗最小尺寸由 `theme.window_floor(dpi_scale)` 决定：100% 为 **1280 × 720**，125% 为 **1600 × 900**，150% 为 **1920 × 1080**；再被工作区夹住。窗口不会小于 floor，缩小请求会被弹回。
- 默认窗口 = 1536 × 992 按 DPI 放大，再夹到工作区；125% / 1920 × 1080 下 client 为 **1920 × 1044**（1240 装不下），不会再出现“窗口比工作区高、顶部被裁”。
- `--offscreen` 允许小于 floor 的尺寸，仅用于布局压力测试与证据图；这不是交互窗的合法尺寸。
- 首次渲染（`_on_first_render`）和 `ConfigureEvent` 都要核对实际 client 与 `_skin_size`：不一致就按实际尺寸重建皮肤、重设两个 viewport，避免皮肤按请求尺寸画、却按实际底边对齐而裁掉顶部。
- 已验收尺寸：**1280 × 680（720p 工作区）、1280 × 720、1536 × 992（设计框）、1920 × 1044（125% / 1080p）、2304 × 1488（150%）、2560 × 1440**。

## 2. 图层

v2 的窗口固定三层，由 `viewer._setup_skin()` 建立：

| 层 | 内容 | 关键做法 |
|----|------|----------|
| layer 0 | 整窗 Pillow 皮肤 | `vtkImageMapper + vtkActor2D`，显示空间 1:1，`SetViewport(0,0,1,1)`，`SetInteractive(False)`，相机钉死；**不要**用 `vtkImageActor` 铺满视口（world bounds = 尺寸−1，最近邻会造成逐行 1 px 抖动，P2 已定） |
| layer 1 | 两个 3D 视口 | `SetLayer(1)`，viewport 由 `layout.plot_viewport(metric)` 给出 |
| layer 2 | 帮助浮层（RGBA） | `SetNumberOfLayers(3)`；`SetBackgroundAlpha(0)`、`SetPreserveColorBuffer(True)`、`SetPreserveDepthBuffer(True)`、`SetInteractive(False)`、`SetDraw(False)`；`vtkImageMapper` 按像素 alpha 与下层混合 |

- 帮助打开时，两个 plot renderer 调 `SetInteractive(False)`（避免在遮罩上拖动仍旋转 3D 视图），关闭时恢复；皮肤层和帮助层始终不可交互。
- 皮肤层相机必须保持 `ViewUp=(0,1,0)`、`Position=(w/2,h/2)`、`ParallelScale=h/2`；`t14.test_05` 防回归。
- 导出/截图前临时隐藏 `_skin_actor` 与 `_help_actor`，完成后恢复；不改变存储场、`.tsr3`、CSV。

## 3. 视觉

- 浅色主题；颜色一律取 `gui/theme.COLORS`，不要在画笔里写死新色值。
- 字体：`msyh` / `msyhbd` / `segmdl2`，由 `theme.FontBook` 缓存；`px(base) = round(base × font_scale × TYPE_BOOST)`，最小 8 px；找不到字体时回退 Pillow 默认字体并给 `notice.font_fallback`。
- 圆角 10 px（卡片/面板），帮助卡片圆角 18 px；所有间距用 `layout._d()`，不写裸像素。
- 标题栏保持 **Windows 原生浅色**。P6 探针验证过 `DwmSetWindowAttribute` 的沉浸式深色可设（attr 20/19），但设计稿缺失、浅色与 v2 更一致，用户拍板不接 DWM；产品代码中不得调用 `DwmSetWindowAttribute`。

## 4. 帮助浮层（A：居中卡片 + 遮罩）

- 入口：工具栏 `?`、**F1**；关闭：**Esc**、右上角圆形 ×、点击遮罩（卡片外）；点击卡片内部保持打开。depth 数字框正在编辑时，Esc 先取消编辑（`_on_ui_key` 的顺序）。
- 几何（设计像素）：卡片 **860 × 660**，宽高取 `min(设计值, 窗口 − 2×48)`；关闭按钮圆心 `(card.x1 − 46, card.y0 + 48)`，半径 18；遮罩 RGBA `(10, 22, 44, 118)`；卡片阴影 18 px GaussianBlur；footer 线在 `card.y1 − 70`。
- 内容：两列四组——视图操作、深度与切片、切片插值、导出；footer 左 `F1 打开/关闭　Esc 关闭`，右品牌名。不滚动；1280 × 720 为压力尺寸，正文可读，TPS 备注框最多两行。
- TPS 备注框只在“正文底部到 footer 上方至少 14 设计像素”时绘制；1280 英文刚好两行，不压 footer。
- 文案全部走 `i18n` 的 `help.*`；语言切换时若帮助打开，立即用新语言重绘（`viewer.set_language` → `_render_help`）。
- 帮助打开期间，单字母快捷键（1/2/3/r/c/a/l/e/o/x/s/k/g）全部失效，只保留 F1/Esc。
- 明确不做：滚动、富文本、搜索、上下文相关帮助、多页。

## 5. Widget 契约

- `gui/ui_widgets.build(layout)` 是唯一来源：同一个 `Widget.rect` 既用于 Pillow 绘制，也用于 `WidgetSet.hit()` 命中；不要为测试另建一套几何。
- `WidgetSet.hit()` 从后往前找（后添加的在上层）；`checked` 由 `_skin_state()` 按 `view/range/reference/interp/display/language` 组同步。
- 相邻 radio 行的可点击 rect 有 **1 px 纵向重叠**，这是有意的（行距小于文字高度，重叠保证点击连续）；测试不得做“所有 widget 两两不重叠”的旧式断言，只检查同组纵向堆叠和关键控件横向顺序。
- 工具栏、侧栏、导出卡片的 id 固定（`toolbar.* / view.* / range.* / reference.* / interp.* / export.* / depth.*`）；新增控件必须同时更新画笔、命中、i18n 和 `t14` 的期望集合。

## 6. 导出与 rc

- PNG：只含两个 3D 视图，不含 UI/帮助；`clean_render_png` 与 `_capture_export_pixels` 都先隐藏皮肤与帮助 actor，再截图，最后恢复。
- `.tsr3` / CSV：仍按 P4 口径导出真实场/原始点；切片插值只影响显示，不写回存储场。
- CLI 退出码：0 正常、1 未处理异常、2 参数错、3 导出失败、4 `--require-cache` 模块不可用、5 保存失败。P6 实测 0/2/3；5 由 `t43.test_07` 覆盖；4 属于环境异常，不伪造。
- 真窗口口径：`tools/ui_v2/p2_app_window_smoke.py` 校验 DPI、floor、client 尺寸、最小化/最大化/边框样式、缩小被弹回。

## 7. 验收与证据

### 7.1 测试

```bash
cd triboscan_build/desktop_app
.buildenv/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py'
.buildenv/Scripts/python.exe -m unittest discover -s tests/exports -p 'test_*.py'
```

P8 结果：`tests/` **217 tests OK**（P5 基线 201 + P6 `t91` 11 + P8 `t92` 5）；`tests/exports/` **73 tests OK (skipped=1)**。
P9 启动空白工作区新增 `tests/test_t93_welcome.py` 13 项，全套 **231 tests OK**；见 [07_startup_welcome.md](07_startup_welcome.md)。

### 7.2 视觉/真窗口探针

```bash
.buildenv/Scripts/python.exe -m app --data '../3d hardness plot raw data' \
  --offscreen --window-size 1536x992 --interpolation tps \
  --screenshot build/evidence/ui_v2/p6/size_1536x992.png
.buildenv/Scripts/python.exe tools/ui_v2/p2_app_window_smoke.py
.buildenv/Scripts/python.exe build/evidence/ui_v2/_probe_help_realtime.py
```

P6 证据在 `build/evidence/ui_v2/p6/`（已 gitignore）：

- `help_{zh-CN,en}_{1280x720,1536x992,1920x1240}.png`：帮助浮层。
- `size_{1280x720,1536x992,1920x1240,2304x1488,2560x1440}.png`：主界面尺寸矩阵。
- `help_realtime_open.png` / `help_realtime_closed.png`：真窗口（2304 × 1440，150% DPI）经 VTK framebuffer 回读；中心亮度 170.8 → 244.7 → 170.8（开/关）。
- `rc/rc0.log`、`rc/rc2.log`、`rc/rc3.log`：CLI rc 0/2/3。
- `window_smoke_1108.log`：DPI 144、floor/client 2304 × 1488、缩小被弹回、exit 0。
- `_p6_tests_1108.log`、`_p6_exports_1108.log`：全量回归原始输出。

真窗口的**屏幕像素**截图在本会话不可用：`PrintWindow` 与 `ImageGrab` 对 VTK OpenGL 内容均返回黑图；因此真窗口证据用 VTK `vtkWindowToImageFilter` 从渲染窗口回读，而不是截屏。这不改变“真窗口必须跑探针”的 P2 规则。

### 7.3 遗留 UI 用例迁移

v2 把旧控制带（`viewer.buttons`、`_layout_buttons`、`control_renderer`、`summary_actor`、`status_actor`）整体隐藏，但仍留在代码里供旧逻辑复用；原测试因此会“通过”，却不再验证用户看到的东西。P6 只改断言，不改业务：

| 文件 | 旧断言 | v2 断言 | 理由 |
|------|--------|---------|------|
| `tests/test_t12_regressions.py` | 控制带按钮盒子分区、summary/status 像素宽 | `toolbar.*` / `view.*` / `export.*` 的 widget rect 分区；`Skin._fit_notice()` 的省略号与宽度；notice_bar 与 status/卡片不相交 | 画笔与命中共用 widget rect；长通知现在由皮肤层裁剪 |
| `tests/test_t14_layout.py` | 900 px 时 summary 挪到 status、按钮业务集合、行距 | 参考层可见且选中、10 层 reference_selection、侧栏分组堆叠、v2 widget 业务集合 | v2 的下限是 1280 × 720，900 px 布局已不存在 |
| `tests/test_t43_i18n_and_crash.py` | `status_actor` / `language_button` / 所有 VTK 文本 actor 的字体 | `_skin_state()` 的 range/notice/status、`toolbar.lang.*` 的 checked、`FontBook` 的实际字体路径与 CJK 测量、1280 下 widget 不越界 | 可见文本由 Pillow 绘制，VTK 文本 actor 不再是用户界面 |
| `tests/test_t69_export_notice.py` | 900/1400 下 notice_actor 的 bbox 与按钮/状态 actor 不重叠 | 1280/1536 下 notice_bar 的 fitted 宽度、实际暗像素、与导出卡/深度卡/状态栏不相交 | 通知现在是皮肤层的一行位图，不是 VTK actor |

其余 UI 用例（`t49/t58/t63/t74/t79` 等）在 P2–P4 已适配，P6 未再改。

### 7.4 P8 响应式布局回归

- `tests/test_t92_responsive_layout.py`（5 项）：1920 × 1044 单一比例与主区扩展、1280 × 720 紧凑下限不越界、125%/720p 的 floor/default/clamp、1920 × 1044 下所有 widget 与 plot viewport、`ConfigureEvent` 重建到 1280 × 720。
- 视觉证据在 `build/evidence/ui_v2/p8/`：
  - `b_s_1920x1044.png`、`b_s_1280x720.png`、`b_s_1280x680.png`、`b_s_1536x992.png`；
  - `window_probe.json`：真窗口在子进程里把 `theme.system_ui_scale` 模拟为 1.25，client 实测 **1920 × 1044**，窗口尺寸在屏幕内。
- 真窗口探针：`tools/ui_v2/p8_window_probe.py`（模拟 125%）；原 `p2_app_window_smoke.py` 仍用于 150% 默认窗口/最小尺寸检查。

## 8. 已知限制（不在 P6 修）

- TPS 首次切面约 **0.43 s**（103 × 103 算子），缓存后约 9.8 ms；cubic 约 40 ms；TPS 峰顶外推约 +0.1 GPa（74.442 vs 74.371）。这是 P5 已验收的显示口径，不在本轮改插值。
- 帮助卡片不滚动；超长文案只能靠 i18n 控制长度。1280 英文正文已接近卡片容量，新增条目要先量宽度。
- 标题栏不做 DWM 深色；如未来设计改深色，需要重新出图并让用户确认。
- P6 已验收并合并 `main`（2026-11-08）；P8 在同一 `ui/design-v2` 分支上做，验收后再合并。
- 1280 × 680 是 720p 屏加任务栏后的实际 client，字号约 9 px，属于可读但偏紧的压力尺寸；再小不再支持。
- 响应式只把额外宽度给主区；侧栏与深度卡保持设计宽度，避免滑条/缩略图被拉伸。
