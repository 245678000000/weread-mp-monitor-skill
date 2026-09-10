#!/usr/bin/env python3
"""Small HTTP companion for weread-mp-monitor.

Run this only on infrastructure you control. Put it behind HTTPS and a private
network/reverse proxy. Authentication is a bearer token from MONITOR_API_TOKEN.
The WeRead cookie is read from WEREAD_COOKIE or WEREAD_COOKIE_FILE and is never
returned by the API.

Requires Python 3.9 or newer.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import weread_monitor as wm  # noqa: E402

HOST = os.environ.get("MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("MONITOR_PORT", "8787"))
TOKEN = os.environ.get("MONITOR_API_TOKEN", "")
STATE_PATH = os.environ.get("WEREAD_MONITOR_STATE", wm.DEFAULT_STATE_FILE)
AUTH_WARN_HOURS = float(os.environ.get("MONITOR_AUTH_WARN_HOURS", wm.DEFAULT_AUTH_WARN_HOURS))
MAX_BODY_BYTES = 1 << 20  # 1 MiB


class BadRequest(Exception):
    pass


def cookie_from_env() -> str:
    if os.environ.get("WEREAD_COOKIE_FILE"):
        return wm.read_cookie_file(os.environ["WEREAD_COOKIE_FILE"])
    cookie = os.environ.get("WEREAD_COOKIE", "").strip()
    if not cookie:
        raise wm.AuthError("WEREAD_COOKIE or WEREAD_COOKIE_FILE is required in service mode.")
    return cookie


class Handler(BaseHTTPRequestHandler):
    server_version = "weread-monitor/0.2"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            self._json(503, {"status": "error", "code": "token_not_configured"})
            return False
        supplied = self.headers.get("Authorization", "")
        # Constant-time comparison: a plain != leaks the token byte by byte.
        if not hmac.compare_digest(supplied, f"Bearer {TOKEN}"):
            self._json(401, {"status": "error", "code": "unauthorized"})
            return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise BadRequest("Invalid Content-Length") from exc
        if length < 0:
            raise BadRequest("Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise BadRequest(f"Request body exceeds {MAX_BODY_BYTES} bytes")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"Invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise BadRequest("Request body must be a JSON object")
        return data

    def _run(self, fn) -> None:
        try:
            self._json(200, fn())
        except BadRequest as exc:
            self._json(400, {"status": "error", "code": "bad_request", "message": str(exc)})
        except wm.AuthError as exc:
            # Route through the same throttle the CLI uses so a stuck login does
            # not produce one alert per scheduled run.
            with wm.state_lock(STATE_PATH):
                state = wm.load_state(STATE_PATH)
                payload = wm.auth_warning_payload(state, str(exc), AUTH_WARN_HOURS)
                wm.save_state(STATE_PATH, state)
            self._json(401, payload)
        except wm.AccountNotFound as exc:
            self._json(404, {"status": "error", "code": exc.code, "message": str(exc)})
        except wm.AmbiguousAccount as exc:
            self._json(409, {"status": "error", "code": exc.code, "candidates": exc.candidates})
        except wm.BatchNotFound as exc:
            self._json(404, {"status": "error", "code": exc.code, "message": str(exc)})
        except wm.StateError as exc:
            self._json(500, {"status": "error", "code": exc.code, "message": str(exc)})
        except wm.MonitorError as exc:
            self._json(502, {"status": "error", "code": exc.code, "message": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._json(500, {"status": "error", "code": "server_error", "message": str(exc)})

    # -- routes ----------------------------------------------------------

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "version": wm.__version__})
            return
        if not self._authorized():
            return
        if path == "/doctor":
            def doctor():
                accounts = wm.get_accounts(cookie_from_env())
                with wm.state_lock(STATE_PATH):
                    state = wm.load_state(STATE_PATH)
                    wm.clear_auth_warning(state)
                    wm.save_state(STATE_PATH, state)
                return {
                    "status": "ok",
                    "auth": "valid",
                    "followed_accounts": len(accounts),
                    "version": wm.__version__,
                }
            self._run(doctor)
        elif path == "/accounts":
            self._run(lambda: {"status": "ok", "accounts": wm.get_accounts(cookie_from_env())})
        elif path == "/monitors":
            def monitors():
                state = wm.load_state(STATE_PATH)
                return {"status": "ok", "monitors": [wm.monitor_view(v) for v in state["monitors"].values()]}
            self._run(monitors)
        else:
            self._json(404, {"status": "error", "code": "not_found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        path = urlparse(self.path).path

        if path == "/monitors":
            def add():
                data = self._body()
                cookie = cookie_from_env()
                with wm.state_lock(STATE_PATH):
                    state = wm.load_state(STATE_PATH)
                    result = wm.add_monitor(
                        cookie,
                        state,
                        str(data.get("name", "")),
                        [str(x) for x in data.get("keywords", [])],
                        int(data.get("baseline", 20)),
                        match_mode=str(data.get("match_mode", "any")),
                        match_body=bool(data.get("match_body", False)),
                        append_keywords=[str(x) for x in data.get("add_keywords", [])],
                        reset_baseline=bool(data.get("reset_baseline", False)),
                        clear_keywords=bool(data.get("clear_keywords", False)),
                    )
                    wm.clear_auth_warning(state)
                    wm.save_state(STATE_PATH, state)
                return result
            self._run(add)

        elif path == "/check":
            def check():
                data = self._body()
                cookie = cookie_from_env()
                no_mark = bool(data.get("no_mark", False))
                with wm.state_lock(STATE_PATH):
                    state = wm.load_state(STATE_PATH)
                    result = wm.check_monitors(
                        cookie,
                        state,
                        data.get("name"),
                        int(data.get("limit", 20)),
                        bool(data.get("with_body", False)),
                        int(data.get("body_chars", 4000)),
                        defer_ack=bool(data.get("defer_ack", False)),
                        mark_seen=not no_mark,
                    )
                    if result.get("auth_expired"):
                        result["auth_notice"] = wm.auth_warning_payload(
                            state,
                            "WeRead login expired during the run; scan to sign in again at weread.qq.com.",
                            AUTH_WARN_HOURS,
                        )
                    else:
                        wm.clear_auth_warning(state)
                    if not no_mark:
                        wm.save_state(STATE_PATH, state)
                    elif result.get("auth_expired"):
                        wm.save_state(STATE_PATH, state)
                return result
            self._run(check)

        elif path == "/ack":
            def ack():
                data = self._body()
                with wm.state_lock(STATE_PATH):
                    state = wm.load_state(STATE_PATH)
                    result = wm.ack_batch(state, data.get("batch_id"), bool(data.get("all", False)))
                    wm.save_state(STATE_PATH, state)
                return result
            self._run(ack)

        else:
            self._json(404, {"status": "error", "code": "not_found"})

    def do_DELETE(self):  # noqa: N802
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if not path.startswith("/monitors/"):
            self._json(404, {"status": "error", "code": "not_found"})
            return
        name = unquote(path[len("/monitors/"):])

        def remove():
            with wm.state_lock(STATE_PATH):
                state = wm.load_state(STATE_PATH)
                result = wm.remove_monitor(state, name)
                wm.save_state(STATE_PATH, state)
            return result
        self._run(remove)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> int:
    if not TOKEN:
        print("MONITOR_API_TOKEN is required", file=sys.stderr)
        return 2
    if len(TOKEN) < 24:
        print("MONITOR_API_TOKEN must be at least 24 characters", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"weread monitor service listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
