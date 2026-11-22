#!/usr/bin/env python3
"""Entry point kept for compatibility: `python3 grizly.py pull ...` works as in 1.x.

The code lives in the `grizly/` package next to this file (standard library
only, no pip install). Copy the whole folder, not just this file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from grizly.cli import main

if __name__ == "__main__":
    sys.exit(main())
