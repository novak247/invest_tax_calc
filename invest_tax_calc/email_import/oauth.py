from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Mapping

from .scopes import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPE_POLICY,
    ScopePolicy,
)


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    authorization_url: str
    token_url: str
    default_scopes: tuple[str, ...]
    scope_policy: ScopePolicy
    client_id_env: str
    client_secret_env: str | None = None
    authorization_params: Mapping[str, str] = field(default_factory=dict)


GMAIL_OAUTH = OAuthProvider(
    name="gmail",
    authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    default_scopes=(GMAIL_READONLY_SCOPE,),
    scope_policy=GMAIL_SCOPE_POLICY,
    client_id_env="GMAIL_CLIENT_ID",
    client_secret_env="GMAIL_CLIENT_SECRET",
    authorization_params={
        "access_type": "offline",
        "include_granted_scopes": "false",
        "prompt": "consent select_account",
    },
)

def build_authorization_url(
    provider: OAuthProvider,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    provider.scope_policy.validate_requested(provider.default_scopes)
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(provider.default_scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **provider.authorization_params,
    }
    return f"{provider.authorization_url}?{urllib.parse.urlencode(query)}"


def run_loopback_oauth(
    provider: OAuthProvider,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    callback_path: str = "/oauth/callback",
    open_browser: bool = True,
) -> dict:
    resolved_client_id = client_id or os.getenv(provider.client_id_env)
    if not resolved_client_id:
        raise RuntimeError(
            f"Missing OAuth client ID. Set {provider.client_id_env} or pass --client-id."
        )

    resolved_client_secret = client_secret
    if resolved_client_secret is None and provider.client_secret_env:
        resolved_client_secret = os.getenv(provider.client_secret_env)

    verifier = _new_code_verifier()
    challenge = _code_challenge(verifier)
    state = secrets.token_urlsafe(32)

    server = _OAuthCallbackServer((host, port), _OAuthCallbackHandler)
    try:
        redirect_uri = f"http://{host}:{server.server_port}{callback_path}"
        auth_url = build_authorization_url(
            provider,
            client_id=resolved_client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        )

        print("Open this URL to connect your mailbox read-only:")
        print(auth_url)
        if open_browser:
            webbrowser.open(auth_url)

        server.handle_request()
        if server.error:
            raise RuntimeError(f"OAuth failed: {server.error}")
        if not server.code:
            raise RuntimeError("OAuth callback did not include an authorization code.")
        if server.state != state:
            raise RuntimeError("OAuth callback state mismatch.")

        token = exchange_code_for_token(
            provider,
            client_id=resolved_client_id,
            client_secret=resolved_client_secret,
            code=server.code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
        )
        granted_scope = token.get("scope")
        if granted_scope:
            provider.scope_policy.validate_granted(granted_scope)
        return token
    finally:
        server.server_close()


def exchange_code_for_token(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    payload = {
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        provider.token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_access_token(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
) -> dict:
    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        provider.token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _OAuthCallbackServer(HTTPServer):
    code: str | None = None
    state: str | None = None
    error: str | None = None


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.code = _first(params.get("code"))  # type: ignore[attr-defined]
        self.server.state = _first(params.get("state"))  # type: ignore[attr-defined]
        self.server.error = _first(params.get("error"))  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        message = (
            "Connected. You can close this tab and return to the importer."
            if not self.server.error  # type: ignore[attr-defined]
            else "Connection failed. Return to the importer for details."
        )
        self.wfile.write(message.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return encoded.rstrip("=")
