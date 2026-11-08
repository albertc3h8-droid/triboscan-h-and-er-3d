# 04 导出接口 A（原始 CSV/Excel）与 B（切片 PNG）

实现所有者：export-engineer（`desktop_app/io_exports/`）。app 只经 02 §7 的适配器调用。签名与语义在此冻结；字段增删须 leader 确认。

## 0. 共同规则

- **只导真实测量值**：A 中每一行都来自 `DatasetView.points`（真实文件/物理行），不得写入插值值、均值、排序重排或补齐点；不得静默丢点。
- 单位在表头与 PNG 标注中显式写出（`nm`、`mm`、`GPa`）。
- 失败必须返回 `ExportResult(ok=False, ...)` 并抛/附 `ExportError`；**不得**先写半个文件再报成功。
- 写盘策略：先写 `*.tmp`，完成后 `os.replace` 原子替换；默认不覆盖已有文件（`overwrite=False` 时自动加 `_2`、`_3` 后缀或返回冲突错误，由实现二选一但必须在文档/测试中固定，建议自动加序号并报告实际路径）。
- 线程：A 可在后台线程；B 必须在 **GUI 主线程**调用（需要真实渲染窗口）。
- `io_exports` 不得 import vtk/pyvista 的渲染类（B 的 `plotter` 参数只使用其公开方法，不构造 Plotter），不得反向依赖 `gui/` 或 `app/`。

## 1. A：原始点 CSV / Excel

### 1.1 列（顺序固定，中英文表头均不允许改名）

| # | 列名 | 单位 | 来源/计算 | 精度/格式 |
|---|------|------|-----------|-----------|
| 1 | `point_id` | - | 稳定 ID `P:<source_file>#<line_no>` | 文本 |
| 2 | `group_id` | - | 稳定 ID `G:<x_nm 6位>,<y_nm 6位>` | 文本 |
| 3 | `source_file` | - | 原始 `*_analysis.txt` 文件名 | 文本 |
| 4 | `source_line` | - | 源文件 1-based 物理行号（标题行之后的数据行） | 整数 |
| 5 | `source_row` | - | 源文件内 0-based 数据行序号 | 整数 |
| 6 | `X_mm` | mm | 由 `x_nm/1e6` 还原（加载器做过 `mm*1e6`） | `%.9f` |
| 7 | `Y_mm` | mm | 由 `y_nm/1e6` 还原 | `%.9f` |
| 8 | `X_nm` | nm | 真实绝对 X（内部权威值） | `%.6f` |
| 9 | `Y_nm` | nm | 真实绝对 Y | `%.6f` |
| 10 | `heff_nm` | nm | 真实深度（原始 heff） | `%.6f` |
| 11 | `Er_GPa` | GPa | 原始 Er | `%.9g` |
| 12 | `H_GPa` | GPa | 原始 H | `%.9g` |

- 行顺序 = `load_directory` 的点顺序（文件名字典序、文件内行序）；行数必须 `== dataset.point_count`。
- 不写 `A`、`hf`、`m`、`Pmax` 等未进入当前数据模型的列（本版不扩科学字段）；如用户将来需要，另立需求。
- `X_mm/Y_mm` 与源文本的数值差应 ≤ `1e-9 mm`（浮点往返），验收按此容差核对；`X_nm` 为加载器实际使用值，验收按解析结果精确比对。

### 1.2 CSV 文件

- 文件名：`原始测量点_<dataset_slug>.csv`（slug 见 §3）；编码 **UTF-8 with BOM（utf-8-sig）**；CRLF；逗号分隔；全字段加引号由 `csv` 模块决定（实现统一 `QUOTE_MINIMAL`）。
- 数值字段用 `repr` 级别精度（`%r`/`%.17g`）无法满足上表格式时，以表中格式为准；**不得**四舍五入到整数。
- 输出同时包含 `dataset.point_count`、文件数、生成时间、版本到“导出说明”（Excel 的 sheet 或 CSV 同名 `.txt` 说明；二选一，实现固定后在测试记录）。

### 1.3 Excel 文件

- 文件名：`原始测量点_<dataset_slug>.xlsx`；用 **openpyxl**（已在构建环境离线安装计划中）。
- Sheet 1 `原始测量点`：上表 12 列；第 1 行表头加粗/底色/冻结；`source_line/source_row` 为整数；数值列写为 float，数字格式 `0.000000`/`0.000000000`，**不得写成公式**。
- Sheet 2 `来源文件`：`source_file`、`bytes`、`sha256`、`point_count`（每文件实际导出点数）；与 manifest 来源一致。
  - `bytes` 必须来自**已保留的来源记录**：优先 `DatasetView.source_sizes`（`.tsr3` manifest 中的
    `source.files[].size`，使“原数据目录已删除后重开结果”仍能导出正确字节数）；仅当数据是刚加载、
    没有 size 表时才回退读取 `source_dir/<name>` 的实际大小。两者都不可得时**留空并在“导出信息”
    写明缺失文件数**（不编造数字、不因原目录不在就静默写空而不解释）。
  - 该规则由 t9/F4 补充（t6 发现 `bytes` 列全空）：100 个来源文件的 Excel 回读逐行核对。
