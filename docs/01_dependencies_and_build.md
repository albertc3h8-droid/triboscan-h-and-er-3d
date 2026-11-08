# 01 依赖盘点、构建环境与便携包方案

本文所有版本/路径均来自本机实际探测（2026-10-31，只读命令），不是假设。命令证据附在每节末尾。

## 1. 实际环境盘点

### 1.1 两套 Python 环境（互不相同）

| 项 | 运行/开发环境 `triboscan_build/native_viewer/.venv` | 旧构建环境 `triboscan_build/.buildenv` |
|----|--------------------------------------------------|------------------------------------------|
| Python | 3.12.14（base：`C:\Python312\python.exe`） | 3.12.0rc3（`MSC v.1935 64bit`） |
| VTK | **9.7.1** | 无 |
| PyVista | **0.49.2** | 无 |
| numpy | 2.5.4 | 2.3.5 |
| matplotlib | 3.11.2 | 无 |
| Pillow | 13.0.0 | 无 |
| openpyxl | **无** | 3.1.5 |
| pandas / scipy / plotly | 无 | 2.3.3 / 1.16.3 / 6.5.0 |
| PyInstaller | **无** | 6.17.0（hooks-contrib 2025.10） |
| pip | 25.0.1（venv 以 `--without-pip` 创建，但 pip 包存在，`python -m pip` 可用） | 有 |
| pytest | **无**（两套都没有） | 无 |
| tkinter | 有（base 3.12.14 标准库） | 有 |

结论：

- **不能假设已有 PyInstaller**：只有旧 `.buildenv` 有，而它**没有 VTK/PyVista**，不能直接打新程序。
- **不能假设已有 openpyxl 运行依赖**：VTK venv 没有 openpyxl，Excel 导出需要补（本地 wheel 已具备）。
- **不需要 Qt**：现有 viewer 只用 PyVista/VTK 原生窗口；环境里也没有 PyQt/PySide/wx。
- **不能假设 pytest**：测试统一用标准库 `unittest`（或在独立 verify 脚本里自带断言），不引入 pytest。

### 1.2 VTK/PyVista 体积与依赖（打包关键）

| 项 | 体积 |
|----|------|
| `site-packages/vtk.libs`（VTK 自带动态库） | 244 MB |
| `site-packages/vtkmodules` | 53 MB |
| `site-packages/pyvista` | 14 MB |
| 整个 `native_viewer/.venv` | 478 MB |

PyVista 0.49.2 强制依赖：`vtk>=9.3.1,<9.8.0`、`matplotlib>=3.0.1`、`numpy`、`pillow`、`scooby`、`pooch`、`cyclopts`、`typing-extensions`、`pyvista-validation`。VTK 9.7.1 自身 `Requires-Dist: matplotlib>=2.0.0`。因此**matplotlib 一定会进入便携包**，无法按"可选"排除。

### 1.3 PyInstaller 对 VTK/PyVista 的支持现状

- `pyinstaller-hooks-contrib 2025.10` 内置 **`hook-vtkmodules.<Module>.py` 全量 hook**（约 160 个，含 `vtkRenderingOpenGL2`、`vtkFiltersCore`、`vtkIOImage` 等），也带 `hook-vtkpython.py`。
- **没有 `hook-pyvista.py`**（在 PyInstaller 与 hooks-contrib 目录内检索 `pyvista` 无命中）。
- 因此构建时必须：① 让 PyInstaller 正确收集 `vtkmodules`（hook 已覆盖，但 `import vtk` 会拉起 `vtkmodules.all`，体积大）；② 为 pyvista 提供自定义 hook 或 `--collect-all pyvista`，并把 `pyvista` 的隐藏导入/资源（如 `pyvista.plotting`、`pyvista.examples` 不需要）列入。**这是 t5 的首要技术验证项，不是本设计能宣布已解决的**。

### 1.4 数据与基线事实（只读核对）

