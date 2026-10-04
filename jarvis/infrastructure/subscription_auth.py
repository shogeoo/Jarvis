"""Independent ChatGPT OAuth session for Jarvis's direct model transport."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_ISSUER = "https://auth.openai.com"
_CALLBACK = "http://localhost:1455/auth/callback"


def _account_id(jwt: str) -> str:
    try:
        segment = jwt.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
        nested = payload.get("https://api.openai.com/auth") or {}
        value = payload.get("chatgpt_account_id") or nested.get("chatgpt_account_id")
        if isinstance(value, str) and value:
            return value
    except (IndexError, ValueError, TypeError):
        pass
    raise ValueError("ChatGPT authorization did not contain an account identifier")


class SubscriptionAuth:
    """Keep OAuth credentials under .jarvis; never reuse Codex CLI's auth.json."""

    def __init__(self, root: Path, *, session=None):
        self.path = Path(root) / "runtime" / "auth" / "chatgpt.json"
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._state = None

    def _read(self):
        if self._state is not None:
            return self._state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (
            not isinstance(state, dict)
            or not all(
                isinstance(state.get(key), str) and state[key]
                for key in ("access_token", "refresh_token", "account_id")
            )
            or not isinstance(state.get("expires_at"), (int, float))
        ):
            raise ValueError(
                f"Invalid saved ChatGPT authorization: {self.path}; original preserved"
            )
        self._state = state
        return state

    def _save(self, tokens: dict, *, previous: dict | None = None) -> dict:
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token") or (previous or {}).get("refresh_token")
        account = (
            tokens.get("account_id")
            or (previous or {}).get("account_id")
            or _account_id(tokens.get("id_token") or access or "")
        )
        if not access or not refresh:
            raise ValueError(
                "ChatGPT authorization did not return both access and refresh tokens"
            )
        state = {
            "access_token": access,
            "refresh_token": refresh,
            "account_id": account,
            "expires_at": time.time() + int(tokens.get("expires_in") or 3600),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._state = state
        return state

    def _post_token(self, fields: dict[str, str]) -> dict:
        response = self.session.post(_ISSUER + "/oauth/token", data=fields, timeout=30)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("ChatGPT token endpoint returned invalid JSON")
        return value

    def _refresh(self, state: dict) -> dict:
        tokens = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": state["refresh_token"],
                "client_id": _CLIENT_ID,
            }
        )
        return self._save(tokens, previous=state)

    def credentials(self, *, force_refresh: bool = False) -> dict:
        with self._lock:
            state = self._read()
            if state is None:
                raise RuntimeError(
                    "Jarvis has no ChatGPT authorization; sign in before starting agents"
                )
            if force_refresh or state["expires_at"] <= time.time() + 60:
                state = self._refresh(state)
            return dict(state)

    def ensure_login(self, announce) -> None:
        with self._lock:
            state = self._read()
            if state is not None:
                self.credentials()
                return
            announce("Войдите в аккаунт ChatGPT в открывшемся браузере.")
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            expected_state = secrets.token_urlsafe(24)
            result: dict[str, str] = {}
            done = threading.Event()

            class Callback(BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed = urlsplit(self.path)
                    if parsed.path != "/auth/callback":
                        self.send_error(404)
                        return
                    fields = parse_qs(parsed.query)
                    if fields.get("state", [None])[0] != expected_state:
                        self.send_error(400, "OAuth state mismatch")
                        return
                    result["code"] = fields.get("code", [""])[0]
                    result["error"] = fields.get(
                        "error_description", fields.get("error", [""])
                    )[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        (
                            "Вход завершён. Вернитесь к Jarvis."
                            if result["code"]
                            else "Ошибка входа. Вернитесь к Jarvis."
                        ).encode()
                    )
                    done.set()

                def log_message(self, format, *args):
                    return

            with HTTPServer(("localhost", 1455), Callback) as server:
                params = {
                    "response_type": "code",
                    "client_id": _CLIENT_ID,
                    "redirect_uri": _CALLBACK,
                    "scope": "openid profile email offline_access",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "state": expected_state,
                    "originator": "jarvis",
                }
                url = _ISSUER + "/oauth/authorize?" + urlencode(params)
                if not webbrowser.open(url):
                    announce("Браузер не открылся. Откройте ссылку для входа: " + url)
                server.timeout = 0.5
                while not done.is_set():
                    server.handle_request()
            if not result.get("code"):
                raise RuntimeError(
                    "ChatGPT login failed: "
                    + (result.get("error") or "no authorization code")
                )
            tokens = self._post_token(
                {
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "redirect_uri": _CALLBACK,
                    "client_id": _CLIENT_ID,
                    "code_verifier": verifier,
                }
            )
            self._save(tokens)
            announce("Вход в ChatGPT завершён.")

    def close(self) -> None:
        self.session.close()
