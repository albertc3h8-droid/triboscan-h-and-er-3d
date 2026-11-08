# 启动空白工作区（P9）

状态：已实现，等待用户验收；分支 `ui/design-v2`，尚未合并 `main`。

## 1. 问题

旧启动流程在没有 `--data` / `--open-result` 时直接弹“选择原始数据文件夹”。用户如果只想打开
已经导出的 `.tsr3` 或原始点 CSV，必须先取消文件夹框；而取消会直接 `return 0` 退出程序，
没有任何入口。空白工作区把“先有数据再建视图”改成“先给入口，再按需建视图”。

## 2. 行为

- 无参数启动：显示空白欢迎层（纯 2D 位图，不构造 `TrialViewer`，不持有 Dataset/FieldGrid）。
- 两个入口：顶栏和中间卡片都有“打开数据文件夹”“打开已导出结果”。
  - 数据文件夹：`*_analysis.txt` 目录，行为与原来的 `--data` 相同。
  - 已导出结果：`.tsr3` 走缓存重开；`.csv` 走原始点模式（无插值场）。
- 选择框取消：回到同一个空白工作区，不退出；关闭窗口（X）才退出，退出码 0。
- 语言：顶栏“中文 / English”切换并记住；再打开数据后主界面沿用该语言。
- 命令行 `--data` / `--open-result` 优先，直接进入原加载流程，不显示欢迎层。
- 存储场、`.tsr3`、CSV、导出格式、`--export-to` 均未改。

## 3. 代码

| 文件 | 内容 |
|------|------|
| `gui/welcome.py` | 新增 `WelcomeWindow`：全窗 RGB 位图 + `vtkImageMapper`/`vtkActor2D`，顶栏/卡片/状态栏，按钮命中与悬停，语言切换，`show()/screenshot()/close()` |
| `gui/i18n.py` | 新增 `welcome.*` 中英文文案（标题、副标题、两个按钮、提示、状态、窗口标题） |
| `app/app.py` | `ACTION_PICK` → `ACTION_WELCOME`；默认空状态；`_show_welcome()`、`_welcome_open_data()`、`_welcome_open_result()`；加载取消/报错后回空状态；删除 `_pick_source()` |
| `build/assets/说明_README.txt` | 启动与两个入口说明 |
| `tests/test_t93_welcome.py` | 新增 13 项：绘制/按钮矩形/命中、回调 action、语言重绘、空状态 show、默认 action、关闭/取消回空状态、选择结果后进加载/视图、选择器取消 |

欢迎层和主界面共用 `theme` 调色板/字体；窗口下限、DPI、工作区夹取仍走 `theme`/`window_policy`。
按钮的文件夹图标用 Segoe MDL2 的 `E8BA`（闭合文件夹）和 `E8A7`（打开文件夹+箭头）。

## 4. 验证

- `tests/`：**232 tests OK**（18.211 s；P8 217 + t93 14 + t83 1）。
- `tests/exports/`：**73 tests OK（skipped=1）**（7.020 s）。
- 离屏：`build/evidence/ui_v2/p9/welcome_{zh-CN,en}_{1920x1044,1280x720,1280x680,2304x1488}.png`。
- 真窗口：`tools/ui_v2/p9_welcome_probe.py`，无参数启动，DPI 144、
  client **2304×1488**（= `default_window_size(1.5)`）、标题 `TriboScan3D | 未打开数据`、
  自动关闭 exit 0；报告 `build/evidence/ui_v2/p9/welcome_probe.json`。
- 真窗口（模拟 125%）：子进程把 `theme.system_ui_scale` 临时改为 1.25，请求 1920×1044；
  client 实测 **1920×1044**、窗口 1942×1100，见 `build/evidence/ui_v2/p9/welcome_probe_125.json`。
- 窗口缩放回归：欢迎层 setSize 后 `vtkImageData` 尺寸随位图更新；t93 新增 1 项，修复后重跑全量。
- 回归：`--open-result build/evidence/t9/saved/real_result.tsr3 --offscreen` rc 0，
  `build_count=0`；`--data` 路径未改。

## 5. 限制

- 欢迎层是 2D 位图，不能滚动；1280×720 已排版验证。
- 欢迎层没有“最近文件”列表；不新增设置项。
- 未在真实 125% 屏幕上人工点选两个按钮（离屏/单元测试覆盖命中与回调；真窗口覆盖尺寸/标题）。
- 主界面 P8 的响应式规则不变；帮助、导出、插值均未改。