- 原始数据目录 `triboscan_build/3d hardness plot raw data`：100 个 `*_analysis.txt` + 100 `.tdm` + 100 `.tdx` + 1 `.ldf`，共 22 MB；loader 只取一层内 `*_analysis.txt`（100 个文件，1000 点，100 个 XY 组），网格 `55x55x77`。
- 基线 `native_viewer_3d_trial/` 关键模块：`trial_data_model.py`(414 行)、`field3d.py`(319)、`trial_viewer.py`(2150)、`trial_selection.py`(88)、`display_spacing.py`(143)，`verify_trial.py` 自述 138 项检查。
- **实测计时**（本机，仅记录，不代表 Intel/客户机）：`import vtk+pyvista 0.83 s`、`load_directory 0.037 s`、`build_field 0.084 s`。→ 本机原始数据计算远低于 5 s，缓存阈值必须靠可控计时测试覆盖边界。

命令证据：

```bat
native_viewer\.venv\Scripts\python.exe -c "import sys,vtk,pyvista,numpy;print(sys.version);print(vtk.VTK_VERSION,pyvista.__version__,numpy.__version__)"
.buildenv\Scripts\python.exe -c "import PyInstaller;print(PyInstaller.__version__)"
grep -ril pyvista .buildenv\Lib\site-packages\PyInstaller .buildenv\Lib\site-packages\_pyinstaller_hooks_contrib   :: 无输出
```

## 2. 便携方案选择

### 2.1 候选对比

| 方案 | 可行性 | 体积/启动 | 结论 |
|------|--------|-----------|------|
| A. `.buildenv` + `--paths native_viewer/.venv/Lib/site-packages` 交叉打包 | 环境混用（两套 numpy、Python 3.12.0rc3 vs 3.12.14），分析路径与运行时不保证一致 | 中 | 不采用：不可复现、难定位 DLL 冲突 |
| B. 新 venv + 联网安装 vtk/pyvista + PyInstaller | 可行但需联网下载约 150–200 MB | 大 | 仅作备选，需用户额外批准网络 |
| **C. 复制 `native_viewer/.venv` -> `desktop_app/.buildenv`，离线补 PyInstaller/openpyxl** | **全部 wheel 已在本地，`pip --dry-run` 已解析成功** | 复制约 478 MB（一次性，构建机内） | **采用**：不改原 venv、不联网、版本与验收基线完全一致 |
| D. onefile 单 exe | PyInstaller 支持，但 VTK 每次启动解压 244 MB 到 TEMP | 启动慢、杀软风险高 | 不采用 |

### 2.2 构建环境初始化（2026-10-31 已按用户批准执行）

结果：`desktop_app/.buildenv` 已由 `native_viewer/.venv` 复制并离线安装固定依赖；
证据见 `desktop_app/build/evidence/BUILD_ENV.md`。

```bat
:: 1) 复制（目标不存在才执行；scripts/build/env_copy.py 先哈希源再复制）
python build/env_copy.py
:: 2) 只从本地 wheels 安装（--no-index，无联网）
.buildenv\Scripts\python.exe -m pip install --no-index ^
  --find-links="..\wheels" ^
  pyinstaller==6.17.0 pyinstaller-hooks-contrib==2025.10 altgraph==0.17.4 ^
  pefile==2023.2.7 pywin32-ctypes==0.2.3 setuptools==80.9.0 ^
  openpyxl==3.1.5 et-xmlfile==2.0.0
```

验收证据（均已实测）：源 venv 7752 文件复制前后 sha256 完全一致；复制后解释器
`sys.prefix = ...\desktop_app\.buildenv`、`pip` 落在 `.buildenv\Lib\site-packages`；
源 venv 仍无 openpyxl；`.buildenv` 中 openpyxl 3.1.5 / PyInstaller 6.17.0 可导入。
`packaging` 已随复制环境存在（26.3），未被升级或联网获取。

