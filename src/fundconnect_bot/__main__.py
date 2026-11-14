"""Allow `python -m fundconnect_bot` execution."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
