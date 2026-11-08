# 02 App 架构、接口与任务分界

本文件是 t2/t3/t5 的共同契约。接口一旦被下游引用，改动须经 leader 确认（见 §10）。

## 1. 目录布局与模块所有权

所有新代码只在 `triboscan_build/desktop_app/` 内。既有 `native_viewer_3d_trial/`、`native_viewer/`、`source/`、原始数据、样本、旧测试、`.buildenv`、`native_viewer/.venv`、`.agent-teams/` 一律只读。

```text
triboscan_build/desktop_app/
  docs/                        # t1 设计（本目录；其他成员只读）
  app/                         # [app-integrator] 入口/状态机/UI 编排
    __init__.py
    __main__.py                # python -m app；exe 入口是 app.main:main
    main.py                    # argparse、环境准备(MPLCONFIGDIR)、异常兜底、创建 App
    app.py                     # App 状态机：EMPTY/LOADING/READY/ERROR；打开/重开/关闭
    actions.py                 # 用户动作：open_folder/recompute/save_result/export_*/reset
    stats.py                   # 加载前统计与网格预估（供 UI 文本）
  gui/                         # [app-integrator] 全部 VTK/PyVista 对象
    viewer.py                  # 由 trial_viewer.py 移植（只改结构，不改科学口径）
    band.py                    # 顶部控制带/按钮/文件操作组
    overlay.py                 # 进度/错误/提示 2D 文本覆盖层（TimerEvent 驱动）
    picker.py                  # ctypes SHBrowseForFolderW；拖放事件注册
  runtime/                     # [app-integrator] 无 VTK 渲染对象
    loader.py                  # 移植 trial_data_model 的读取/解析（纯 numpy/IO）
    field.py                   # 包装 field3d.build_field；调用计数计量
    jobs.py                    # ComputeJob：后台线程、事件队列、取消、计时
    events.py                  # ProgressEvent/JobResult/JobError/AppError 定义
    timings.py                 # perf_counter 计量；CACHE_THRESHOLD_S=5.0
    paths.py                   # 便携目录解析（exe 所在目录）、logs/results/cache
    logging_setup.py
  integration/                 # [app-integrator] 只做适配，不含业务实现
    protocols.py               # ExportService / ResultStore 的 Protocol 与 DTO
    export_adapter.py          # 调 io_exports；缺失时显式报"模块未就绪"
    result_adapter.py          # 调 result_store；缺失时显式报"模块未就绪"
  io_exports/                  # [export-engineer] A 原始 CSV/Excel + B PNG
  result_store/                # [export-engineer] C .tsr3 保存/载入
  tests/                       # 各所有者自己的测试
    test_runtime.py            # [app-integrator]
    test_app_offscreen.py      # [app-integrator]
    exports/                   # [export-engineer]
    review/                    # 不在此目录：验收写 desktop_app_review/
  build/                       # [app-integrator] triboscan.spec/build.py/hooks
  dist/                        # [app-integrator] 构建产物（不入库）
  .buildenv/                   # [app-integrator] 构建环境副本（不入库、不在包内）
```

边界规则：

- `gui/` 之外**任何模块**不得 import pyvista/vtk 的渲染类，也不得持有 Plotter/actor/mapper/render window 引用；`runtime/`、`io_exports/`、`result_store/` 只允许 numpy/标准库（`result_store`、`io_exports` 不得 import vtk；PNG 截图函数除外，见 04）。
- `io_exports/`、`result_store/` 的公共 API 由 export-engineer 实现，但**签名由本文档冻结**；app 只通过 `integration/` 适配。
- 验收代码只写 `triboscan_build/desktop_app_review/`，不修改本目录。

## 2. 入口与命令行契约（2026-10-31 按实现更新）

