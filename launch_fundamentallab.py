"""Launch the bundled Streamlit application from the Windows executable."""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from streamlit.web import cli as stcli


def _open_browser() -> None:
    time.sleep(2.5)
    webbrowser.open("http://localhost:8501")


if __name__ == "__main__":
    # Keep the SQLite database beside the downloaded executable, not in the
    # temporary extraction folder used by PyInstaller's one-file mode.
    os.chdir(Path(sys.executable).resolve().parent)
    app_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "app.py"
    threading.Thread(target=_open_browser, daemon=True).start()
    sys.argv = ["streamlit", "run", str(app_path), "--server.headless=true"]
    stcli.main()