- Sheet 3 `导出信息`：app 版本、VTK/PyVista 版本、数据集目录、数据类别（raw/synthetic/transformed/mixed）、导出 UTC 时间、总点数/组数、单位说明、生成程序名。
- **公式注入防护**：任何文本单元格（`point_id`/`group_id`/`source_file` 等）若以 `= + - @` 或制表符/回车开头，写入前加前缀 `'`（Excel 文本），并保留原值到同 sheet 的隐藏列或“导出信息”说明；不得让 Excel 当成公式。CSV 同样规则（前缀 `'`），并在说明中注明。
- 不写整网格/切片数值表（明确不做，避免体积与"插值冒充测量"）。

### 1.4 API

```python
# desktop_app/io_exports/__init__.py
def export_raw_points(
    dataset: DatasetView,
    out_dir: Path,
    *,
    formats: tuple[str, ...] = ('csv', 'xlsx'),   # 子集或 ('csv',) / ('xlsx',)
    overwrite: bool = False,
    app_version: str = '',
) -> ExportResult: ...
```

- 返回 `files` 为实际写出的绝对路径；任何格式失败 -> `ok=False` 且已写文件需清理或明确列出（实现固定为“全部成功才替换，失败清理 .tmp”）。

## 2. B：当前切片 PNG

### 2.1 必须满足

1. **真实渲染状态**：用当前 `Plotter` 的 render window 实际渲染结果截图（保持当前相机/缩放/旋转/深度/颜色/模式），不得重设相机或重建数据。
2. **图内标注**（烧录进像素，不能只靠文件名）：
   - 采用**预留标注带**（t9/F5 起，替代早期“在窗口内叠加 VTK 临时文本”的做法）：先用
     `Plotter.screenshot()` 取当前 render window 的原始像素，再把这段像素**原样**贴入一张更高的
     画布，上方为 header 带、下方为 footer 带，用 Pillow 在带内绘制文字。
   - header 带至少包含：`TriboScan3D`、数据集标签、`view=<normal|locate|raw>`、`range=<common|actual>`、
     面板度量（`Er (GPa)` / `H (GPa)`，`metric='both'` 时为 `panels: Er (GPa) + H (GPa)`）；
     footer 带包含 `slice depth = <值> nm` 或 RAW 说明 + 生成 UTC 时间 + 单位 `nm / GPa`。
   - 深度用**当前实际切片深度**（`SliceSnapshot.depth_nm`，即用户滑块/命令行设置并实际用于切片的位置），
     不是显示缩放后的 Z、不是网格节点近似值。
   - 带高/画布宽由实测文字包围盒计算（文字超宽则加宽画布），**每行文字完整落在带内、不触画布边缘**，
     因此不可能遮挡查看器标题/按钮/色标/数据像素；导出**不添加任何临时 VTK actor**，用户视图
     与 actor 列表在导出前后完全一致（取代“移除临时 actor”的恢复语义）。
   - 像素比对策略（验收用）：`export` 后把标注带裁剪掉，剩余区域必须与导出前
     `plotter.screenshot()` 逐字节一致——即差异只可能出现在两条预留带内。
   - 依赖：坑位文字用 Pillow（随程序环境由 matplotlib 附带）绘制；缺 Pillow 时返回
     `ok=False`（不生成半成品），不做字体安装或额外下载。
   - 该策略不擦除、不覆盖查看器自身已有的文字（例如查看器条带里的 `heff = …`）；查看器是否
     在 RAW 下显示单深度数字是 GUI 侧的独立缺陷（t6 F6），由 `gui/` 所有者修复，导出模块只负责
     自己的标注带语义。
3. **PNG 元数据（tEXt）**：`Software`、`AppVersion`、`Dataset`、`ViewMode`、`RangeMode`、`Depth_nm`、`DepthKind`、`Units`(=nm/GPa)、`CreatedUTC`、`SliceMethod`。以标准 PNG `tEXt` 块写入（不要求外部工具可读，但可用 Python 标准库解析）。写入不得改变像素数据（仅插入块，不重编码像素）。
4. **文件名**：`TRIBOSCAN_<dataset_slug>_<view>_<metric>_z<depth>nm_<UTC时间戳>.png`（深度保留 3 位小数，如 `z123.456nm`）；`raw` 模式用 `_raw_alldepths_` 代替深度段。slug 规则见 §3。
5. **模式语义（关键，防误标）**：
   - `normal` / `locate`：切片可见，`depth_kind="slice"`，图内明确 `slice depth = X nm`。
   - `raw`：显示全部真实深度点集，**不是单深度切片**：`depth_kind="not_a_slice"`，图内写 `RAW = all measured depths (not a single-depth slice)`，不得出现可被误读为切片深度的数字；`Depth_nm` 元数据写 `""`。
   - 若当前深度为空（无有效范围）：B 导出返回 `ExportError("no active slice")`，不生成图。