```text
TriboScan3D.exe [options]
TriboScan3D_console.exe [options]     # 同一程序，带控制台，排障用

  --data DIR               含 *_analysis.txt 的数据文件夹（一层，非递归）
  --open-result FILE.tsr3  直接打开已保存的计算结果（不需要原数据文件夹）
  --offscreen              离屏渲染（测试/无人值守）
  --window-size WxH        默认 1400x800
  --view-mode normal|locate|raw
  --range-mode common|actual
  --reference-mode auto|layers|envelope
  --depth NM               初始切片深度
  --no-cache               不读缓存（仍计算签名，便于之后保存）
  --force-recompute        忽略缓存，强制重建三维场
  --require-cache          无可用缓存即退出码 4
  --auto-close-ms N        冒烟：N 毫秒后关闭（GUI 模式）
  --timings-json PATH      写三段计时 + cache_hit/build_count（测试用；build_count
                           是原生 field 构建的真实调用计数：fresh=1、缓存/结果重开=0，
                           同时出现在 timings 块与顶层）
  --scene-report PATH      写 JSON 场景报告
  --screenshot PATH        离屏截图（产品 B 走 io_exports，不依赖此项）
  --export-to DIR          首帧就绪后立即导出 A(CSV/Excel)+B(PNG) 到 DIR
                           （验收/无人值守；不改变交互式 UI 行为）
```

- 无参数：程序打开选择文件夹对话框；取消则正常退出 0。
- 有 `--data`/`--open-result`：直接加载，加载窗口显示进度与错误。
- 退出码（实测实现）：`0` 正常；`1` 未捕获内部错误；`2` 离屏模式下的加载/输入错误；`3` 启动失败（目录不可写/导入失败）；`4` `--require-cache` 未命中。
- 单实例不强制；同一进程内只允许一个数据组（重开即替换）。
- 打包版不提供 `--debug-console` 开关，而是并列提供 `TriboScan3D_console.exe`（同一入口、console=True）。

## 3. 状态机与用户流程

```text
EMPTY --open_folder--> LOADING --ok--> READY
  ^                       |  \--error--> ERROR --open_folder--> LOADING
  |                       |                |
  +------- cancel --------+                +--open_folder--> LOADING
READY --open_folder--> LOADING（先释放旧数据集，再建新）
READY --reset--> READY（不重载数据、不重建场）
```

LOADING 内部阶段（顺序固定，进度文本用中文）：

1. `SCAN`：列出 `*_analysis.txt`（一层、排序、计数、大小）；空目录/只有 `.txt` 但无 `_analysis.txt` -> `InputError`。
2. `HASH`：逐文件 sha256 + 大小（用于 03 §5 签名）；进度按文件数/总数。
3. `CACHE_CHECK`：按 03 文档签名查找 `.tsr3`；命中 -> `CACHE_LOAD`，**跳过 4、5 两步**（不重新解析、不重建场）；未命中 -> 继续。
4. `PARSE`：逐文件读取+解析（沿用 `trial_data_model` 规则：latin-1 单字节解码、必需表头、声明点数不符报错、坏行报错）；进度按文件数/总数。
5. `FIELD`：`vtkDelaunay3D + vtkProbeFilter -> FieldGrid`（后台线程；进度无法细分时为不确定进度条 + 已用秒数）。
6. `STATS`：点/组数、XY/Z 范围、网格实际 `Nx/Ny/Nz` 与步长（缓存命中时取 manifest 值），以及 **RAW 显示间距统计**（`display_spacing`，与场构建同在 compute 阶段计时）；显示在进度文本/状态栏。
7. `APPLY`：主线程把 FieldGrid/Dataset 交给 viewer，构建 actors，首帧渲染（计入 `render_s`）。
8. `READY`：状态栏第三行显示 `read/compute/render` 三段耗时、是否来自缓存、缓存命中时明确写 `field not rebuilt`；若 `compute_s > 5.0` 且非缓存命中，才显示 `save` 按钮；保存只能由用户点击触发，程序不自动保存、不改源目录。

用户动作（READY）：`normal/locate/raw`、`common/actual`、`auto/layers/envelope`、深度滑块、`RESET`，以及第一行右侧的四个文件按钮：

