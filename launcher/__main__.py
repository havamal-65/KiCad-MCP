"""`python -m launcher` — open the webview launcher window.

GUI entry point (imports pywebview via launcher.app). The core modules stay
import-safe; only launcher.app touches the webview.
"""

from __future__ import annotations

from launcher.app import main

if __name__ == "__main__":
    main()
