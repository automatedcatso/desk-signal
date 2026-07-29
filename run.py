"""Run Signal Desk locally on one port."""
from __future__ import annotations

import argparse

from werkzeug.serving import run_simple

from portal import app


def main():
    parser = argparse.ArgumentParser(description="Run Signal Desk locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args()
    run_simple(args.host, args.port, app, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
