"""One-shot localhost receiver for a desktop OAuth 1.0a callback."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class OAuthCallbackServer:
    """Receives one registered loopback callback; never exposes a network port externally."""

    def __init__(self, callback_url: str, timeout_seconds: float = 600) -> None:
        parsed = urlparse(callback_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise ValueError("Automatic callback requires an http://127.0.0.1:<port>/... callback URL.")
        self.host, self.port, self.path = parsed.hostname, parsed.port, parsed.path
        self.timeout_seconds = timeout_seconds
        self.query: dict[str, list[str]] = {}

    def wait_for_verifier(self) -> str:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (HTTP method name)
                if urlparse(self.path).path != receiver.path:
                    self.send_error(404)
                    return
                receiver.query = parse_qs(urlparse(self.path).query)
                body = b"Authorization received. You may close this tab and return to the terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = HTTPServer((self.host, self.port), Handler)
        server.timeout = self.timeout_seconds
        try:
            server.handle_request()
        finally:
            server.server_close()
        verifier = receiver.query.get("oauth_verifier", [None])[0]
        if not verifier:
            raise TimeoutError("No OAuth verifier received before the callback timed out.")
        return verifier
