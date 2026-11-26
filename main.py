"""启动入口：保持项目根在 sys.path，再交给 app.main。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import main  # noqa: E402

if __name__ in {"__main__", "__mp_main__"}:
    main()
