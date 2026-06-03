from __future__ import annotations

import os
import sys
from pathlib import Path


base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
tcl_root = base_dir / "tcl"
if tcl_root.exists():
    os.environ.setdefault("TCL_LIBRARY", str(tcl_root / "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", str(tcl_root / "tk8.6"))
