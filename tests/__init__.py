"""测试包初始化：保证 src/ 进入 sys.path（与各测试模块的直接运行兼容）。"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
