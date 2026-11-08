# TriboScan 便携版首版设计与接口清单（t1）

状态：t1 设计基线 + t2 实现 + t5 便携构建均已落地（2026-10-31）。作者：app-integrator。范围：本目录设计文档与 `desktop_app/` 产品代码。
原生基线 `triboscan_build/native_viewer_3d_trial/`、旧程序、原始数据、样本、旧测试全部只读，本任务未修改任何既有文件。
t2 实现与实测证据：[../build/evidence/T2_APP.md](../build/evidence/T2_APP.md)；测试：`tests/test_app_integration.py`。
t5 冻结产物证据：[../build/evidence/T5_PACKAGE.md](../build/evidence/T5_PACKAGE.md)；产物：`dist/TriboScan3D_Portable_Windows_x64/`（1364 文件 / 414,662,094 B，21 项冻结验证全通过）。
t8 修复证据：[../build/evidence/t8/T8_FIXES.md](../build/evidence/t8/T8_FIXES.md)；回归测试：`tests/test_t8_regressions.py`（F1 交互线程/定时器、F2 job 终态、F6 RAW 文案、F7 保留接口）。
t12 首版收尾：控制带分区（文件按钮/状态/摘要互不重叠、长提示按像素裁剪）、`--timings-json` 记录真实原生 `build_count`（fresh=1、缓存/结果重开=0）、`build/build.py` 改为只准备标准 PyInstaller 命令（不执行、不覆盖冻结 `dist/`）；证据与命令见 [01 §8](01_dependencies_and_build.md) 与 `build/evidence/t12/`。UI v2 之后旧控制带已隐藏，t12 的分区断言改为读 v2 widget rect（见 P6 规范 §7.3）。
UI v2 P6 收尾（2026-11-08）：帮助浮层（A 居中卡片）、浅色标题栏、遗留 UI 用例迁移、尺寸/rc 矩阵；设计规范与验收口径见 [05_ui_v2_design_spec.md](05_ui_v2_design_spec.md)。回归 `tests/` 212 OK、`tests/exports/` 73 OK (1 skip)；P6 已验收，2026-11-08 合并 `main`（`7990b06`）。
UI v2 P8 响应式布局（2026-11-08）：统一比例 `s=min(w/1536,h/992)`，多余宽度给主区；下限 1280×720，默认窗口夹到工作区；修复 1920×1080@125% 的顶部裁切与 x/y 错位。新增 `tests/test_t92_responsive_layout.py`；`tests/` 217 OK。设计口径见 [05_ui_v2_design_spec.md §1/§7.4](05_ui_v2_design_spec.md)，交接见根目录 `TriboScan3D_UI_v2_P8_交接.md`。
UI v2 P7 便携包（2026-11-08）：源码基线 `177a9cb`，产物 `dist/TriboScan3D_Portable_Windows_x64/`（1364 文件 / 415,007,545 B）与 `dist/TriboScan3D_Portable_Windows_x64_UIv2.zip`（144,192,664 B，SHA256 `8193ce23a52c8a2e7d3f61a87e158490182d288c42179e54decfbba3040af110`），校验/限制见 [06_portable_package_v2.md](06_portable_package_v2.md)。`dist/` 为构建产物，不入库。
UI v2 P8 便携包（2026-11-08）：响应式布局重打，目录 1364 文件 / 415,010,522 B；ZIP 144,195,452 B，SHA256 `44d27ce0ac255c70ee21d7fe374b05c5e6fbbc85f88dd4be4d518a6c7b016496`；125% 真窗口 client 1920×1044。
UI v2 P9 便携包（2026-11-08）：空白启动工作区重打，目录 1365 文件 / 415,045,727 B；ZIP 144,229,501 B，SHA256 `4b1f37dac09231e731db403b33c73c9bea72dc194a1f3e6c9103a24195adba19`；无参数启动、导出、重开 .tsr3、ZIP 解压均 rc 0。
P9 启动空白工作区（2026-11-08）：无参数启动不再强制弹原始数据文件夹框；空白欢迎层提供“打开数据文件夹”/“打开已导出结果”，选择器取消回空白、关闭窗口退出；[07_startup_welcome.md](07_startup_welcome.md)。回归 `tests/` 232 OK、`tests/exports/` 73 OK (1 skip)。P9 待验收，未合并 `main`。

