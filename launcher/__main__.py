"""`python -m launcher` — build the launcher window and run the Tk loop.

GUI-only entry point (imports tkinter via launcher.app). The core modules stay
import-safe; only this module and launcher.app touch Tk.
"""

from __future__ import annotations

import tkinter as tk

from launcher.app import LauncherApp
from launcher.config import load_config


def main() -> None:
    cfg = load_config()
    root = tk.Tk()
    LauncherApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