6. **线程/异常**：仅在 GUI 主线程调用；内部 `try/finally` 移除临时标注 actor 并恢复渲染；截图失败返回 `ok=False`，不留下半成品。

### 2.2 API

```python
# desktop_app/io_exports/__init__.py
def export_slice_png(
    plotter,                       # pyvista.Plotter，GUI 线程
    snapshot: SliceSnapshot,       # 02 §6.3；含 depth_nm/depth_kind/metric/camera/dataset_label
    out_path: Path,
    *,
    overwrite: bool = False,
) -> ExportResult: ...
```

实现可拆为 `build_png_info(snapshot)`、`burn_in_texts(snapshot)` / `annotation_texts(snapshot)`、
`compose_annotation(shot_png, snapshot, out_png)`、`write_png_metadata(path, info)`，便于无 GUI
单测覆盖标注文本、元数据与带布局（用离屏 Plotter + PIL 像素断言；t9/F5 的 `add_burnin` 已移除）。

## 3. `dataset_slug` 规则

- 输入：`Path(source_dir).name`；替换 Windows 非法字符 `<>:"/\|?*` 与控制字符为 `_`；去掉首尾空格/点；压缩连续 `_`；截断到 60 字符；为空则 `dataset`。
- 中文/空格保留（Windows 支持），但**文件名同时可用于跨机复制**；验收覆盖中文+空格数据集名。元数据里保留完整目录路径。

## 4. 验收测试（t3 自测 + t6 复核）

A：

1. 导出一份原始数据：CSV 行数 == Excel sheet1 行数 == `dataset.point_count`；逐行核对 `point_id/source_file/source_line/X_nm/Y_nm/heff_nm/Er/H` 与 `DatasetView` 精确相等（浮点按 repr 比对或 `==`；`X_mm` 按 1e-9 容差）。
2. 打乱/重复文件名、含空格/中文目录：slug 与口径正确，无覆盖。
3. 公式注入样例（`=1+1`、`@cmd`、`+A1` 开头的文件名/ID）在 xlsx/csv 中均为文本，Excel 打开不计算。
4. 失败路径：目标只读/被占用/磁盘空间不足（可模拟异常）-> `ok=False`，无成功提示、无残留 `.tmp`。

B：

1. 离屏/实窗截图，PNG 可解析；`Depth_nm` 与当前滑块/命令一致（如 123.456）；图内文字含该值 + `nm`。
2. 改深度再导出：PNG 不同且标注同步；相机不变（同相机导出像素除标注区外一致或按实现说明）。
3. `raw` 模式导出：无“slice depth”数字，含“not a single-depth slice”字样，元数据 `DepthKind=not_a_slice`。
4. `locate` 模式：仍含切片深度与 locate 标记；导出不改变 hover/标记状态。
5. 临时标注 actor 在导出后全部移除（scene report actor 数恢复），重复导出结果稳定。
6. 像素：截图内容确实来自当前 render window（与 `plotter.screenshot()` 基线比对，允许仅标注区域差异，实现需说明比对策略）。

C：见 [03_result_cache_c.md](03_result_cache_c.md) §7。

## 5. 与 app 的集成约定

- app 在 READY 状态构造 `SliceSnapshot`（GUI 线程、只读副本）并调用 B；A 的 `DatasetView` 由 `JobResult.dataset` 直接投影。
- 导出结果目录默认 `results/run_<时间戳>_<dataset_slug>/`；A 与 B 可写同一 run 目录；UI 成功只显示实际 `files` 路径。
- 模块未实现/导入失败：`integration/export_adapter.py` 返回 `ok=False, message="导出模块未就绪"`；**不得**用临时自写 CSV 冒充实现。

## 6. app 侧接线（2026-10-31 补充）

- `gui/viewer.py::build_snapshot(metric='both')` 生成 `integration.protocols.SliceSnapshot`；`metric` 可为 `'Er'`、`'H'` 或 `'both'`，t3 的 `export_slice_png` 已接受 `'both'`（Er+H 双面板一张 PNG，文件名段 `_both_`，图内标注 `panels: Er (GPa) + H (GPa)`）。
- app 一次 `export` 同时调用 A（`export_raw_points`）与 B（`export_slice_png`），目录为 `results/run_<时间戳>/`；适配器不使用临时自写 CSV 兜底。PNG 文件名优先调用 `io_exports.png_filename(snapshot)`，与 t3 测试共用同一命名规则（含 `_both_`、`z<depth>nm`、UTC 段）；仅当模块未提供该函数时才用 app 本地回退命名。
- 快照含 `depth_nm/actual depth`、`depth_kind`、`view_mode`、`range_mode`、网格参数、相机与来源签名；`raw` 模式必须 `depth_kind='not_a_slice'` 且 `depth_nm=None`。