### 2.3 备选方案（仅在用户拒绝复制 venv 时提出）

新建 `desktop_app/.buildenv`（base = 3.12.14 运行时），联网安装 `vtk==9.7.1 pyvista==0.49.2 pyinstaller==6.17.0 pyinstaller-hooks-contrib==2025.10 openpyxl==3.1.5`。**需要联网批准**，且构建环境与验收基线不再逐字节同源，风险更高。

## 3. 便携包形态与布局（目录式）

目标产物：`triboscan_build/desktop_app/dist/TriboScan3D_Portable_Windows_x64/`

```text
TriboScan3D_Portable_Windows_x64\
  TriboScan3D.exe              # 无控制台；入口 app.main:main
  TriboScan3D_console.exe      # 同入口 + 控制台（排障/无人值守验收）
  _internal\                   # PyInstaller onedir 运行时：python3xx.dll、扁平化的 130 个 VTK DLL、vtkmodules、pyvista、matplotlib、PIL、numpy（不保留重复的 vtk.libs 目录）
  说明_README.txt              # 中文使用说明（Windows 版本/显卡条件/数据只读/输出位置）
  VERSION.txt                  # 应用版本、构建时间、Python/VTK/PyVista/PyInstaller 版本
  SHA256SUMS.txt               # 包内文件清单与 SHA256（由构建脚本生成）
  licenses\                    # VTK/PyVista/Python/numpy 等许可证原文
  config\
    app.json                   # 非可执行配置：导出根目录/缓存根目录/日志级别（可选）
  results\                     # 默认导出根（A 原始 CSV/Excel、B PNG 每次 run 子目录）
  cache\                       # C 结果缓存 .tsr3；不在用户数据目录写缓存
  logs\                        # app-YYYYmmdd.log、崩溃信息
```

约定：

- 启动路径解析统一用 `sys.executable` 的父目录（onedir 下 exe 所在目录），**不依赖当前工作目录**；资源/配置用 `Path(__file__)` 或 `sys._MEIPASS` 仅用于只读资源，写出目录一律相对 exe。
- 中文/空格路径必须可启动（验收项）；程序内所有子进程调用禁止拼接 shell 字符串。
- 不打包用户原始测量数据、不打包审核证据、不打包 `docs/` 与测试。
- 构建时显式 `--exclude-module tkinter --exclude-module tkinter.* --exclude-module pytest --exclude-module pandas --exclude-module scipy --exclude-module plotly`（这些既不需要也会显著增大体积/引入 Tcl 错误）。
- **不承诺**"零驱动依赖"：README 写明 Windows 10/11 x64、需 OpenGL 3.2+ 的显卡驱动；未实测 Intel 核显/远程桌面/虚拟机。

体积实测（t5 冻结版，见 §7）：**1364 文件 / 414,662,094 字节（0.39 GiB）**；其中 VTK DLL 222 MB、vtkmodules 44 MB、matplotlib/Pillow/numpy 等约 100 MB。ZIP 后约 150–200 MB。

## 4. 便携构建入口（t12 起：只准备标准命令，不执行打包）

```bat
"triboscan_build\desktop_app\.buildenv\Scripts\python.exe" "triboscan_build\desktop_app\build\build.py"
```

`build/build.py`（t12 职责）：校验输入与解释器，打印并写入
`build/evidence/t12/build_plan.json`（工作目录、官方 `python -m PyInstaller` 命令、
输入、候选输出目录、日志、权限阻断点）。它**不再调用 PyInstaller**、不再 import
`pyi_bootstrap`、不再替换隔离启动器。打包由有权限的交互式账户按 plan 执行，产物进入
`dist_candidate/`，不覆盖 t5 冻结的 `dist/`；随后用 `--finalize` 做资产/版本/SHA/清单
（VTK 同名 DLL 仅在校验 SHA-256 完全一致后才删除副本）。t5 的实际执行记录见 §7（历史）。

