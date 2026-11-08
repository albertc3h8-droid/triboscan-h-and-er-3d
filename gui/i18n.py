"""Small, dependency-free UI language layer for the portable viewer.

The product ships two languages, ``zh-CN`` and ``en``.  The preferred language
is chosen from the Windows UI language and may be overridden from the top-right
toggle.  The choice is stored next to the executable in ``config/settings.json``
so the portable folder remains self-contained; the registry is never touched.

Only user-visible prose is translated.  Technical tokens (nm, GPa, RAW,
Delaunay3D, Er, H, CSV, Excel) stay unchanged.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
from pathlib import Path

from runtime.paths import app_root

LANGUAGES = ('zh-CN', 'en')
DEFAULT_LANGUAGE = 'en'
FONT_CANDIDATES = (
    Path(r'C:\Windows\Fonts\msyh.ttc'),
    Path(r'C:\Windows\Fonts\simhei.ttf'),
    Path(r'C:\Windows\Fonts\simsun.ttc'),
)

STRINGS = {
    'en': {
        'btn.normal': 'normal',
        'btn.locate': 'locate',
        'btn.raw': 'raw',
        'btn.reset': 'reset',
        'btn.open': 'open',
        'btn.export': 'export',
        'btn.save': 'save',
        'btn.load': 'load',
        'btn.common': 'common',
        'btn.actual': 'actual',
        'btn.auto': 'auto',
        'btn.layers': 'layers',
        'btn.envelope': 'envelope',
        'btn.language': '中文',
        'label.range': 'RANGE',
        'label.grey': 'GREY',
        'depth.heff': 'heff = {depth:.3f} nm',
        'depth.none': 'heff = --',
        'depth.raw': 'RAW: all measured depths',
        'help.slice': 'exact slice; XY grid uses real physical spans',
        'help.raw': 'RAW: measured points only; no interpolated slice',
        'kind.synthetic': 'SYNTHETIC (not measured)',
        'kind.transformed': 'transformed original',
        'kind.measured': 'raw measurement',
        'kind.short_synthetic': 'SYNTHETIC',
        'kind.short_transformed': 'transformed',
        'kind.short_measured': 'measured',
        'status.exact_compact': (
            'exact Z {depth:.2f} nm | {valid} samples | '
            'field {nx}x{ny} dx={dx:.0f} nm | Delaunay3D'),
        'status.range_compact': (
            '{mode} {lo:.1f}..{hi:.1f} nm | {view} | {kind} | '
            'fixed Er {er0:.1f}-{er1:.1f} H {h0:.1f}-{h1:.1f} GPa'),
        'status.exact_full': (
            'exact Z {depth:.3f} nm | {valid} supported, {blank} blank | '
            'field {nx}x{ny}x{nz} (dx={dx:.1f} dy={dy:.1f} '
            'dz={dz:.2f} nm, Delaunay3D+probe)'),
        'status.range_full': (
            '{mode} {lo:.3f}..{hi:.3f} nm | view {view} | {kind} | '
            'fixed Er {er0:.2f}..{er1:.2f} H {h0:.2f}..{h1:.2f} GPa'),
        'status.raw1': (
            'RAW: {points} original XYZ points ({groups} XY groups), '
            'measured Er/H values, no interpolation/surface'),
        'status.raw2': (
            'slice control disabled in raw mode | real depth range '
            '{z0:.3f}..{z1:.3f} nm | {kind}'),
        'status.unavailable': (
            '{mode} range unavailable: {note}. No slice is drawn '
            '(no silent fallback to another range or old field).'),
        'info.cache_short': 'cache hit {seconds}',
        'info.computed_short': 'computed {seconds}',
        'info.ref': 'ref {ref}',
        'info.cache_full': 'loaded from cache (field not rebuilt)',
        'info.computed_full': 'computed 3D field',
        'info.timings': 'read {read} | compute {compute} | render {render}',
        'info.save_hint': 'compute >5 s: "save" stores this result',
        'info.source_saved': 'source: saved result file',
        'summary.mode': '{prefix}: {resolved}',
        'summary.range_note': '{mode}: {note}',
        'prefix.auto': 'auto',
        'prefix.manual': 'manual',
        'resolved.layers': '{n} layers (N={n})',
        'resolved.envelope': 'envelope (N={n})',
        'reference.auto_layers': 'auto {n}L',
        'reference.manual_layers': 'manual {n}L',
        'reference.auto_envelope': 'auto env',
        'reference.manual_envelope': 'manual env',
        'slice.none': 'no slice',
        'slice.summary': 'exact Z-slice heff={depth:.3f} nm | {valid} supported samples, {blank} blank',
        'source.one': 'source: {file}',
        'source.same_xy': 'source: {first} | {second} (same XY)',
        'source.many': 'source: {first} | {second} (+{extra} more at same XY)',
        'point.line': 'point: {file} line {line} row {row}',
        'point.xyz': 'real XYZ: X={x:.1f} Y={y:.1f}',
        'point.depth': 'heff={z:.3f} nm (slice {slice:.3f}, dz={dz:.3f})',
        'point.values': 'Er={er:.3f} GPa, H={h:.3f} GPa (same fixed clim)',
        'point.group': 'group XY: {x:.1f}, {y:.1f} nm; 2D dist={dist:.1f} nm',
        'point.tie': 'same XY files (deterministic tie): {files}',
        'hint.occluded': (
            'locate hint: the real value-coloured sphere at real XYZ is behind '
            'the opaque slice; the thin screen ring is an auxiliary guide at '
            'its projected position, not the measured sphere'),
        'hint.front': (
            'locate hint: the thin screen ring is an auxiliary guide; the real '
            'value-coloured sphere at real XYZ is in front of the slice'),
        'notice.save_unavailable': 'save is available only after a slow (>5 s) compute',
        'notice.saved': 'saved result: {name}',
        'notice.exported': 'exported {count} files -> {name}',
        'notice.exported_path': 'exported {count} files to {path}',
        'notice.exported_open_failed': (
            'exported {count} files to {path} (could not open the folder)'),
        'notice.export_failed': 'export failed: {message} | reason: {detail}',
        'notice.export_cancelled': 'export cancelled',
        'notice.points_only': (
            'measured points only: this CSV has no interpolated field; open '
            'the data folder to recompute it'),
        'dialog.export_folder': 'Choose export folder / 选择导出文件夹',
        'dialog.export_field': 'Export the interpolated field (.tsr3)',
        'dialog.open_result': (
            'Open a saved result (*.tsr3) or a raw-points export (*.csv)'),
        'notice.font_fallback': 'CJK font not found; English UI is used',
        'notice.language_saved': 'language preference saved',
        'loading.title': 'TriboScan3D - loading',
        'loading.preparing': 'preparing',
        'loading.elapsed': 'elapsed {seconds:6.1f} s',
        'loading.hint': 'background compute - Esc cancels; window stays responsive',
        'stage.scan': 'scanning source folder',
        'stage.signature': 'hashing source files',
        'stage.cache_check': 'checking the result cache',
        'stage.cache_hit': 'cache hit',
        'stage.parse': 'reading measurements',
        'stage.field': 'building the 3D field',
        'stage.spacing': 'display spacing',
        'stage.ready': 'ready',
        'stage.result_file': 'opening saved result',
        'stage.detail.scan': 'scanning {directory}',
        'stage.detail.signature': 'hashing {count} source files',
        'stage.detail.cache_check': 'checking the saved result cache',
        'stage.detail.cache_hit': 'saved field found - field build skipped',
        'stage.detail.parse': 'reading {count} measurement files',
        'stage.detail.field': 'Delaunay3D + probe',
        'stage.detail.spacing': 'nearest-neighbour display spacing',
        'stage.detail.ready': 'data ready',
        'stage.detail.result_file': 'opening saved result {name}',
        'stage.detail.result_ready': 'saved result ready',
        'stage.detail.points_csv': 'reading raw points {name}',
        'message.close_hint': 'Close this window to continue.',
        'message.cannot_load': '{app} - cannot load',
        'message.error_title': '{app} - error',
        'error.data_folder_missing': 'The data folder does not exist:\n{path}',
        'error.select_folder': (
            'Please select the folder that contains the *_analysis.txt files, '
            'not a file:\n{path}'),
        'error.no_analysis_files': (
            'No *_analysis.txt files were found in the first level of this '
            'folder:\n{path}'),
        'error.saved_result_missing': 'The saved result file does not exist:\n{path}',
        'error.open_saved_result': (
            'Cannot open the saved result file:\n{path}\n\n{error}'),
        'error.result_adapter_unavailable': (
            'The result cache module is not available in this build.'),
        'error.data_invalid': 'Cannot read the measurement data:\n{error}',
        'error.compute_failed': 'Cannot compute the 3D field:\n{error}',
        'error.points_csv_header': (
            'This CSV is not a TriboScan3D raw-points export.\n'
            'Missing columns: {missing}\nFound: {found}'),
        'error.points_csv_row': 'Cannot read row {row} of {name}: {reason}',
        'error.save_dialog': 'Cannot open the save dialog: {reason}',
        'error.save_failed': (
            'save failed: {path} | {kind}: {reason} | choose a writable folder '
            'or use the default results folder'),
        # ---- UI v2 (P1 skeleton) ----
        'toolbar.tagline': '3D hardness visualisation · real data · native PyVista',
        'toolbar.open': 'open data folder',
        'toolbar.open_result': 'open result file',
        'toolbar.save': 'save result',
        'toolbar.help': 'help',
        'sidebar.title.data': '1. DATA',
        'sidebar.title.view': '2. VIEW MODE',
        'sidebar.title.range': '3. DEPTH RANGE',
        'sidebar.title.reference': '4. REFERENCE LAYERS',
        'sidebar.dataset': 'dataset',
        'sidebar.points': 'points',
        'sidebar.files': 'files',
        'sidebar.grid': 'grid',
        'sidebar.step': 'step',
        'sidebar.normal': 'Normal',
        'sidebar.locate': 'Locate',
        'sidebar.raw': 'Raw',
        'sidebar.common': 'Common',
        'sidebar.actual': 'Actual',
        'sidebar.auto': 'Auto',
        'sidebar.layers': 'Layers',
        'sidebar.envelope': 'Envelope',
        'sidebar.title.interp': '5. SLICE INTERPOLATION',
        'sidebar.interp.linear': 'Linear (raw)',
        'sidebar.interp.cubic': 'Cubic spline (C2)',
        'sidebar.interp.tps': 'Thin-plate spline (TPS)',
        'sidebar.interp.note': 'Interpolates measured points, no blur',
        'punct.colon': ': ',
        'header.title': '3D view',
        'header.slice': 'slice (current depth)',
        'header.cloud': 'point cloud (all depths)',
        'header.fog': 'haze above slice',
        'header.reset': 'reset view',
        'header.hint': 'mouse: drag rotate | right-drag pan | wheel zoom',
        'card.er': 'Elastic modulus Er (GPa)',
        'card.h': 'Hardness H (GPa)',
        'card.heff': 'heff = {depth:.3f} nm',
        'depth.title': 'DEPTH CONTROL (heff)',
        'depth.thumb': 'current slice ({metric})',
        'depth.current': 'current depth',
        'depth.total': 'total depth: {lo:.2f} - {hi:.2f} nm',
        'depth.none': 'no depth data',
        'depth.strip.index': 'slice index: {idx}/{count}',
        'depth.strip.step': 'depth step: {dz:.2f} nm',
        'depth.strip.view': 'view: X-Y plane',
        'hist.title': 'MEASURED DEPTH DISTRIBUTION (1 nm bins)',
        'hist.y': 'points',
        'hist.x': 'depth heff (nm)',
        'hist.marker': 'current depth',
        'hist.note': 'bars count measured points per 1 nm bin (not interpolated)',
        'export.section': 'EXPORT',
        'export.image.title': 'export image PNG',
        'export.image.line1': 'clean view, no controls',
        'export.image.line2': 'no on-screen controls',
        'export.field.title': 'export field (.tsr3)',
        'export.field.line1': 'field + measured points',
        'export.field.line2': 'reopen, no recompute',
        'export.raw.title': 'export raw points (CSV)',
        'export.raw.line1': 'measured points, one CSV',
        'export.raw.line2': 'Excel opens it directly',
        'export.dir_title': 'export folder',
        'export.browse': 'browse...',
        'export.open_after': 'open folder after export',
        'export.dir_note': 'all three exports share this folder',
        'status.version': 'TriboScan3D v{version}',
        'status.heff': 'heff {depth:.3f} nm',
        'status.ready': 'ready',
        'status.view': 'view: {view}',
        'notice.help': 'left drag rotate | right drag pan | wheel zoom',
        'help.title': 'Help',
        'help.subtitle': 'TriboScan3D · UI v2',
        'help.view.title': 'View controls',
        'help.view.b1': 'Left drag rotate · right drag pan · wheel zoom',
        'help.view.b2': 'Reset restores the default view',
        'help.view.b3': 'Locate mode: hover a measured point for its value',
        'help.depth.title': 'Depth & slice',
        'help.depth.b1': 'Drag the slider, or click the number box to type a depth',
        'help.depth.b2': 'The histogram red line marks the current slice',
        'help.depth.b3': 'Haze fades distant points; reference Auto / Layers / Envelope',
        'help.depth.b4': 'Display modes Normal / Locate / Raw',
        'help.interp.title': 'Slice interpolation',
        'help.interp.b1': 'Linear: fastest, best for a quick check',
        'help.interp.b2': 'Cubic: smoother tensor-product interpolation',
        'help.interp.b3': 'TPS (default): isotropic, passes through measured nodes; first slice is slower',
        'help.export.title': 'Export',
        'help.export.b1': 'PNG: the two 3D views only, no UI',
        'help.export.b2': '.tsr3: saves the interpolated field for reopening',
        'help.export.b3': 'CSV: the measured points',
        'help.export.b4': 'Pick a folder, then optionally open it after export',
        'help.keys.title': 'Shortcuts',
        'help.keys.b1': 'F1 open / close help',
        'help.keys.b2': 'Esc close help / cancel depth entry',
        'help.keys.b3': 'Digits edit depth (click the number box first)',
        'help.footer': 'F1 open / close    Esc close',
        'help.note': 'The first TPS slice takes about 0.4 s, later ones about 10 ms; interpolation only changes the display, not the stored field or exports.',
        'welcome.title': 'No data open yet',
        'welcome.subtitle': 'Open a raw data folder, or an exported .tsr3 / .csv result file.',
        'welcome.open_data': 'Open data folder',
        'welcome.open_result': 'Open exported result',
        'welcome.hint': 'The 3D views, slice controls and export area appear here after a dataset is open.',
        'welcome.status': 'no data open',
        'welcome.window_title': 'no data',
        'app.title_error': '{app} - error',
    },
    'zh-CN': {
        'btn.normal': '法向',
        'btn.locate': '定位',
        'btn.raw': '原始点',
        'btn.reset': '重置',
        'btn.open': '打开',
        'btn.export': '导出',
        'btn.save': '保存',
        'btn.load': '载入',
        'btn.common': '公共',
        'btn.actual': '实际',
        'btn.auto': '自动',
        'btn.layers': '分层',
        'btn.envelope': '包络',
        'btn.language': 'English',
        'label.range': '范围',
        'label.grey': '灰层',
        'depth.heff': 'heff = {depth:.3f} nm',
        'depth.none': 'heff = --',
        'depth.raw': 'RAW：全部实测深度',
        'help.slice': 'exact slice（精确切片）；XY 网格使用真实物理跨度',
        'help.raw': 'RAW: measured points only（仅实测点，无插值切片）',
        'kind.synthetic': '合成数据（非实测）',
        'kind.transformed': '变换后的原始数据',
        'kind.measured': '原始测量',
        'kind.short_synthetic': '合成',
        'kind.short_transformed': '变换',
        'kind.short_measured': '实测',
        'status.exact_compact': (
            '精确 Z {depth:.2f} nm | {valid} 个有效点 | '
            '场 {nx}x{ny} dx={dx:.0f} nm | Delaunay3D'),
        'status.range_compact': (
            '{mode} {lo:.1f}..{hi:.1f} nm | {view} | {kind} | '
            '固定 Er {er0:.1f}-{er1:.1f} H {h0:.1f}-{h1:.1f} GPa'),
        'status.exact_full': (
            '精确 Z {depth:.3f} nm | {valid} 个有效，{blank} 个空白 | '
            '场 {nx}x{ny}x{nz} (dx={dx:.1f} dy={dy:.1f} '
            'dz={dz:.2f} nm, Delaunay3D+probe)'),
        'status.range_full': (
            '{mode} {lo:.3f}..{hi:.3f} nm | 视图 {view} | {kind} | '
            '固定 Er {er0:.2f}..{er1:.2f} H {h0:.2f}..{h1:.2f} GPa'),
        'status.raw1': (
            'RAW：{points} 个原始 XYZ 点（{groups} 个 XY 组），'
            '实测 Er/H 值，无插值/曲面'),
        'status.raw2': (
            'raw 模式禁用切片控制 | 真实深度范围 '
            '{z0:.3f}..{z1:.3f} nm | {kind}'),
        'status.unavailable': (
            '{mode} 范围不可用：{note}。未绘制切片'
            '（不会静默回退到其他范围或旧场）。'),
        'info.cache_short': '缓存命中 {seconds}',
        'info.computed_short': '已计算 {seconds}',
        'info.ref': 'ref {ref}',
        'info.cache_full': '已从缓存载入（未重建三维场）',
        'info.computed_full': '已计算三维场',
        'info.timings': '读入 {read} | 计算 {compute} | 渲染 {render}',
        'info.save_hint': '计算 >5 秒：点击“保存”可保存本次结果',
        'info.source_saved': '来源：已保存结果文件',
        'summary.mode': '{prefix}: {resolved}',
        'summary.range_note': '{mode}: {note}',
        'prefix.auto': 'auto',
        'prefix.manual': 'manual',
        'resolved.layers': '{n} 层（layers, N={n}）',
        'resolved.envelope': '包络 (N={n})',
        'reference.auto_layers': 'auto {n}L',
        'reference.manual_layers': 'manual {n}L',
        'reference.auto_envelope': 'auto 包络',
        'reference.manual_envelope': 'manual 包络',
        'slice.none': '无切片',
        'slice.summary': '精确 Z 切片 heff={depth:.3f} nm | {valid} 个有效点，{blank} 个空白',
        'source.one': '来源：{file}',
        'source.same_xy': '来源：{first} | {second}（相同 XY）',
        'source.many': '来源：{first} | {second}（相同 XY 另有 {extra} 个）',
        'point.line': '测点：{file} 第 {line} 行，第 {row} 条',
        'point.xyz': '真实 XYZ：X={x:.1f} Y={y:.1f}',
        'point.depth': 'heff={z:.3f} nm（切片 {slice:.3f}，dz={dz:.3f}）',
        'point.values': 'Er={er:.3f} GPa，H={h:.3f} GPa（同一固定 clim）',
        'point.group': '组 XY：{x:.1f}, {y:.1f} nm；二维距离={dist:.1f} nm',
        'point.tie': '相同 XY 文件（确定性选择）：{files}',
        'hint.occluded': (
            '定位提示：真实 XYZ 处的真实着色球被不透明切片遮挡；'
            '细屏幕圆环只是其投影位置的辅助指示，不是实测球'),
        'hint.front': (
            '定位提示：细屏幕圆环只是辅助指示；真实 XYZ 处的真实着色球'
            '位于切片前方'),
        'notice.save_unavailable': '仅在计算 >5 s 后可保存',
        'notice.saved': '已保存结果：{name}',
        'notice.exported': '已导出 {count} 个文件 -> {name}',
        'notice.exported_path': '已导出 {count} 个文件到 {path}',
        'notice.exported_open_failed': (
            '已导出 {count} 个文件到 {path}（无法自动打开文件夹）'),
        'notice.export_failed': '导出失败：{message} | 原因：{detail}',
        'notice.export_cancelled': '已取消导出',
        'notice.points_only': (
            '仅显示实测测点：此 CSV 不含插值场；如需插值场请重新选择数据目录计算'),
        'dialog.export_folder': '选择导出文件夹 / Choose export folder',
        'dialog.export_field': '导出插值场（.tsr3）',
        'dialog.open_result': '打开已保存结果（.tsr3）或原始点导出（.csv）',
        'notice.font_fallback': '未找到中文字体，已回退英文界面',
        'notice.language_saved': '语言偏好已保存',
        'loading.title': 'TriboScan3D - 载入数据',
        'loading.preparing': '准备中',
        'loading.elapsed': '已用 {seconds:6.1f} s',
        'loading.hint': '后台计算，按 Esc 取消；窗口保持响应',
        'stage.scan': '扫描数据文件夹',
        'stage.signature': '计算源文件签名',
        'stage.cache_check': '检查结果缓存',
        'stage.cache_hit': '缓存命中',
        'stage.parse': '读取测量文件',
        'stage.field': '构建三维场',
        'stage.spacing': '显示间距统计',
        'stage.ready': '就绪',
        'stage.result_file': '打开已保存结果',
        'stage.detail.scan': '正在扫描 {directory}',
        'stage.detail.signature': '正在计算 {count} 个源文件签名',
        'stage.detail.cache_check': '正在检查已保存结果缓存',
        'stage.detail.cache_hit': '找到已保存场，跳过三维场构建',
        'stage.detail.parse': '正在读取 {count} 个测量文件',
        'stage.detail.field': 'Delaunay3D + 探针',
        'stage.detail.spacing': '最近邻显示间距',
        'stage.detail.ready': '数据就绪',
        'stage.detail.result_file': '正在打开已保存结果 {name}',
        'stage.detail.result_ready': '已保存结果就绪',
        'stage.detail.points_csv': '正在读取原始点文件 {name}',
        'message.close_hint': '关闭此窗口以继续。',
        'message.cannot_load': '{app} - 无法载入',
        'message.error_title': '{app} - 错误',
        'error.data_folder_missing': '数据文件夹不存在：\n{path}',
        'error.select_folder': '请选择包含 *_analysis.txt 文件的文件夹，而不是文件：\n{path}',
        'error.no_analysis_files': '该文件夹第一层未找到 *_analysis.txt 文件：\n{path}',
        'error.saved_result_missing': '已保存结果文件不存在：\n{path}',
        'error.open_saved_result': '无法打开已保存结果文件：\n{path}\n\n{error}',
        'error.result_adapter_unavailable': '此构建未包含结果缓存模块。',
        'error.data_invalid': '无法读取测量数据：\n{error}',
        'error.compute_failed': '无法计算三维场：\n{error}',
        'error.points_csv_header': (
            '该 CSV 不是本程序导出的原始点文件。\n'
            '缺少列：{missing}\n实际列：{found}'),
        'error.points_csv_row': '无法读取 {name} 第 {row} 行：{reason}',
        'error.save_dialog': '无法打开保存对话框：{reason}',
        'error.save_failed': (
            '保存失败：{path} | {kind}：{reason} | 请改选可写目录，'
            '或使用默认 results 目录'),
        # ---- UI v2（P1 骨架）----
        'toolbar.tagline': '三维硬度可视化分析 · 真实数据 · 原生 PyVista',
        'toolbar.open': '打开数据文件夹',
        'toolbar.open_result': '打开结果文件',
        'toolbar.save': '保存结果',
        'toolbar.help': '帮助',
        'sidebar.title.data': '1. 数据信息',
        'sidebar.title.view': '2. 视图模式',
        'sidebar.title.range': '3. 深度范围',
        'sidebar.title.reference': '4. 参考层显示',
        'sidebar.dataset': '数据集',
        'sidebar.points': '测点数',
        'sidebar.files': '文件数',
        'sidebar.grid': '三维网格',
        'sidebar.step': '步长',
        'sidebar.normal': 'Normal（正常视图）',
        'sidebar.locate': 'Locate（定位模式）',
        'sidebar.raw': 'Raw（全部实测深度）',
        'sidebar.common': 'Common（公共范围）',
        'sidebar.actual': 'Actual（各自范围）',
        'sidebar.auto': 'Auto（自动识别）',
        'sidebar.layers': 'Layers（分层显示）',
        'sidebar.envelope': 'Envelope（包络显示）',
        'sidebar.title.interp': '5. 切片插值',
        'sidebar.interp.linear': '线性（原始）',
        'sidebar.interp.cubic': '三次样条（C2）',
        'sidebar.interp.tps': '薄板样条（TPS）',
        'sidebar.interp.note': '插值穿过实测点，不做模糊',
        'punct.colon': '：',
        'header.title': '3D 三维视图',
        'header.slice': '切片视图（当前深度）',
        'header.cloud': '完整云图（全部深度）',
        'header.fog': '切片以上淡雾',
        'header.reset': '重置视图',
        'header.hint': '鼠标：左键旋转  右键平移  滚轮缩放',
        'card.er': '弹性模量 Er（GPa）',
        'card.h': '硬度 H（GPa）',
        'card.heff': 'heff = {depth:.3f} nm',
        'depth.title': '深度控制（heff）',
        'depth.thumb': '当前切片（{metric}）',
        'depth.current': '当前深度',
        'depth.total': '总深度范围：{lo:.2f} – {hi:.2f} nm',
        'depth.none': '无深度数据',
        'depth.strip.index': '切片索引：{idx}/{count}',
        'depth.strip.step': '深度步长：{dz:.2f} nm',
        'depth.strip.view': '当前视图：X-Y 平面',
        'hist.title': '实测深度分布（1 nm 统计）',
        'hist.y': '测点数',
        'hist.x': '深度 heff（nm）',
        'hist.marker': '当前深度',
        'hist.note': '灰条表示每 1 nm 区间内的实测点数（非插值数据）',
        'export.section': '导出操作',
        'export.image.title': '导出图片（PNG）',
        'export.image.line1': '当前视图的干净图',
        'export.image.line2': '不含操作控件',
        'export.field.title': '导出插值场（.tsr3）',
        'export.field.line1': '插值场+实测点+来源签名',
        'export.field.line2': '可重开，无需重新计算',
        'export.raw.title': '导出原始点（CSV）',
        'export.raw.line1': '仅实测测点，单个 CSV 文件',
        'export.raw.line2': '可直接用 Excel 打开',
        'export.dir_title': '导出目录',
        'export.browse': '浏览...',
        'export.open_after': '导出后打开文件夹',
        'export.dir_note': '三种导出共用此目录',
        'status.version': 'TriboScan3D v{version}',
        'status.heff': 'heff {depth:.3f} nm',
        'status.ready': '就绪',
        'status.view': '当前视图：{view}',
        'notice.help': '鼠标：左键旋转  右键平移  滚轮缩放',
        'help.title': '帮助',
        'help.subtitle': 'TriboScan3D · UI v2',
        'help.view.title': '视图操作',
        'help.view.b1': '左键旋转 · 右键平移 · 滚轮缩放',
        'help.view.b2': '「重置」恢复默认视角',
        'help.view.b3': 'Locate 模式悬停测点查看数值',
        'help.depth.title': '深度与切片',
        'help.depth.b1': '拖动滑条，或点数字框输入深度',
        'help.depth.b2': '直方图红线标出当前切片位置',
        'help.depth.b3': '「雾」弱化远处点；参考层 Auto / Layers / Envelope',
        'help.depth.b4': '显示模式 Normal / Locate / Raw',
        'help.interp.title': '切片插值',
        'help.interp.b1': 'Linear：最快，适合快速查看',
        'help.interp.b2': 'Cubic：更平滑的张量积插值',
        'help.interp.b3': 'TPS（默认）：各向同性，精确过实测点；首次切片稍慢',
        'help.export.title': '导出',
        'help.export.b1': 'PNG：仅两个 3D 视图，不含 UI',
        'help.export.b2': '.tsr3：保存插值场，可重新打开',
        'help.export.b3': 'CSV：原始测量点',
        'help.export.b4': '选择目录后可勾选「完成后打开」',
        'help.keys.title': '快捷键',
        'help.keys.b1': 'F1 打开或关闭帮助',
        'help.keys.b2': 'Esc 关闭帮助 / 取消深度输入',
        'help.keys.b3': '数字键编辑深度（先点数值框）',
        'help.footer': 'F1 打开 / 关闭    Esc 关闭',
        'help.note': 'TPS 首次切片约 0.4 s，之后约 10 ms；插值只影响显示，存储场和导出不变。',
        'welcome.title': '尚未打开数据',
        'welcome.subtitle': '打开原始数据文件夹，或直接打开已导出的 .tsr3 / .csv 结果文件。',
        'welcome.open_data': '打开数据文件夹',
        'welcome.open_result': '打开已导出结果',
        'welcome.hint': '打开数据后，这里会显示三维视图、切片控制和导出区。',
        'welcome.status': '未打开数据',
        'welcome.window_title': '未打开数据',
        'app.title_error': '{app} - 错误',
    },
}


def normalize_language(value) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if text.startswith('zh'):
        return 'zh-CN'
    if text.startswith('en'):
        return 'en'
    return None


def system_language() -> str:
    """Chinese Windows UI -> zh-CN; every other UI language -> en."""
    try:
        langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:
        return 'en'
    # Primary language 0x04 is Chinese (any sublanguage).
    return 'zh-CN' if (langid & 0x3FF) == 0x04 else 'en'


def find_cjk_font() -> str | None:
    for candidate in FONT_CANDIDATES:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def tr(language: str, key: str, **kwargs) -> str:
    table = STRINGS.get(language) or STRINGS[DEFAULT_LANGUAGE]
    template = table.get(key)
    if template is None:
        template = STRINGS[DEFAULT_LANGUAGE].get(key, key)
    return template.format(**kwargs)


_EXPORTED = re.compile(r'^exported (\d+) files? -> (.+)$')
_SAVED = re.compile(r'^saved result: (.+)$')


def localize_notice(text: str, language: str) -> str:
    """Translate the few legacy English notices emitted by ``app.app``.

    The task keeps ``app.py`` changes to save-failure diagnostics only, so the
    two ordinary success notices are translated here at the display boundary.
    Unknown text is preserved verbatim rather than hidden or guessed.
    """
    text = str(text)
    match = _EXPORTED.match(text)
    if match:
        return tr(language, 'notice.exported', count=match.group(1),
                  name=match.group(2))
    match = _SAVED.match(text)
    if match:
        return tr(language, 'notice.saved', name=match.group(1))
    return text


class LanguageSettings:
    """Portable language preference in ``config/settings.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else (
            app_root() / 'config' / 'settings.json')

    def load(self) -> str:
        return normalize_language(self._load_dict().get('language')) or system_language()

    def _load_dict(self) -> dict:
        """Read the shared settings object; corrupt/missing JSON means empty."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_dict(self, data: dict) -> bool:
        """Best-effort portable write; never raises into the export path."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + '.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n',
                            encoding='utf-8', newline='\n')
            os.replace(temp, self.path)
            return True
        except OSError:
            return False

    def load_export_dir(self) -> str | None:
        """Last successful export folder, or ``None`` when absent/unusable."""
        value = self._load_dict().get('export_dir')
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            path = Path(value)
            return str(path.resolve()) if path.is_dir() else None
        except (OSError, ValueError):
            return None

    def save_export_dir(self, directory: str | Path) -> bool:
        """Remember the chosen folder; a read/write failure never blocks export."""
        data = self._load_dict()
        try:
            data['export_dir'] = str(Path(directory).resolve())
        except (OSError, ValueError):
            return False
        return self._write_dict(data)

    def save(self, language: str) -> None:
        language = normalize_language(language)
        if language is None:
            return
        data = self._load_dict()
        data['language'] = language
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n',
                        encoding='utf-8', newline='\n')
        os.replace(temp, self.path)


def preferred_language(settings_path: Path | None = None) -> str:
    """The one shared UI language preference (viewer and loading window)."""
    return LanguageSettings(settings_path).load()


def resolve_language(language: str | None = None,
                     settings_path: Path | None = None,
                     font: str | None = None) -> tuple[str, str | None]:
    """Pick the effective language and CJK font, falling back to English.

    A Chinese preference without a usable CJK font is displayed in English so
    the window never contains tofu boxes.
    """
    chosen = normalize_language(language) or preferred_language(settings_path)
    if chosen == 'zh-CN':
        candidate = font if font is not None else find_cjk_font()
        if not candidate:
            return 'en', None
        return chosen, candidate
    return 'en', None