| 按钮（VTK 文本，ASCII 以免缺字） | 作用 | 何时可见 |
|---|---|---|
| `open` | 选择数据文件夹（先取消旧 job，再完整替换数据集） | 始终 |
| `export` | 导出 A（CSV/Excel）+ B（当前窗口 PNG）到 `results/run_<时间戳>/` | 始终（无切片时 PNG 明确失败，不伪造） |
| `save` | Save As 一个自包含 `.tsr3`（并复制到 `cache/` 供下次同文件夹自动命中） | 仅 `compute_s > 5.0` 且非缓存命中；无慢计算时按钮隐藏 |
| `load` | 打开任意已保存 `.tsr3`（不需要原数据文件夹） | 始终 |

键盘：`1/2/3` 视图、`r` reset、`c` range、`a/l/e` 参考层、`o/x/s/k` = open/export/save/load。

控制带分区（t12）：第一行右侧 `open/export/save/load` 是独立文件操作组（中心 x
0.650/0.735/0.820/0.905，按钮框宽 = 文本宽 + 14 px，避免选中加粗后相碰）；第二行
`RANGE/common/actual`、`GREY`、`auto/layers/envelope` 与摘要文字互不重叠，摘要只显示
参考层解析结果（`auto: 10 layers (N=10)`，900 宽为 `auto 10L`），从 x=0.632 起并被
裁剪到 x=0.862，绝不进入 `load` 按钮列。状态栏每行按像素宽度裁剪（x 0.012..0.990），
导出完成提示只写短目录名，完整路径始终在 `logs/` 中；900 与 1400 宽均可用。3D 轴刻度
在斜视角下互压属于已知限制，不在本版处理。

## 4. 后台计算与 VTK 线程所有权（强制，2026-10-31 修正）

| 资源/操作 | 允许的线程 | 说明 |
|-----------|------------|------|
| `import vtk/pyvista` | 主线程（进程启动时） | 在创建窗口前完成；工作线程不再首次初始化 VTK 全局状态 |
| `pv.Plotter`、render window、interactor、actors、mappers、lookups、2D 文本、`render()`、`screenshot()` | **仅主线程** | 任何渲染或渲染管线对象的增删/修改都必须在主线程 |
| 文件扫描/读取/解析/sha256、`result_store` 读写 | 工作线程 | 纯 IO + numpy/zipfile |
| `vtkDelaunay3D`/`vtkProbeFilter`、`display_spacing` 等**局部计算** filters | 工作线程 | 每个 job 内部新建局部对象；不加入任何渲染管线、不引用主线程/共享 VTK 对象；只返回 numpy/dict |
| 文件夹/文件选择对话框 | 主线程 | ctypes 模态调用；LOADING 期间不可用 |
| 关闭窗口 | 主线程 | `cancel()` 置位；**不得** `join(timeout)` 后立即并行启动新 job |

实现约束（对应 `app/jobs.py`、`app/app.py`）：

- 工作线程只通过 `JobHandle.emit()`/`queue.Queue` 投递事件与结果，**不得**调用任何 GUI 方法或渲染对象。
- 单 worker 串行：任何时刻最多 1 个 compute job。取消旧 job 后，新请求进入 `_next` 等待旧线程真正返回；旧线程返回后才启动新 job，不存在并行 job。
- 每个 job 有单调递增 `generation`；app 只接受当前尝试句柄的 outcome，旧 generation 的事件/结果一律丢弃；取消的 job 不得写缓存、不得投递 READY、不得回写任何文件。
- 关闭程序时 `JobManager.shutdown()` 置 closed、取消当前/排队 job，之后不再启动新任务。
- `FieldGrid`/`Dataset` 交给主线程后只读；viewer 只消费 `field + raw_spacing`，**永不**调用 `build_field`（viewer 构造函数会显式拒绝缺失的预计算结果）。
- 若并发测试发现 VTK 计算 filters 在子线程不稳定：回退为"主线程分片计算 + 定时器保持响应"，**不得**把渲染搬进子线程。

## 5. 进度、错误与日志

### 5.1 事件类型（`runtime/events.py`）