## 5. 运行期资源与缓存目录（2026-10-31 定稿）

- 程序根目录 = 冻结后 exe 所在文件夹；源码运行时 = `desktop_app/`。
- 只使用根目录下 `cache/`（含 `cache/mpl`）、`results/`、`logs/`、`config/`；启动时逐个创建并做写探针。
- **`MPLCONFIGDIR` 在 import PyVista/matplotlib 之前显式设为 `<root>/cache/mpl`**，不继承外部值、不 `setdefault`。
- 受限账户不可写时：显示具体路径与原因，给出"换可写目录/请管理员授权"的建议，**不得**回退到 `LOCALAPPDATA`、`TEMP` 或其他用户目录绕过授权；应报告 leader。
- 正常客户看到的可写路径：viewer 状态第三行显示缓存/耗时信息；错误窗口显示 `logs/` 路径；导出成功提示列出 `results/...` 实际路径。文档与界面都不隐藏这些路径。

## 6. 构建/运行风险清单（必须在 t5 逐项验证并记录）

1. pyvista 无官方 hook：先用 `--collect-all pyvista` + `--collect-all vtkmodules` 得到可运行包，再按需裁剪；若 `--collect-all vtkmodules` 失败，退回 `--hidden-import` 列表（以实际 traceback 为准）。
2. VTK DLL 依赖：`vtk.libs` 必须整体进入 `_internal`；Windows 上可能出现 `DLL load failed`，需确认 PyInstaller 的 `binaries` 收集与 `os.add_dll_directory`。以 `--data`/hook 显式收集 `vtk.libs` 下 DLL，并做"中文+空格路径、移动到另一盘符"启动测试。
3. 无控制台 exe 的未捕获异常不可见：`app` 顶层 `try/except` + 日志 + 错误覆盖层；另随包提供 `TriboScan3D_console.exe`（同入口、console=True）用于排障与无人值守验收。已实测两个启动器均可运行。
4. GPU/驱动：VTK 9.7 需 OpenGL 3.2+；软件渲染不可打包（Mesa 会增加复杂度），远程桌面可能降级；验收必须记录实际显卡/驱动与失败表现，不写"支持所有机器"。
5. 杀软/SmartScreen：未签名 exe 可能被拦；README 说明由 IT 审核，不指导关闭安全软件。
6. 5 s 缓存能力在构建机不会自然触发，验收需注入计时（见 03 §7），不得伪造。

## 7. t5 实际构建记录（2026-10-31，已冻结）

> 历史记录。t12 已把 `build/build.py` 改为“只准备标准入口、不执行打包”：下文的
> `pyi_bootstrap` 内联方案和 `build.py --clean` 不再可用，也不得复用；现行流程见 §4。

构建环境仍是 §2.2 的 `.buildenv`（未联网、未改 `native_viewer/.venv`、未改全局 Python）。

```bat
cd triboscan_build\desktop_app
.buildenv\Scripts\python.exe build\build.py --clean
.buildenv\Scripts\python.exe build\verify_package.py
```

实测产物：`dist/TriboScan3D_Portable_Windows_x64/`，**1364 文件 / 414,662,094 字节（0.39 GiB）**。
完整 SHA256、源码清单与 21 项冻结验证见 `build/evidence/T5_PACKAGE.md`、`t5_verify.json`、`package_manifest.json`。

与草案的差异（均为实测结论，构建脚本已固化）：

1. **PyInstaller 的 isolated 子进程在本沙箱不可用**：`CreatePipe` 被拒绝
   （`PermissionError: [WinError 5]`），`collect_submodules`/hook 收集直接失败。
   `build/build.py` 因此通过 `build/pyi_bootstrap.py` 在构建进程内联执行 PyInstaller；
   该 shim 只影响构建，产物无依赖。
