"""Makes scanner.py and editorial_gates.py -- scanner/'s flat modules --
importable from within the trend_radar package, regardless of the
caller's cwd or how trend_radar itself was imported. Both modules are
confirmed side-effect-free at import time (scanner.py gates all execution
behind `if __name__ == "__main__"`; editorial_gates.py is a stdlib-only
library with no top-level side effects), so importing them here does not
violate "no import-time database or network side effects."
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import editorial_gates  # noqa: E402
import scanner  # noqa: E402

__all__ = ["scanner", "editorial_gates"]