## 1. 目标（本团队）

把已验收的原生 PyVista/VTK 三维查看模块做成**独立 Windows 便携程序**：

1. 单文件夹工作流：`选择数据文件夹 -> 统计 -> 计算（进度/错误）-> 原生 Er/H 双图 -> 可换另一数据组重开`。
2. 导出 A：带真实坐标的原始点 CSV/Excel（真实 XYZ、原始 Er/H、来源文件/物理行/稳定 ID）。
3. 导出 B：当前切片 PNG，图内标注**本次实际切片深度与 nm 单位**及模式。
4. 计算结果 C：计算阶段超过 5 s 时提供保存；重开时**不得再执行三维场重建**。
5. 保留未来 XY 切片、排除测点后场重建的**接口**，不实现其 UI。
6. 不改变已认可的科学口径（真实 XYZ -> Delaunay3D+probe -> 精确切片、物理 XY 比例、normal/locate/RAW、common/actual 无外推、灰参考、NN 显示尺寸、RESET、固定色标）。

## 2. 关键决策摘要

| # | 决策 | 结论 | 依据/代价 |
|---|------|------|-----------|
| D1 | GUI 技术栈 | 继续用 PyVista/VTK 原生窗口（`vtkRenderWindowInteractor`），**不引入 Qt**；文件夹选择用 ctypes 调 Win32 `SHBrowseForFolderW`，拖放用 VTK `DropFilesEvent`（可选） | 现有 2150 行 viewer 直接复用；免 Qt 依赖与打包体积。tkinter 虽在环境中但需额外 Tcl/Tk 资源，构建时排除 |
| D2 | 便携形态 | **目录式（onedir）+ 无控制台 exe**，整个文件夹自包含；不用 onefile；另附同入口的 console 启动器用于排障/验收 | VTK 运行库 244 MB，onefile 每次启动解压到 TEMP 会显著变慢且易触发杀软；onedir 启动稳定。实测冻结版 414.7 MB |
| D3 | 构建环境 | `desktop_app/.buildenv` = **复制**现有 `native_viewer/.venv`（Python 3.12.14 + VTK 9.7.1 + PyVista 0.49.2），再**离线**装 PyInstaller/openpyxl（wheel 已在 `triboscan_build/wheels/`） | 不联网、不改 `native_viewer/.venv`、不动全局 Python；见 01 文档。需 leader 征求用户批准（见 §4） |
| D4 | 计算线程 | 读入/解析/哈希/三维场全部在**后台工作线程**；GUI 主线程独占所有 VTK 渲染对象；结果以纯 numpy `FieldGrid` 交接 | VTK 渲染非线程安全；计算线程只创建线程内局部 filters，不碰 Plotter/actor/render window |
| D5 | 计时口径 | `time.perf_counter()` 分别记录 `read_s`、`compute_s`、`render_s`；**5 s 阈值只针对 `compute_s`**（读入、渲染不计） | 本机实测原始 100 文件：read 0.037 s、compute 0.084 s，说明阈值是为更慢机器/更大数据保留的能力，不是当前样本需要 |
| D6 | 结果缓存 C | 版本化 `.tsr3` 容器：ZIP(stdlib) + `manifest.json` + 固定白名单 raw 数组（float64/uint8），无 pickle、无代码、校验和；`compute_s > 5.0`（严格大于）才提供保存；命中后 `build_field` 调用次数必须为 0 | 见 03 文档；安全读取、校验与体积上限已定义 |
| D7 | 导出接口 | app 只依赖 `io_exports` 的纯函数适配器与 GUI 线程内的 PNG 截图函数；A 用真实点表，B 用真实渲染帧 + 烧录标注 + tEXt 元数据 | 见 04 文档；export-engineer 实现，app 不伪造成功 |
| D8 | 保留接口 | `slice_plane(axis='x'|'y')` 保留；排除/重算以 `recompute_with_exclusions(...)` 保留并 `raise NotImplementedError`；缓存 schema 预留 `active_point_ids_sha256` | 不实现 UI、不做批处理/HTML/账户/更新 |
| D9 | 结果与缓存位置 | 默认全部在便携文件夹内：`results/`、`cache/`、`logs/`；不写用户原始数据目录、不写注册表、不写 `%APPDATA%`（matplotlib 缓存例外，见 01 §6） | 单文件夹可整体移动/删除；数据目录只读也可用 |