```python
@dataclass(frozen=True)
class ProgressEvent:
    stage: str          # scan|hash_parse|stats|cache_check|cache_load|field|apply
    done: int
    total: int          # total<=0 表示不确定进度
    message: str        # 中文，面向用户
    elapsed_s: float    # 该阶段单调时钟已用

@dataclass(frozen=True)
class JobResult:
    dataset: DatasetView      # 真实点/组/来源（不可变，见 §6.3）
    field: FieldGridView      # 纯 numpy；来自计算或缓存
    layers: LayerModesView
    timings: Timings
    cache_hit: bool
    cache_path: Path | None
    source_signature: str
    warnings: tuple[str, ...]

@dataclass(frozen=True)
class JobError:
    kind: str           # InputError|FieldError|CacheError|ExportError|InternalError
    user_message: str   # 中文，含下一步建议
    detail: str         # 技术细节，写入日志
    context: dict
```

### 5.2 错误分类与用户可见行为

| kind | 触发 | UI | 是否保留窗口 |
|------|------|----|--------------|
| InputError | 路径不存在/无 `*_analysis.txt`/表头缺失/声明点数不符/有效行为 0 | 覆盖层红字 + 日志路径 + "重新选择文件夹" | 是 |
| FieldError | 点<4/退化 extents/超过网格上限/Delaunay 无四面体 | 同上 + 建议（换范围/减少步长） | 是 |
| CacheError | 损坏/版本不兼容/参数不符/超限 | 黑色警告"缓存不可用，将重新计算：<原因>"；从不静默使用；不自动删除原缓存 | 是 |
| ExportError | 写盘失败/文件被占用/模块未实现 | 明确"未导出/未保存"，绝不显示成功 | 是 |
| InternalError | 未捕获异常 | 通用错误 + 日志；限制 traceback 只在日志 | 是 |

所有错误同时写 `logs/app-*.log`（UTF-8，含 `kind/detail/context`）。禁止把 traceback 直接弹给用户。

### 5.3 计时口径（`runtime/timings.py`）

```python
CACHE_THRESHOLD_S = 5.0   # 只比较 compute_s，严格 >

@dataclass(frozen=True)
class Timings:
    read_s: float      # scan+read+parse+sha256（不含缓存命中）
    compute_s: float   # build_field 全过程（Delaunay+probe+数组整理）；缓存命中=0.0
    render_s: float    # 主线程应用数据+首次完整 render；不含用户后续旋转
    cache_load_s: float
    total_s: float     # perf_counter 起点->READY
```

- 全部用 `time.perf_counter()`（单调）；禁止 `time.time()`。
- `>5s` 判定只针对 `compute_s`；读入/渲染再慢也不触发保存建议。
- 边界：`<5.0` 不提供；`==5.0` 不提供；`>5.0` 提供。测试通过注入 `compute_s`（`runtime.timings.set_clock_for_tests` 或直接构造 `Timings`）覆盖三者。

## 6. 打开 / 重开 / 结果重开协议

### 6.1 打开数据文件夹（正常路径，2026-10-31 修正）

```text
open_dataset(dir):
  files = scan_analysis_files(dir)                     # SCAN；空目录 -> InputError
  sig   = result_store.source_signature(files)         # HASH（工作线程）
  hit   = result_store.load(cache_dir, sig)            # CACHE_CHECK
  if hit:
      dataset, field = hit.dataset, hit.field          # 代理为 runtime 视图（runtime/views.py）
      timings.compute_s = RAW 间距统计（若 bundle 未带 raw_spacing）
      cache_hit = True; build_count = 0                 # 绝不调用 build_field
  else:
      dataset = loader.load_directory(dir)             # PARSE（不命中才解析）
      precompute.precompute(dataset)                   # FIELD + SPACING，compute_s 计时
      build_count = 1；compute_s > 5.0 时只显示 save 按钮，不自动保存
  apply(dataset, field)                                # APPLY：主线程；先替换旧数据
```

- `--require-cache` 未命中 -> 退出码 4；`--no-cache` 不读缓存但仍计算签名（供之后用户保存）。
- 缓存命中路径：`build_field` 调用次数为 0（`JobResult.build_count == 0`，测试用 monkeypatch 把 `build_field` 换成抛异常来证明）。
- 重开完全替换旧数据：旧 viewer 关闭后新建 `Plotter`，不共享旧 actors/相机/状态。
- 同一文件夹再次打开：重新扫描 + 签名；源文件变化则缓存不命中，明确提示后重算。

