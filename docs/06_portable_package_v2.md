# TriboScan3D UI v2 便携包记录（P9，2026-11-08）

状态：已构建、已校验、可交付。源码为 `main` 的公开准备提交 `4c3d82f`
（P6 基线 `177a9cb` + P8 响应式布局 + P9 空白启动工作区，含欢迎层缩放修复、定位标注收缩与邮箱移除）；本文档与 `build/assets/说明_README.txt` 随 P9 提交。
不改存储场、`.tsr3`/CSV 格式。

## 1. 产物

```text
dist/TriboScan3D_Portable_Windows_x64/
  TriboScan3D.exe
  TriboScan3D_console.exe
  _internal/                # 1361 个运行时文件；VTK DLL 扁平存放，无重复 vtk.libs
  说明_README.txt           # UI v2 说明
  VERSION.txt               # 1.0.0，构建时间/主机/依赖版本
  SHA256SUMS.txt            # 1364 行
  licenses/                 # 9 份
  config/app.json
  results/ cache/ logs/     # 空目录
dist/TriboScan3D_Portable_Windows_x64_UIv2.zip
dist/TriboScan3D_Portable_Windows_x64_UIv2.zip.sha256
```

体积：目录 **1365 文件 / 415,045,727 B（0.39 GiB）**；ZIP **144,229,501 B（138 MiB）**，
SHA256 `4b1f37dac09231e731db403b33c73c9bea72dc194a1f3e6c9103a24195adba19`。
`dist/` 是构建产物，不入库；仓库只提交构建资产/文档。

## 2. 构建

```bat
cd triboscan_build\desktop_app
.buildenv\Scripts\python.exe build\build.py --plan
.buildenv\Scripts\python.exe -m PyInstaller --noconfirm --clean ^
  --distpath dist_candidate --workpath build\pyinstaller_work_candidate ^
  build\triboscan.spec
.buildenv\Scripts\python.exe build\build.py --finalize dist_candidate\TriboScan3D
```

`--plan` 只写 `build/evidence/t12/build_plan.json`，不改产物；PyInstaller 输出
`dist_candidate/TriboScan3D`；`--finalize` 补 README/VERSION/licenses/config、
校验内容、按 SHA-256 删除重复 `_internal/vtk.libs`，生成 `SHA256SUMS.txt` 与
`build/evidence/t12/package_manifest_candidate.json`；随后把
`dist_candidate/TriboScan3D_Portable_Windows_x64_candidate` 移到 `dist/`。

构建环境：Python 3.12.14、VTK 9.7.1、PyVista 0.49.2、PyInstaller 6.17.0。
未联网，未改全局 Python 或 `native_viewer/.venv`。

## 3. 校验

| 项 | 结果 |
|----|------|
| `SHA256SUMS.txt` 全量 | 1364/1364 通过，0 失败（含项目 LICENSE） |
| 包内容 | 无 `tests/ docs/ build/ .buildenv/`、无 `*.py`、无 `*_analysis.txt`；`cache/results/logs` 为空 |
| 新数据离屏 | `--data` + `--offscreen --window-size 1920x1044` 与 `1280x680`，均 rc 0、`build_count=1`；导出 PNG、CSV、`.tsr3`、导出说明 |
| 重开 `.tsr3` | rc 0，`build_count=0`，不再重建场 |
| 窗口版离屏 | `TriboScan3D.exe --offscreen` rc 0 |
| 中文+空格路径 | 复制到 `移动 后 的 目录/` 后 rc 0，`cache/logs` 写在 exe 旁 |
| 真窗口（150%） | DPI 144，client 2304×1488；floor 1920×1080，缩到 1200×800 被弹回 1920×1080；exit 0 |
| 真窗口（125% 模拟） | `theme.system_ui_scale=1.25` 子进程，client 实测 1920×1044，窗口在屏幕内；见 `p8/window_probe.json` |
| ZIP | 1461 条目；解压后启动 rc 0；包含空 `cache/results/logs` 目录 |
| 源码回归 | `tests/` 232 OK（18.211 s）；`tests/exports/` 73 OK（1 skip，7.203 s） |

DPI 探针必须自己先调用 `gui.theme.enable_dpi_awareness()` 再 `GetClientRect`；
否则探针进程是 DPI-unaware，Windows 会把 2304×1488 的窗口虚拟化成 1536×992，
看起来像“窗口没放大”，实际不是产品问题。

`build/verify_package.py` 是 t5 口径，断言 xlsx 导出；UI v2 的“原始点”卡片只写 CSV
（`--export-to` 也不含 xlsx），因此未用它作为 v2 的通过条件；上表校验覆盖了
启动、导出、重开、路径与 ZIP。

## 4. 已知限制

- 未数字签名；首次运行可能出现 SmartScreen/杀毒提示。按 `说明_README.txt` 交 IT 审核。
- v2 原始点导出为 CSV（可用 Excel 打开），不再生成 xlsx。
- TPS 首次切面约 0.4 s，之后约 10 ms；峰顶比 Cubic 约高 0.1 GPa。只影响显示切片。
- 帮助卡片不滚动；1280×720 英文接近容量，1280×680 更紧，建议 1536×992 以上。
- 屏幕下限：100% 为 1280×720、125% 为 1600×900、150% 为 1920×1080；窗口会夹到工作区。
- 未实测 Intel 核显/远程桌面/虚拟机；VTK 9.7 需要 OpenGL 3.2+。

## 5. 证据位置

`build/evidence/p6_package/`（均在本机，`build/evidence/` 不入库）：
`verify_summary.json`、`package_manifest.json`、`tests_final.log`、`exports_final.log`、
`window_smoke.json`、`dpi_probe.json`、`run2/`（P7 离屏/导出/重开）、`p8_run/`（P8 1920×1044/1280×680、重开、导出）、
`moved/`（中文路径）、`zip_extract/`（ZIP 解压启动）。

P9 追加：无参数启动直接进入空白欢迎层（不弹选择框）；欢迎层可打开数据文件夹或 .tsr3/.csv，取消回空白，关闭退出。包内验证：无参数启动 rc 0；离屏 fresh 导出 PNG/CSV/说明/.tsr3，重开 .tsr3 rc 0/build_count 0；解压 ZIP 后启动 rc 0。
P9 修复：欢迎层在窗口 resize/DPI 变化后同步 `vtkImageData.SetDimensions/SetExtent`，修复黑边与平铺/拉伸；新增 t93 resize 用例，全量 232 OK。
公开准备：帮助 footer 增加 `https://github.com/albertc3h8-droid/triboscan-h-and-er-3d`；新增 MIT LICENSE、公开 README、requirements 文件。
P9 追加：locate 标注字体 16→12 px、底边距 10.5%→3%，整体下移贴近卡片下沿；t83 新增 1 项，全量 232 OK。
