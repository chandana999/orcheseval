"""Entry point: python scripts/migrate.py [upgrade|status]"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "src"), str(_ROOT)]

from evalorch.db.migrate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