### 6.2 结果重开（C）的含义（2026-10-31 修正）

C 保存的是**可直接在本程序重新载入并继续查看/切片/导出的计算结果**，不是"只能拿去看的静态表"：

- **用户可见入口**：viewer 第一行 `save`（Save As）与 `load`（打开任意 `.tsr3`）；CLI `--open-result`。只靠"原文件夹自动缓存"不满足要求。
- 保存的文件**自包含**：无原数据文件夹也能打开（`result_store.load_file` 不依赖源目录）；源文件缺失时 UI 显著提示 `source_verified=False`，不伪造已验证。
- 重开后得到与保存时逐元素一致的 `valid/Er/H`、网格几何/参数与真实点来源，可继续切片/RAW/导出 A/B；相机/滑块状态不要求保存。
- **不自动保存、不暗中修改源**：保存仅由用户点击按钮或显式 CLI 触发；程序只写自己的 `cache/`、`results/`、`logs/`。
- 若源文件存在但内容变化：`load_file` 校验签名并拒绝（CacheError），提示后重算；损坏/版本不符/超限同样拒绝，不静默使用。
- 字节格式与安全限制见 [03_result_cache_c.md](03_result_cache_c.md)。

### 6.3 跨模块 DTO

为避免 `io_exports`/`result_store` 反向依赖 GUI，定义只读视图（`integration/protocols.py`）。下文的 `DatasetView` / `FieldGridView` / `LayerModesView` 在 03/04 文档中即指这些协议：

```python
class FieldGridView(Protocol):      # 对应 field3d.FieldGrid 的可读投影
    x0_nm/y0_nm/z0_nm/dx_nm/dy_nm/dz_nm: float
    nx/ny/nz: int
    valid: np.ndarray               # bool (nz,ny,nx)，只读
    er_gpa/h_gpa: np.ndarray        # float64 (nz,ny,nx)，NaN=无支持，只读
    point_count: int
    def metadata(self) -> dict: ...

class LayerModesView(Protocol):     # 对应 trial_data_model.LayerModes
    depths_nm: tuple[float, ...]; counts: tuple[int, ...]
    bin_nm: float; anchor_nm: float; rank_count: int; method: str

class DatasetView(Protocol):        # 对应 trial_data_model.Dataset 的可读投影
    source_dir: str; kind: str; files: tuple[str, ...]
    points: tuple[PointView, ...]
    groups: tuple[GroupView, ...]
    x_nm/y_nm/z_nm/er_gpa/h_gpa: np.ndarray   # float64, read-only
    source_sha256: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class PointView:
    point_id: str; group_id: str; source_file: str
    line_no: int; row_index: int
    x_nm: float; y_nm: float; z_nm: float; er_gpa: float; h_gpa: float

@dataclass(frozen=True)
class GroupView:
    group_id: str; x_nm: float; y_nm: float
    source_files: tuple[str, ...]; point_ids: tuple[str, ...]
    z_min_nm: float; z_max_nm: float


@dataclass(frozen=True)
class SliceSnapshot:                # 供 B 导出；只读、GUI 线程内构造
    metric: str                     # 'Er' | 'H' | 'both'（产品窗口用 both）
    view_mode: str                  # normal | locate | raw
    depth_nm: float | None          # 当前实际切片深度（raw 状态为 None）
    depth_kind: str                 # 'slice' | 'not_a_slice'
    range_mode: str                 # common | actual
    grid: GridView; plane: PlaneView | None
    camera: CameraView              # 两个 plot 的相机参数快照
    dataset_label: str; app_version: str
```

## 7. 导出适配器（app 侧接口，详见 04 文档）

实际实现：`integration/export_adapter.py`（不重新实现 CSV/PNG）。

```python
def export_current(dataset, plotter, snapshot, out_dir: Path,
                   app_version: str) -> ExportResult: ...
# 内部调用 io_exports：
#   export_raw_points(dataset, out_dir, app_version=...)
#   export_slice_png(plotter, snapshot, png_path)      # 仅 GUI 主线程
```

