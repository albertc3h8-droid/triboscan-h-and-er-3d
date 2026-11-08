# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the TriboScan3D portable (onedir) package.

Two launchers share one runtime folder:
  * TriboScan3D.exe          - windowed product launcher (no console)
  * TriboScan3D_console.exe  - same program with a console, for diagnostics
"""
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all
import vtkmodules

ROOT = Path(SPECPATH).resolve().parent          # desktop_app/
ENTRY = str(ROOT / 'build' / 'entry.py')
VERSION_FILE = str(ROOT / 'build' / 'version_info.txt')

datas, binaries, hiddenimports = [], [], []

# PyVista has no official PyInstaller hook: collect modules for the PYZ and
# only real package data (never .py/.pyi sources as loose files).
for package in ('pyvista',):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += [item for item in pkg_datas
              if not str(item[0]).lower().endswith(('.py', '.pyi', '.pyc'))]
    binaries += pkg_binaries
    hiddenimports += [name for name in pkg_hidden
                      if not name.startswith(('pyvista.examples',
                                              'pyvista.trame', 'trame'))]

# VTK wheel keeps its C++ DLLs in ``vtk.libs``; PyInstaller cannot resolve them
# from there, so collect every DLL into ``_internal`` (next to python312.dll).
_vtk_libs = Path(vtkmodules.__file__).resolve().parent.parent / 'vtk.libs'
if not _vtk_libs.is_dir():
    raise SystemExit(f'VTK DLL folder not found: {_vtk_libs}')
binaries += [(str(dll), '.') for dll in sorted(_vtk_libs.glob('*.dll'))]

# VTK ships one extension module per toolkit; hooks-contrib has a hook for each
# and resolves their interdependencies.  Importing ``vtkmodules.all`` makes the
# product's single VTK import deterministic.
hiddenimports += [
    'vtk',
    'vtkmodules.all',
    'vtkmodules.numpy_interface.dataset_adapter',
    'vtkmodules.util.numpy_support',
    'pyvista.plotting',
    'pyvista.core',
    'matplotlib',
    'matplotlib.backends.backend_agg',
    'openpyxl',
    'openpyxl.cell._writer',
    'et_xmlfile',
    'result_store',
    'io_exports',
    'PIL',
    'PIL.Image',
    'numpy',
]

excluded = [
    'tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.ttk',
    'pytest', '_pytest', 'pandas', 'scipy', 'plotly',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx',
    'IPython', 'jupyter', 'notebook', 'sphinx', 'pydoc',
    'pyvista.examples', 'pyvista.trame', 'trame', 'trame_vtk',
]

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / 'build' / 'hooks_override')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TriboScan3D',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_FILE,
)

exe_console = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TriboScan3D_console',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_FILE,
)

coll = COLLECT(
    exe_gui,
    exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TriboScan3D',
)
