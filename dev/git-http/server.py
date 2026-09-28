"""Git over http with basic auth, read-only (dev and integration tests).

Serves the repos under /fixtures via `git http-backend`. User `scheduler`,
password = the token from /secrets/git/token (scripts/dev-keys.sh).
"""

import base64
import binascii
import hmac
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = "/fixtures"
USER = "scheduler"
TOKEN = Path("/secrets/git/token").read_text().strip()


class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, USER) and hmac.compare_digest(password, TOKEN)

    def _reply(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        if not self._authorized():
            self._reply(401, [("WWW-Authenticate", 'Basic realm="git"')], b"")
            return
        url = urlsplit(self.path)
        if "git-receive-pack" in url.path or "git-receive-pack" in url.query:
            self._reply(403, [], b"read-only\n")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_PROJECT_ROOT": ROOT,
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": url.path,
            "QUERY_STRING": url.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": USER,
            "REMOTE_ADDR": self.client_address[0],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        if encoding := self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = encoding
        if protocol := self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = protocol
        out = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
        ).stdout
        sep = b"\r\n\r\n" if b"\r\n\r\n" in out else b"\n\n"
        head, _, payload = out.partition(sep)
        status, headers = 200, []
        for line in head.decode(errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.strip().split()[0])
            elif key:
                headers.append((key, value.strip()))
        self._reply(status, headers, payload)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
