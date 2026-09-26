"""A deliberately small, authenticated local bridge for the Chrome companion.

The bridge is disabled by default.  It never exposes API keys and it accepts
only two explicit, user-approved requests; arbitrary commands are rejected.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


class BrowserBridge:
    HOST = "127.0.0.1"
    DEFAULT_PORT = 8766
    PAIRING_TTL_SECONDS = 300
    MAX_CONTEXT_CHARS = 12000
    ALLOWED_REQUESTS = {"summarize_page", "create_word_report"}

    def __init__(self, settings_path: Path, on_request: Callable[[dict[str, Any]], None] | None = None):
        self.settings_path = Path(settings_path)
        self.on_request = on_request
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pairing_code = ""
        self._pairing_expires = 0.0
        self._lock = threading.Lock()

    def _settings(self) -> dict[str, Any]:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self, settings: dict[str, Any]) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def is_enabled(self) -> bool:
        return bool(self._settings().get("browser_bridge_enabled", False))

    def issue_pairing_code(self) -> str:
        """Generate a short-lived code shown only in the desktop UI."""
        with self._lock:
            self._pairing_code = secrets.token_urlsafe(9)
            self._pairing_expires = time.time() + self.PAIRING_TTL_SECONDS
            settings = self._settings()
            settings["browser_bridge_pairing_code"] = self._pairing_code
            settings["browser_bridge_pairing_expires"] = self._pairing_expires
            self._save_settings(settings)
            return self._pairing_code

    def revoke_pairings(self) -> None:
        settings = self._settings()
        settings["browser_bridge_tokens"] = []
        self._save_settings(settings)
        with self._lock:
            self._pairing_code = ""
            self._pairing_expires = 0.0

    def _pair(self, code: str) -> dict[str, Any]:
        settings = self._settings()
        with self._lock:
            stored_code = self._pairing_code or str(settings.get("browser_bridge_pairing_code", ""))
            expires = self._pairing_expires or float(settings.get("browser_bridge_pairing_expires", 0) or 0)
            valid = bool(stored_code) and time.time() < expires and secrets.compare_digest(code, stored_code)
            if valid:
                self._pairing_code = ""
                settings.pop("browser_bridge_pairing_code", None)
                settings.pop("browser_bridge_pairing_expires", None)
        if not valid:
            return {"ok": False, "error": "Pairing code is invalid or expired."}
        token = secrets.token_urlsafe(32)
        tokens = list(settings.get("browser_bridge_tokens", []))[-4:]
        tokens.append(token)
        settings["browser_bridge_tokens"] = tokens
        self._save_settings(settings)
        return {"ok": True, "token": token}

    def _authorized(self, token: str) -> bool:
        return bool(token) and any(secrets.compare_digest(token, item) for item in self._settings().get("browser_bridge_tokens", []) if isinstance(item, str))

    def _submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = str(payload.get("request", ""))
        if request not in self.ALLOWED_REQUESTS:
            return {"ok": False, "error": "That browser request is not allowed."}
        if payload.get("user_approved") is not True:
            return {"ok": False, "error": "Confirm sharing in the Chrome companion first."}
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        safe_context = {
            "request": request,
            "title": str(context.get("title", ""))[:500],
            "url": str(context.get("url", ""))[:2000],
            "selection": str(context.get("selection", ""))[:self.MAX_CONTEXT_CHARS],
            "content": str(context.get("content", ""))[:self.MAX_CONTEXT_CHARS],
        }
        if not (safe_context["selection"] or safe_context["content"]):
            return {"ok": False, "error": "No page text was provided."}
        if self.on_request:
            threading.Thread(target=self.on_request, args=(safe_context,), daemon=True, name="BrahmaBrowserRequest").start()
        return {"ok": True, "status": "Sent to Brahma Echo for desktop confirmation."}

    def start(self) -> bool:
        if not self.is_enabled() or self.is_running():
            return self.is_running()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, data: dict[str, Any]):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "chrome-extension://*")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "chrome-extension://*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def do_GET(self):
                if self.path == "/v1/health":
                    self._json(200, {"ok": True, "enabled": bridge.is_enabled(), "paired": bool(bridge._settings().get("browser_bridge_tokens", []))})
                else:
                    self._json(404, {"ok": False, "error": "Not found"})

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 30000:
                        raise ValueError("Request is too large")
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a JSON object")
                except Exception as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                if self.path == "/v1/pair":
                    self._json(200, bridge._pair(str(payload.get("code", ""))))
                    return
                if self.path == "/v1/context":
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                    if not bridge._authorized(token):
                        self._json(401, {"ok": False, "error": "Pair the extension first."})
                        return
                    self._json(200, bridge._submit(payload))
                    return
                self._json(404, {"ok": False, "error": "Not found"})

            def log_message(self, _format, *_args):
                return

        try:
            self._server = ThreadingHTTPServer((self.HOST, int(self._settings().get("browser_bridge_port", self.DEFAULT_PORT))), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="BrahmaBrowserBridge")
            self._thread.start()
            return True
        except OSError:
            self._server = None
            return False

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = None

    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()
