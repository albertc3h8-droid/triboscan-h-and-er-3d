# 03 计算结果缓存 C：`.tsr3` 格式与协议

C 是**可在本程序重新载入的计算结果**（field + 有效域 + 真实点来源），不是不可用/只读的导出表。重开 C 后必须能继续拖深度、切片、locate/RAW、导出 A/B，且**不得再执行三维场重建**。

实现所有者：export-engineer（`desktop_app/result_store/`）。app 只调用 02 §8 的接口。本文件是格式冻结契约，改动须 leader 确认。

## 1. 原则

1. **不可执行**：容器内无 pickle/marshal/eval/可执行代码，仅 JSON + 固定 dtype 的原始数组；读取端只按白名单解释字节。
2. **版本化**：`format` + `format_version` + `algorithm_id` + `app_version` 齐全；不兼容时明确拒绝并说明原因。
3. **自描述**：单位、网格几何/参数、有效掩码、来源文件（名/大小/sha256/点数）、算法常量、计时全部写入 manifest。
4. **可验证**：整文件摘要 + 每个数据成员 sha256 + 字节长度 + 形状；损坏即拒绝。
5. **不静默**：任何校验失败都报 `CacheError`（含原因），由 app 决定是否回退重算；绝不悄悄使用失效结果，也绝不自动删除用户缓存。
6. **阈值**：只在 `compute_s > 5.0`（严格大于，单调时钟，仅计算阶段）时**向用户提供**保存选项；用户拒绝则不写。

## 2. 文件与位置

