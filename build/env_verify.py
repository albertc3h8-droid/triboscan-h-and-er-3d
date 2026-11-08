"""Re-hash the source venv and compare it with the manifest taken after copying.

Writes build/evidence/venv_source_check.md.  Run with the copied interpreter
or the source interpreter; no subprocess is used (the sandbox denies pipes).
"""
from __future__ import annotations

from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT.parent / 'native_viewer' / '.venv'
EVIDENCE = ROOT / 'build' / 'evidence'


def main() -> int:
    before = json.loads((EVIDENCE / 'venv_after.json').read_text(encoding='utf-8'))
    current = {}
    for dirpath, dirnames, filenames in __import__('os').walk(SRC):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            import hashlib
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            current[str(path.relative_to(SRC))] = {
                'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}
    equal = before == current
    lines = ['# Source venv unchanged after build-environment installation', '',
             f'- source: `{SRC}`',
             f'- files compared: {len(before)}',
             f'- byte-identical: {equal}']
    if not equal:
        missing = sorted(set(before) - set(current))
        added = sorted(set(current) - set(before))
        changed = sorted(k for k in before.keys() & current.keys()
                         if before[k] != current[k])
        lines += [f'- missing: {missing[:10]}',
                  f'- added: {added[:10]}',
                  f'- changed: {changed[:10]}']
    (EVIDENCE / 'venv_source_check.md').write_text('\n'.join(lines) + '\n',
                                                   encoding='utf-8')
    print('\n'.join(lines))
    return 0 if equal else 3


if __name__ == '__main__':
    raise SystemExit(main())