- `io_exports` 缺失/导入失败/`NotImplementedError` -> `ExportResult(ok=False, message="导出模块未就绪")`；**不生成假文件、不显示成功**。
- 目录默认 `results/run_<YYYYmmdd_HHMMSS>/`；A 与 B 写同一 run 目录；成功提示只列实际写出的文件路径。
- PNG 标注/元数据/命名/RAW 非切片语义由 t3 实现并按 04 文档验收。

## 8. 结果存储（C）的 app 侧接口（详见 03 文档）

实际实现：`integration/result_adapter.py`（懒导入 `result_store`，缺失时明确显示"未就绪"，不伪造缓存）。

```python
source_signature(files) -> str
load(cache_dir, signature) -> ResultBundle | None
save(cache_dir, bundle, reason) -> SaveResult
save_file(bundle, path) -> Path       # 用户 Save As：调 result_store.save_file(
                                      # path, bundle, overwrite=True)，见 03 §10
load_file(path) -> ResultBundle       # 用户打开自包含结果（无源目录）
to_bundle(job_result)                 # JobResult -> result_store.ResultBundle
```

- app 只在用户点击 `save` 时保存（且仅 `compute_s > 5.0` 且非缓存命中时按钮可见）；`--no-cache` 不读缓存。
- 保存后额外复制一份到 `cache/`，供同一文件夹下次打开自动命中；复制失败只记日志，不影响已保存的用户文件。
- 载入失败（CacheError）显示原因并回到选择数据；`--require-cache` 未命中退出码 4；程序从不自动删除损坏缓存。
- `result_store` 不 import VTK；缓存还原的 `DatasetView/FieldGridView` 由 `runtime/views.py` 代理成 runtime 表面（`x0`/`x_nm`/`bounds_nm`/`scalar_ranges` 等），不重建场。

## 9. 保留接口（本版不做 UI）

1. **XY 切片**：`field3d.slice_plane(field, 'x'|'y', position_nm, metric)` 已存在并被基线测试覆盖；本版 UI 只暴露 Z。`gui/viewer.set_slice_axis(axis)` 保留但调用即 `NotImplementedError('XY slice UI is reserved')`（未知 axis 先 `ValueError`）。**t8 已落地该入口并有回归测试。**
2. **排除测点/组后重建场**：

```python
# runtime/field.py（保留签名，不实现）
def recompute_with_exclusions(
    dataset: DatasetView,
    excluded_group_ids: Sequence[str] = (),
    excluded_point_ids: Sequence[str] = (),
    params: FieldParams | None = None,
) -> tuple[DatasetView, FieldGridView]:
    """按稳定 ID 排除后重建 Dataset+FieldGrid。本版 raise NotImplementedError。"""
```

   - 必须按 `point_id`（`P:<文件名>#<物理行号>`）与 `group_id`（`G:<x_nm>,<y_nm>`）稳定 ID 校验存在性；不得静默忽略未知 ID。
   - 缓存 schema 预留 `active_point_ids_sha256`（全量时为 `null`），未来实现时无需破坏格式版本。
   - **t8 已落地 `runtime/field.py::recompute_with_exclusions` 签名与 `NotImplementedError`，并有回归测试；尚未接 UI。**
3. **批量处理/HTML/账户/自动更新**：明确不实现。

## 10. 任务分界与集成时序

| 任务 | 所有者 | 范围 | 依赖本设计 |
|------|--------|------|------------|
| t2 | app-integrator | `app/ gui/ runtime/ integration/` + `tests/test_*.py`；把 trial viewer 移植为产品壳，接入导出/缓存接口（用 stub 时显式报未就绪） | §1–§9 |
| t3 | export-engineer | `io_exports/`、`result_store/`、`tests/exports/`；实现 03/04 文档的格式与 API | 03、04 |
| t4/t6 | acceptance-reviewer | `desktop_app_review/`；独立回读/便携验收 | 全部 |
| t5 | app-integrator | `build/`、`dist/`、依赖环境（批准后）、端到端演示 | 01 |