- 扩展名：`.tsr3`；文件名 `field_<signature[:16]>.tsr3`。
- 默认目录：`<便携目录>\cache\`（单文件夹工作流，随包移动；不写用户数据目录）。
- 写入：同目录 `field_<...>.tsr3.tmp` -> 校验 -> `os.replace` 原子替换；失败清理 `.tmp`。
- 同一签名最多 1 个文件；签名变化（源数据/参数/版本）不复用旧文件。
- 缓存是**可选加速**：删除 `cache/` 不影响程序正确性。

## 3. 容器结构

ZIP（Python 标准库 `zipfile`，`ZIP_DEFLATED`；允许 `ZIP_STORED`），成员白名单**恰好**以下 12 个，无目录项、无符号链接、无额外成员：

| 成员 | dtype/格式 | 形状 / 长度 | 说明 |
|------|-----------|-------------|------|
| `manifest.json` | UTF-8 JSON（≤2 MiB） | 1 | 见 §4 |
| `valid.u8` | `uint8` | `nz*ny*nx`，C 序 | 1=三维支持域，0=NaN/无支持 |
| `er.f64` | `float64` LE | 同上 | Er(GPa)；无效处 NaN |
| `h.f64` | `float64` LE | 同上 | H(GPa)；无效处 NaN |
| `pt_x.f64` | `float64` LE | `np` | 真实测量点 X(nm) |
| `pt_y.f64` | `float64` LE | `np` | 真实测量点 Y(nm) |
| `pt_z.f64` | `float64` LE | `np` | 真实测量点 heff(nm) |
| `pt_er.f64` | `float64` LE | `np` | 原始 Er(GPa) |
| `pt_h.f64` | `float64` LE | `np` | 原始 H(GPa) |
| `pt_src.i32` | `int32` LE | `np` | 来源文件索引（指向 manifest.source.files 顺序） |
| `pt_line.i32` | `int32` LE | `np` | 源文件 1-based 物理行号（稳定 ID 用） |
| `pt_row.i32` | `int32` LE | `np` | 源文件内 0-based 数据行序号 |

`np = 真实测量点数`（全量时等于 dataset.point_count；未来排除实现时为本缓存实际使用的点数）。点顺序与 `trial_data_model.load_directory` 一致（文件名字典序、文件内行序），保证 `point_id = f'P:{files[pt_src[i]]}#{pt_line[i]}'`、`group_id = f'G:{x_nm:.6f},{y_nm:.6f}'` 可确定性重建，不需要存字符串表。

**为什么存真实点**：重开 C 后 locate/RAW/灰参考/导出 A 都需要真实点与来源；存点后缓存自包含，源文件缺失也能查看（带明确警告）。三维场仍是唯一"重建"对象，重开时不重算。

不保存：相机/按钮/滑块/窗口尺寸等 UI 状态；不保存切片数值表；不保存整网格 CSV。

## 4. `manifest.json` 字段（`format_version = 1`）

```json
{
  "format": "triboscan.field3d",
  "format_version": 1,
  "algorithm_id": "delaunay3d+probe/zslice/v1",
  "app_version": "0.1.0",
  "created_utc": "2026-10-31T08:12:34Z",
  "kind": "field_result",
  "units": {"x": "nm", "y": "nm", "z": "nm", "Er": "GPa", "H": "GPa", "valid": "0/1"},
  "scientific_method": {
    "interpolation": "VTK vtkDelaunay3D linear tetrahedralization + vtkProbeFilter onto regular volume",
    "support": "vtkValidPointMask; outside support = NaN (no nearest fill, no extrapolation)",
    "translation_only_reversible": true,
    "per_axis_normalised": false,
    "delaunay_tolerance": 1e-06,
    "delaunay_alpha": 0.0,
    "bounding_triangulation": false
  },
  "params": {
    "xy_step_nm": 500.0,
    "z_step_nm": 2.0,
    "max_axis_samples": 1024,
    "max_volume_samples": 6000000
  },
  "grid": {
    "shape": [77, 55, 55],
    "x0_nm": 0.0, "y0_nm": 0.0, "z0_nm": 50.0,
    "dx_nm": 500.0, "dy_nm": 500.0, "dz_nm": 1.97,
    "nx": 55, "ny": 55, "nz": 77,
    "order": "C",
    "bounds_nm": [0.0, 27000.0, 0.0, 27000.0, 50.0, 200.0]
  },
  "source": {
    "dataset_kind": "raw",
    "directory_hint": "…\\3d hardness plot raw data",
    "file_count": 100,
    "point_count": 1000,
    "group_count": 100,
    "files": [{"name": "3d_plot_Position 0_00000_LL_QS_LC_analysis.txt", "size": 4096, "sha256": "…"}],
    "signature_sha256": "…"
  },
  "active_point_ids_sha256": null,
  "arrays": [
    {"name": "valid", "member": "valid.u8", "dtype": "|u1", "count": 232925, "bytes": 232925, "sha256": "…"},
    {"name": "er", "member": "er.f64", "dtype": "<f8", "count": 232925, "bytes": 1863400, "sha256": "…"},
    {"name": "h", "member": "h.f64", "dtype": "<f8", "count": 232925, "bytes": 1863400, "sha256": "…"},
    {"name": "pt_x", "member": "pt_x.f64", "dtype": "<f8", "count": 1000, "bytes": 8000, "sha256": "…"}
  ],
  "timings_s": {"read": 0.037, "compute": 7.42, "render": 0.51, "cache_load": 0.0, "total": 7.97},
  "cache_reason": "compute_s>5.0",
  "app_env": {"python": "3.12.14", "vtk": "9.7.1", "pyvista": "0.49.2", "numpy": "2.5.4", "os": "Windows-11"}
}
```

注：`arrays` 必须列出全部 11 个数据成员（示例截断），一一与 ZIP 成员对应，数量/字节数/dtype 必须精确匹配。

## 5. 签名（cache key）与命中校验

`source_signature` 用于定位/校验缓存，**包含参数与算法**，避免不同步长误用：

```text
sig = sha256(
  "triboscan.field3d\nv1\n" + algorithm_id + "\n" +
  canonical_json(params) + "\n" +
  "\n".join(sorted(f"{name}\t{size}\t{sha256}" for each *_analysis.txt))
).hexdigest()
```

- `files` 按名称排序（大小写不敏感，与 loader 一致）。
- 命中流程（工作线程）：
  1. 扫描数据目录 -> 文件列表 + size；计算 sha256（读入阶段，约 22 MB 量级，远低于三维场耗时）。
  2. 组装 `sig`，找 `cache/field_<sig[:16]>.tsr3`。
  3. 依次校验：ZIP 可打开且成员白名单一致；`manifest.format/format_version/algorithm_id/params/grid` 与当前常量一致；`source.signature_sha256 == sig`；每个成员字节长度 = `count*itemsize` 且 sha256 匹配；`shape` 乘积 = `grid.nx*ny*nz`；`valid` 取值 ∈ {0,1}；`pt_src` 索引在范围内；`pt_line >= 1`；`pt_row >= 0`。
  3a. **值域校验（t9/F3 起，load 与 save 共用同一规则）**：有效域（`valid==1`）内 `er/h` 必须**全部有限**（NaN/±inf 拒绝，提示“有效域内 … 含非有限值”）；无效域（`valid==0`）内 `er/h` 必须为 NaN（保留“无支撑域=NaN”的科学语义，不变）；真实测点数组 `pt_x/pt_y/pt_z/pt_er/pt_h` 必须**全部有限**。该校验只看数值本身，不依赖哈希：一个“长度与 sha256 完全自洽”的恶意 `.tsr3` 也必须被拒绝（t9 测试用重算 manifest 元数据的样本证明拒绝原因是值域而非哈希）。save 在写盘前执行同一校验，被污染的内存结果不会被写出。
  4. 全部通过才构造 `ResultBundle`（数组设 `write=False`），返回 `cache_hit=True`。
- 任一步失败 -> `CacheError`（原因：损坏/版本/参数/来源不符/超限），app 显示原因后按用户策略回退重算；**不删除**缓存文件。
- 源文件缺失（目录里仍有缓存）：允许仅凭缓存打开并显示"源文件缺失，使用缓存结果"；此时无法验证源，`source.signature_sha256` 沿用 manifest 值并在 UI/报告标记 `source_verified=false`。若源文件存在但任一 sha256 不同：`签名不符` -> 拒绝。

## 6. 安全与体积限制

- 读取用 `zipfile.ZipFile.open()` 流式读取，**每个成员最多读 `expected_bytes + 1`**（超出即拒绝），防止 zip 炸弹；成员名必须精确等于白名单（拒绝 `..`、绝对路径、反斜杠、目录项）。
- `manifest.json` 读取上限 2 MiB；JSON 解析后只按固定 schema 取值，不执行任何字段内容。
- 压缩比保护：`file_size / max(compressed_size,1) > 1000` 且 file_size > 64 MiB 时拒绝（正常浮点数组远低于此）。
- 总解压体积上限 **512 MiB**；单文件压缩包上限 **256 MiB**；`nx*ny*nz <= 6,000,000`（与 `field3d.MAX_VOLUME_SAMPLES` 一致）。
- 写入前预估：`4*(2*V) + V + 8*5*np + 4*3*np + manifest`；超过 512 MiB 拒绝保存并告知原因（不截断、不降精度静默转 float32）。
- 数据类型固定：场用 float64（保证与内存中 `FieldGrid` **逐元素**一致，roundtrip 可精确比对）；禁止用 float32 偷减体积。
- 不允许通过缓存恢复任何可执行状态；不允许缓存内嵌路径被当作命令执行。

## 7. 阈值与边界测试（对应 t3 验收）

- `CACHE_THRESHOLD_S = 5.0`，比较 **`compute_s > 5.0`**：`<5` 不提供、`==5.0` 不提供、`>5` 提供。
- 本机原始数据 compute 实测 0.084 s，不会自然触发；t3 必须用**可控计时**测试（构造 `Timings` / 注入 clock），并记录真实数据三段耗时与真实缓存文件大小。
- 边界测试用例：
  1. `compute_s=4.999` -> 无保存入口；
  2. `compute_s=5.0` -> 无保存入口；
  3. `compute_s=5.001` -> 有保存入口；保存后重开：`build_field` 调用计数 **0**，`valid/Er/H` 逐元素相等，同深度 `slice_plane` 结果逐元素相等（NaN 位置也相等）。
  4. 损坏：翻转 `er.f64` 一字节 -> `CacheError`，不得载入；app 回退重算。
  5. 版本：`format_version=2` / `algorithm_id` 改一位 -> 拒绝，提示不兼容。
  6. 参数：把 manifest 的 `xy_step_nm` 改掉 -> 签名/参数不符 -> 拒绝。
  7. 超限：伪造 `nx*ny*nz > 6e6` 或成员长度超上限 -> 拒绝，不分配巨大内存。
- `--force-recompute`：忽略命中，重新 `build_field`；`--no-cache`：不读不写。
- 重复打开同一数据组两次（同进程内重开 / 新进程）都必须 `cache_hit=True` 且 **不调用** `build_field`。

## 8. API（冻结，由 export-engineer 实现）

```python
# desktop_app/result_store/__init__.py（导出给 app）
FIELD_FORMAT = 'triboscan.field3d'
FIELD_FORMAT_VERSION = 1
FIELD_ALGORITHM_ID = 'delaunay3d+probe/zslice/v1'