## 3. 文档索引（其他成员从这里开始）

| 文档 | 内容 | 主要读者 |
|------|------|----------|
| [01_dependencies_and_build.md](01_dependencies_and_build.md) | 实测依赖盘点、构建环境与离线安装命令、便携目录布局、体积/平台条件、打包风险 | app-integrator(t5)、acceptance-reviewer |
| [02_app_architecture_interfaces.md](02_app_architecture_interfaces.md) | 目录与模块所有权、app 入口/CLI、状态机、线程所有权、进度与错误、计时、打开/重开、任务分界、测试命令 | t2/t5 实现者、验收 |
| [03_result_cache_c.md](03_result_cache_c.md) | C 计算结果的 `.tsr3` 格式、签名、校验、体积限制、失败模式、API | export-engineer、验收 |
| [04_export_interfaces.md](04_export_interfaces.md) | A 原始 CSV/Excel 与 B 切片 PNG 的字段、单位、精度、标注、"非切片"语义、API | export-engineer、验收 |
| [05_ui_v2_design_spec.md](05_ui_v2_design_spec.md) | UI v2 设计规范：设计框/缩放、三层结构、帮助浮层、widget 契约、导出/rc、测试与遗留用例迁移 | UI 维护者、验收 |
| [07_startup_welcome.md](07_startup_welcome.md) | 启动空白工作区：无数据入口、取消语义、代码/测试/限制 | UI 维护者、验收 |
| [06_portable_package_v2.md](06_portable_package_v2.md) | UI v2 便携包：产物、构建、校验矩阵、已知限制、证据位置；P8 响应式布局重打 | 交付、验收、维护 |

## 4. 构建依赖（2026-10-31 已获用户批准并执行）

已批准并已完成：`triboscan_build/desktop_app/.buildenv` 由 `native_viewer/.venv` 复制，并从 `triboscan_build/wheels/` **离线**安装：

- PyInstaller 6.17.0 + pyinstaller-hooks-contrib 2025.10 + altgraph 0.17.4 + pefile 2023.2.7 + pywin32-ctypes 0.2.3 + setuptools 80.9.0
- openpyxl 3.1.5 + et-xmlfile 2.0.0（Excel 导出）

证据（复制前后源 venv 7752 文件 sha256 全一致、`sys.prefix`/`pip` 落点、版本、原 venv 仍无 openpyxl、离线安装 exit 0）见 [../build/evidence/BUILD_ENV.md](../build/evidence/BUILD_ENV.md)。未联网下载、未改全局 Python；备选方案（新 venv + 联网装 vtk/pyvista）未使用，见 01 §3.3。t5 已在该环境构建并验证冻结便携包（见 [T5_PACKAGE.md](../build/evidence/T5_PACKAGE.md)）。

## 5. 已知风险（按影响排序）

1. ~~**PyInstaller 没有官方 pyvista hook**~~ 已解决：`collect_all('pyvista')` + 过滤 examples/trame；VTK DLL 由 spec 显式扁平化收集（见 01 §7）。
2. **体积 414.7 MB 已实测**（VTK DLL 222 MB、vtkmodules 44 MB、matplotlib/Pillow/numpy 等）；"便携"不等于"小"。
3. **GPU/驱动不可承诺**：VTK 9.7 需要 OpenGL 3.2+；未实测 Intel 核显/远程桌面/无独显机器，不写"零驱动依赖"。
4. **matplotlib 缓存目录**：本机非交互环境已实测出现 `PermissionError`（TEMP 下 matplotlib-* 清理失败）。程序必须在导入 pyvista 前设置可写 `MPLCONFIGDIR`（§01.6）。
5. 后台线程内使用 VTK 计算 filters（非渲染）需在 t2 用并发/取消测试证明稳定；若发现异常，回退为"主线程分片计算 + 定时器维持响应"，不得把渲染移出主线程。
6. 5 s 阈值在本机样本上不会触发（实测 compute 0.084 s）；必须用可控计时/注入时钟测试边界，不能用真实数据假装触发。

