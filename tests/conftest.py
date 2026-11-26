"""pytest 运行支持：把工作区根目录加入导入路径，容器内以 `pytest /app/tests` 运行。"""

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