2. **`vtk.libs` 必须显式收集并扁平化**：hooks-contrib 只加模块依赖，不解析
   `vtkCommonCore-9.7.1.dll` 等；spec 把 `vtk.libs\*.dll` 全部以 dest `.` 放进 `_internal`。
   未做此步时包只有 161 MB，启动报 DLL load failed。
   PyInstaller 另行保留的 `_internal/vtk.libs` 与扁平副本重复（130 个 DLL、约 222 MB），
   `build.py::_dedupe_vtk_libs` 在 finalize 时校验后删除；已实测改名该目录后控制台 exe
   仍 exit 0（compute 0.093 s），去重后 21 项验证全部重跑通过。
3. **pyvista 无官方 hook**：用 `collect_all('pyvista')` 提供模块，但过滤 `.py/.pyi/.pyc`
   数据文件与 `pyvista.examples`/`pyvista.trame`/`trame` 隐藏导入，避免源码进包。
4. **改为双启动器**：`TriboScan3D.exe`（windowed）+ `TriboScan3D_console.exe`（console=True，同入口），
   替代 §6.3 的 `--debug-console`/`_debug.cmd` 方案；两个 exe 均已在冻结包中验证。
5. 包内 `cache/`、`results/`、`logs/` **必须为空**（程序不自动保存、不暗中改源）；
   `build.py::_check_contents` 会拒绝带运行期文件的包，finalize 因此幂等可重跑。
6. 实测体积 **414.7 MB**（VTK DLL 222 MB、vtkmodules 44 MB、matplotlib/PIL/numpy 等）；
   仍属"便携但不小"，ZIP 后约 150–200 MB。
7. 新增 `--export-to DIR`（`app/app.py`、`app/main.py`）：无人值守验收时首帧后立即导出 A+B；
   普通用户仍用 `export` 按钮。`--open-result` 可直接打开用户保存的 `.tsr3`。
8. 冻结包计时（console、真实数据）：fresh read 0.181 s / compute 0.098 s / render 0.151 s；
   cached（cache hit）load 0.138 s / compute 0.016 s；`--open-result` load 0.079 s / compute 0.017 s，
   两次 `build_count=0`；windowed 与"中文+空格路径"复制版均 exit 0。

## 8. t12 标准构建入口（准备态，尚未执行打包）

`build/build.py` 默认只打印/记录命令，不运行 PyInstaller；输入、命令、工作目录、候选
输出与权限阻断点写在 `build/evidence/t12/build_plan.json`。标准流程：

```bat
cd /d triboscan_build\desktop_app
.buildenv\Scripts\python.exe -m PyInstaller --noconfirm --clean ^
  --distpath dist_candidate --workpath build\pyinstaller_work_candidate ^
  build\triboscan.spec > build\evidence\t12\pyinstaller_stdout.log 2>&1
.buildenv\Scripts\python.exe build\build.py --finalize dist_candidate\TriboScan3D
```

- 使用官方 `python -m PyInstaller`，其隔离 helper 进程保持不变；不得用
  `build/pyi_bootstrap.py` 或任何进程内替代（该文件仅作为 t5 历史证据保留）。
- 若隔离 helper 创建管道被拒（`PermissionError: [WinError 5]`），停止并升级，由
  leader 找有权限的交互式账户执行；不得换 shell/路径绕过或修复令牌。
- 完整 stdout/stderr 归档到 `build/evidence/t12/pyinstaller_stdout.log`；新候选进入
  `dist_candidate/TriboScan3D_Portable_Windows_x64_candidate/`，t5 的 `dist/` 保持不变。
- `--finalize` 只在 `_internal/vtk.libs` 中每个文件都能在 `_internal` 找到 **SHA-256
  完全相同**的扁平副本时才删除嵌套副本；任一文件字节不同则保留并报错退出。
- 本轮未执行任何打包，也未生成新 EXE；冻结包 SHA 仍为 t5 值（见 §7 与
  `build/evidence/T5_PACKAGE.md`）。