@dataclass(frozen=True)
class FieldParams:              # 与 field3d 默认参数一致
    xy_step_nm: float = 500.0
    z_step_nm: float = 2.0
    max_axis_samples: int = 1024
    max_volume_samples: int = 6_000_000

@dataclass(frozen=True)
class ResultBundle:
    dataset: DatasetView        # 含真实点/组/来源（由缓存还原；协议见 02 §6.3）
    field: FieldGridView        # 纯 numpy，只读
    timings: Timings
    source_signature: str
    source_verified: bool
    cache_path: Path | None

class CacheError(RuntimeError):
    def __init__(self, reason: str, detail: str = ''): ...

def source_signature(files: Sequence[Path], params: FieldParams = FieldParams(),
                     algorithm_id: str = FIELD_ALGORITHM_ID) -> str: ...
def load(cache_dir: Path, signature: str,
         params: FieldParams = FieldParams()) -> ResultBundle | None: ...   # 未命中返回 None
def save(cache_dir: Path, bundle: ResultBundle, *, reason: str) -> SaveResult: ...
def invalidate(cache_dir: Path, signature: str) -> None: ...

@dataclass(frozen=True)
class SaveResult:
    ok: bool; path: Path | None; bytes: int; message: str
```

- `load` 不读取/不重建三维场；只解包+校验+构造 `ResultBundle`。
- `save` 由 app 在计算完成后、用户确认时调用（工作线程）；不得阻塞 GUI。
- `result_store` 不 import vtk/pyvista；与 GUI 的交互只经 DTO。
- 内部可复用 `runtime.loader` 的规则，但不得反向要求 app 先重建场。

## 9. 版本策略

| 变更 | 处理 |
|------|------|
| 仅 app 版本升高、算法/参数/格式不变 | `format_version` 不变，可复用；manifest 记录 app_version 供追溯 |
| 三维场算法/常量/默认步长变化 | 必须提升 `algorithm_id`（如 `/v2`），旧缓存自然不命中 |
| 容器字段增删 | 提升 `format_version`；读取端对未知版本明确拒绝 |
| 未来排除测点实现 | `active_point_ids_sha256` 写入实际活动点集哈希（全量=null）；签名计算纳入该哈希；`format_version` 可保持 1（新字段可选），但新缓存不得被旧程序误用 -> 旧程序校验 `active_point_ids_sha256 is null` |

## 10. app 侧适配（2026-10-31 补充，不改变上述冻结 API）

- `integration/result_adapter.py` 懒导入 `result_store`；模块缺失时所有保存/载入入口明确返回"未就绪"，绝不伪造缓存文件。
- `result_store.save(cache_dir, bundle, reason)` 的文件名为 `field_<sig[:16]>.tsr3`。t3 已按本接口新增 `result_store.save_file(path, bundle, *, reason, overwrite)`；`integration/result_adapter.py` 优先调用它写用户选择的文件名（参数顺序 `path, bundle`，以 `SaveResult.ok/path` 判定），仅在模块缺少该 API 时才回退到"写缓存名后 `os.replace`"。
- `load_file(path)` 是"打开已保存结果"的唯一入口；无源目录时 `source_verified=False`，UI 必须显著提示。
- 缓存命中时 app 用 `runtime/views.py` 把 `DatasetView/FieldGridView` 代理成 runtime 表面；**不调用** `field3d.build_field`，`JobResult.build_count=0`。
- t3 确认 `.tsr3` 不存 RAW 显示间距统计（manifest 已冻结、未擅自扩字段）：缓存命中时 `compute_s` 仅包含一次 `display_spacing` 重算（毫秒级），不包含 Delaunay/场重建；若将来经格式升级加入该字段，app 会优先使用。
