"""Start the API.

    python run_api.py                     # 127.0.0.1:8080
    python run_api.py --host 0.0.0.0      # reachable on the LAN or through a tunnel
    python run_api.py --reload            # develop against it

Named `run_api.py` to sit beside the desk's `run_paper.py`: in this repo the file called
`run_*` is the thing you start, and the module called `*_app` is the thing it starts.
"""

from __future__ import annotations

import argparse
import logging
import sys

import api_config
import api_paths

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _logging(level: str) -> None:
    """Log to stderr and to `logs/api.log`.

    The file is the point: the OTP delivery failures and the audit-worthy refusals happen
    in a background task, so a console that scrolled away is not a record.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    file = logging.FileHandler(api_paths.LOG_DIR / "api.log", encoding="utf-8")
    file.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers = [stream, file]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=api_config.HOST)
    ap.add_argument("--port", type=int, default=api_config.PORT)
    ap.add_argument("--reload", action="store_true", help="restart on source changes")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    _logging(args.log_level)

    # The dev bypass returns sign-in codes in the HTTP response. Off loopback that is not
    # a debugging aid, it is an open door: anyone who can reach the port can sign in as
    # any allowed user. Refusing to start is the only honest response -- a warning would
    # be read once and then scrolled past.
    if api_config.DEV_ECHO_OTP and args.host not in LOOPBACK:
        raise SystemExit(
            f"refusing to bind {args.host} with API_DEV_ECHO_OTP set.\n"
            "That flag returns the sign-in code to the caller. Unset it, or bind 127.0.0.1."
        )

    for line in api_config.startup_banner():
        print(line)
    print(f"  listening    http://{args.host}:{args.port}   (docs at /docs)")

    import uvicorn

    uvicorn.run("api_app:app", host=args.host, port=args.port, reload=args.reload,
                log_level=args.log_level, access_log=True)


if __name__ == "__main__":
    main()
