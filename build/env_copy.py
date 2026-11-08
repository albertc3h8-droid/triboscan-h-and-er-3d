"""Prepare desktop_app/.buildenv by copying the existing native_viewer venv.

Verification (captain's boundary): the source venv must be byte-identical
before/after the copy, and the copied interpreter must report itself as the new
environment (sys.prefix), with pip resolving inside the copy.

Usage: python build/env_copy.py
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT.parent / 'native_viewer' / '.venv'
DST = ROOT / '.buildenv'
EVIDENCE = ROOT / 'build' / 'evidence'


def manifest(root: Path) -> dict:
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError as exc:
                entries[str(path.relative_to(root))] = {'error': str(exc)}
                continue
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            entries[str(path.relative_to(root))] = {
                'bytes': stat.st_size, 'sha256': digest.hexdigest()}
    return entries


def main() -> int:
    if DST.exists() and any(DST.iterdir()):
        print(f'REFUSE: target already exists and is not empty: {DST}')
        return 2
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    print(f'source: {SRC}')
    print(f'target: {DST}')
    started = time.perf_counter()
    before = manifest(SRC)
    (EVIDENCE / 'venv_before.json').write_text(
        json.dumps(before, indent=1, sort_keys=True), encoding='utf-8')
    print(f'hashed {len(before)} source files before copy '
          f'({time.perf_counter() - started:.1f} s)')
    copy_started = time.perf_counter()
    shutil.copytree(SRC, DST, symlinks=True, copy_function=shutil.copy2)
    copy_s = time.perf_counter() - copy_started
    after = manifest(SRC)
    (EVIDENCE / 'venv_after.json').write_text(
        json.dumps(after, indent=1, sort_keys=True), encoding='utf-8')
    source_unchanged = before == after
    print(f'copied in {copy_s:.1f} s; source unchanged: {source_unchanged}')

    probe = '''
import json, sys, pip
print(json.dumps({
  "executable": sys.executable,
  "prefix": sys.prefix,
  "base_prefix": sys.base_prefix,
  "pip_file": pip.__file__,
  "version": sys.version,
  "path0": sys.path[0],
}, indent=1))
'''
    result = subprocess.run([str(DST / 'Scripts' / 'python.exe'), '-c', probe],
                            capture_output=True, text=True, timeout=120)
    (EVIDENCE / 'interpreter_probe.txt').write_text(
        result.stdout + result.stderr, encoding='utf-8')
    print(result.stdout)
    print(result.stderr)
    info = json.loads(result.stdout) if result.returncode == 0 else {}
    ok = (source_unchanged and info.get('prefix') == str(DST)
          and str(DST) in info.get('pip_file', ''))
    evidence = [
        '# .buildenv creation evidence', '',
        f'- source venv: `{SRC}`',
        f'- target venv: `{DST}`',
        f'- source files hashed: {len(before)}',
        f'- copy seconds: {copy_s:.1f}',
        f'- source byte-identical before/after: {source_unchanged}',
        f'- copied sys.prefix: `{info.get("prefix")}`',
        f'- copied sys.base_prefix: `{info.get("base_prefix")}`',
        f'- copied pip module: `{info.get("pip_file")}`',
        f'- python: `{info.get("version")}`',
        f'- verification ok: {ok}',
    ]
    (EVIDENCE / 'env_copy.md').write_text('\n'.join(evidence) + '\n',
                                          encoding='utf-8')
    print('VERIFY_OK' if ok else 'VERIFY_FAILED')
    return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
