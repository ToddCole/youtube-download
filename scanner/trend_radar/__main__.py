import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings  # noqa: E402,F401  (loads .env)

from .cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
