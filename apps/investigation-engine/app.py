"""Standalone development entry point for the Investigation Engine."""
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from investigation_app import create_app


app = create_app()


if __name__ == "__main__":
    config = app.config["IIE"]["server"]
    app.run(
        host=config.get("host", "127.0.0.1"),
        port=int(os.environ.get("PORT", config.get("port", 5002))),
        debug=False,
        threaded=True,
    )