- t2 与 t3 **并行**；接口冻结在本文件 + 03/04。t2 不得替 t3 写假实现；t3 不得修改 app/gui。
- t5 才做真正集成构建；t5 须用真实 `io_exports`/`result_store` 演示 A/B/C，并在 t2 尚未完成时不得用源码运行冒充便携包（t5 自身完成 exe 后才有此结论）。
- 任何接口变更（签名、字段、路径）先经 leader 确认并更新 docs；下游不得各自发明。

## 11. 测试命令（统一用 `unittest`，无 pytest 依赖）

开发解释器（只读使用，不安装）：

```bat
cd triboscan_build\desktop_app
..\native_viewer\.venv\Scripts\python.exe -m unittest discover -s tests -t tests -v
```

构建解释器（含 openpyxl，用于 Excel/PNG 真实回读）：

```bat
.buildenv\Scripts\python.exe -m unittest discover -s tests -t tests -v
```

（`tests/` 不是包，发现命令必须用 `-t tests`；`-t .` 会报 "Start directory is not importable"。）

t8 回归（F1 交互定时器、F2 job 终态、F6 RAW 文案、F7 保留接口；离屏 + 假 interactor）：

```bat
.buildenv\Scripts\python.exe -m unittest discover -s tests -t tests -p "test_t8_regressions.py" -v
```

冒烟（离屏，两份解释器都应通过）：

```bat
.buildenv\Scripts\python.exe -m app --data "..\3d hardness plot raw data" --offscreen ^
  --scene-report tests\out\scene.json --timings-json tests\out\timings.json ^
  --screenshot tests\out\shot.png
.buildenv\Scripts\python.exe -m app --open-result tests\out\saved.tsr3 --offscreen ^
  --scene-report tests\out\scene_result.json --timings-json tests\out\timings_result.json
```

t2 必测（`tests/test_app_integration.py`，离屏）：

- 打开原始数据 -> READY；`build_count=1`；进度阶段顺序合法；空目录 -> 退出码 2。
- 保存 `.tsr3` 后用 `--open-result` 打开：成功、`build_count=0`、`cache_hit=True`；把 `runtime.field3d.build_field` 换成抛异常仍能打开。
- 用户保存后再次打开同一文件夹：`cache_hit=True`、`build_count=0`。
- `--no-cache`/`--force-recompute`：`build_count=1`。
- 单 worker：取消 + 立即请求新 job 时不并行；旧 generation 的结果被丢弃；关闭后无回写。
- 导出适配器接 `io_exports`：CSV 行数 == Excel 行数 == `point_count`，PNG 有实际深度文本（仅构建解释器跑）。

t12 回归（控制带分区、真实 build_count、标准构建入口准备；离屏）：

```bat
.buildenv\Scripts\python.exe -m unittest discover -s tests -t tests -p "test_t12_regressions.py" -v
```

t3/t6 必测见 03/04 文档。构建（t12 起 `build/build.py` 只准备、不执行打包）：

```bat
.buildenv\Scripts\python.exe build\build.py
:: 按 build/evidence/t12/build_plan.json 打印的命令，由有权限的交互式账户执行：
::   .buildenv\Scripts\python.exe -m PyInstaller --noconfirm --clean ^
::     --distpath dist_candidate --workpath build\pyinstaller_work_candidate ^
::     build\triboscan.spec > build\evidence\t12\pyinstaller_stdout.log 2>&1
.buildenv\Scripts\python.exe build\build.py --finalize dist_candidate\TriboScan3D
```

新候选写入 `dist_candidate/TriboScan3D_Portable_Windows_x64_candidate/`，不覆盖已冻结的
`dist/TriboScan3D_Portable_Windows_x64/`。若 PyInstaller 的隔离子进程被沙箱拒绝
（`PermissionError: [WinError 5]`），停止并升级，不得改用 `pyi_bootstrap` 或替换隔离实现。

## 12. 明确不做

批处理/多数据组并行、XY 切片 UI、排除测点 UI 与重算、HTML/WEB 导出、账户/许可/自动更新、安装器/注册表、单文件 onefile、修改科学算法或颜色口径、把用户数据打进包、把审核证据打进包。
