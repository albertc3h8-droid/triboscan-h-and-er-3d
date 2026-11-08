"""PyInstaller entry point for the portable TriboScan3D program."""
from __future__ import annotations

import sys

from app.main import main

if __name__ == '__main__':
    raise SystemExit(main())
