"""命令行启动到货检验隔离后端服务。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iqc.__main__ import main

if __name__ == "__main__":
    main()
