"""Shared pytest setup.

The repo ships no packaging (KTD9), so the package is imported from the
checkout itself: put the repo root first on sys.path before any test runs.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
