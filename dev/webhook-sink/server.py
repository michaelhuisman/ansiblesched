"""Webhook-ontvanger voor dev en integratietests (alleen stdlib).

POST /hook           ontvangt een webhook (faalt met 500 zolang fail_next > 0)
GET  /received       alle ontvangen webhooks: [{"headers": {...}, "body": {...}}]
POST /fail?n=2       laat de volgende n webhooks falen
POST /reset          wist alles
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

received: list[dict[str, object]] = []
fail_next = 0
attempts = 0
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/received":
            with lock:
                self._json(200, {"attempts": attempts, "received": received})
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        global fail_next, attempts
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with lock:
            if url.path == "/hook":
                attempts += 1
                if fail_next > 0:
                    fail_next -= 1
                    self._json(500, {"error": "sink told to fail"})
                    return
                received.append(
                    {"path": self.path, "headers": dict(self.headers), "raw": raw.decode()}
                )
                self._json(200, {"ok": True})
            elif url.path == "/fail":
                fail_next = int(parse_qs(url.query).get("n", ["1"])[0])
                self._json(200, {"fail_next": fail_next})
            elif url.path == "/reset":
                received.clear()
                fail_next = attempts = 0
                self._json(200, {})
            else:
                self._json(404, {})

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
