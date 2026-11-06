"""python -m zeitnot: serve the app on 127.0.0.1 and open the browser."""

import argparse
import os
import threading
import webbrowser

import uvicorn

from .config import Settings
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="zeitnot")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ZEITNOT_PORT", "8000")))
    parser.add_argument(
        "--no-browser", action="store_true", default=os.environ.get("ZEITNOT_NO_BROWSER") == "1",
        help="do not open a browser tab (also ZEITNOT_NO_BROWSER=1)",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    app = create_app(settings)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"zeitnot on {url} ({'fake model' if settings.fake else 'Gemini'})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
