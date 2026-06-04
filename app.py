from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from invest_tax_calc.email_import.oauth import (
    GMAIL_OAUTH,
    _code_challenge,
    _new_code_verifier,
    build_authorization_url,
    exchange_code_for_token,
)
from invest_tax_calc.email_import.providers import Attachment, GmailClient
from invest_tax_calc.email_import.scopes import GMAIL_READONLY_SCOPE
from invest_tax_calc.email_import.storage import AttachmentStore, ImportLedger
from invest_tax_calc.t212 import parse_trading212_csv
from invest_tax_calc.tax import analyze_transactions, parse_rate_table, plan_sale


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
GMAIL_CALLBACK_PATH = "/oauth/gmail/callback"
GMAIL_DEFAULT_QUERY = "from:trading212 has:attachment filename:csv"
DEV_RELOAD_TOKEN = f"{os.getpid()}:{time.time_ns()}"
GMAIL_IMPORT_LOCK = threading.Lock()
GMAIL_IMPORTS: dict[str, "GmailImportSession"] = {}
WATCH_SUFFIXES = {".py", ".html", ".css", ".js", ".toml"}
WATCH_SKIP_DIRS = {
    ".git",
    ".invest_tax_calc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "imports",
}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class GmailImportSession:
    state: str
    code_verifier: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    query: str
    max_messages: int
    output: Path
    ledger: Path
    status: str = "waiting"
    message: str = "Waiting for Google approval."
    saved: int = 0
    skipped: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    csv_files: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)


def _resolve_project_path(value: str) -> Path:
    raw = Path(value.strip() or ".")
    target = raw if raw.is_absolute() else ROOT / raw
    resolved = target.resolve()
    project_root = ROOT.resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise AppError("Import paths must stay inside this project.")
    return resolved


class Handler(BaseHTTPRequestHandler):
    server_version = "InvestTaxCalc/0.1"

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
                return

            if path == GMAIL_CALLBACK_PATH:
                self._handle_gmail_callback(parsed)
                return

            if path == "/api/email/gmail/status":
                self._send_json(self._handle_gmail_status(parsed))
                return

            if path == "/api/dev/reload-state":
                self._send_json(
                    {
                        "enabled": os.getenv("INVEST_TAX_CALC_RELOAD_CHILD") == "1",
                        "token": DEV_RELOAD_TOKEN,
                    }
                )
                return

            if path.startswith("/static/"):
                rel = path.removeprefix("/static/")
                target = (STATIC_DIR / rel).resolve()
                if not str(target).startswith(str(STATIC_DIR.resolve())):
                    self._send_json({"error": "Invalid path"}, status=403)
                    return
                self._send_file(target)
                return

            self._send_json({"error": "Not found"}, status=404)
        except AppError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json({"error": f"Unexpected error: {exc}"}, status=500)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/analyze":
                payload = self._read_json()
                result = self._handle_analyze(payload)
                self._send_json(result)
                return

            if self.path == "/api/plan":
                payload = self._read_json()
                result = self._handle_plan(payload)
                self._send_json(result)
                return

            if self.path == "/api/email/gmail/start":
                payload = self._read_json()
                result = self._handle_gmail_start(payload)
                self._send_json(result)
                return

            self._send_json({"error": "Not found"}, status=404)
        except AppError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json({"error": f"Unexpected error: {exc}"}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _handle_analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AppError("Upload a Trading 212 CSV first.")

        rates = parse_rate_table(str(payload.get("rates") or ""))
        default_currency = str(payload.get("defaultCurrency") or "CZK")
        tax_year = int(payload.get("taxYear") or 0) or None
        as_of = str(payload.get("asOf") or "")

        transactions = parse_trading212_csv(
            content,
            filename=str(payload.get("filename") or "trading212.csv"),
            default_currency=default_currency,
        )
        if not transactions:
            raise AppError("No Trading 212 buy or sell orders were found in this CSV.")

        return analyze_transactions(
            transactions,
            rates=rates,
            tax_year=tax_year,
            as_of=as_of or None,
        )

    def _handle_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AppError("Upload a Trading 212 CSV first.")

        rates = parse_rate_table(str(payload.get("rates") or ""))
        transactions = parse_trading212_csv(
            content,
            filename=str(payload.get("filename") or "trading212.csv"),
            default_currency=str(payload.get("defaultCurrency") or "CZK"),
        )

        return plan_sale(
            transactions,
            rates=rates,
            instrument_key=str(payload.get("instrumentKey") or ""),
            quantity=str(payload.get("quantity") or ""),
            price_per_share_czk=str(payload.get("pricePerShareCzk") or ""),
            sale_date=str(payload.get("saleDate") or ""),
        )

    def _handle_gmail_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_id = str(payload.get("clientId") or "").strip() or os.getenv("GMAIL_CLIENT_ID", "").strip()
        if not client_id:
            raise AppError("Missing Gmail OAuth client ID. Paste it here or set GMAIL_CLIENT_ID.")

        client_secret = str(payload.get("clientSecret") or "").strip() or os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        query = str(payload.get("query") or "").strip() or GMAIL_DEFAULT_QUERY
        try:
            max_messages = int(payload.get("maxMessages") or 200)
        except (TypeError, ValueError) as exc:
            raise AppError("Gmail max messages must be a number.") from exc
        if max_messages < 1 or max_messages > 500:
            raise AppError("Gmail max messages must be between 1 and 500.")

        output = _resolve_project_path(str(payload.get("output") or "imports/email_reports"))
        ledger = _resolve_project_path(str(payload.get("ledger") or ".invest_tax_calc/email_import_ledger.json"))
        state = secrets.token_urlsafe(32)
        verifier = _new_code_verifier()
        redirect_uri = f"{self._base_url()}{GMAIL_CALLBACK_PATH}"
        auth_url = build_authorization_url(
            GMAIL_OAUTH,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=_code_challenge(verifier),
        )
        session = GmailImportSession(
            state=state,
            code_verifier=verifier,
            client_id=client_id,
            client_secret=client_secret or None,
            redirect_uri=redirect_uri,
            query=query,
            max_messages=max_messages,
            output=output,
            ledger=ledger,
        )
        with GMAIL_IMPORT_LOCK:
            GMAIL_IMPORTS[state] = session

        return {
            "state": state,
            "authUrl": auth_url,
            "requestedScope": GMAIL_READONLY_SCOPE,
            "status": session.status,
            "message": session.message,
        }

    def _handle_gmail_status(self, parsed: urllib.parse.ParseResult) -> dict[str, Any]:
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if not state:
            raise AppError("Missing Gmail import state.")
        session = _get_gmail_session(state)
        return _gmail_session_payload(session)

    def _handle_gmail_callback(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if not state:
            self._send_html("Missing Gmail import state.", status=400)
            return

        try:
            session = _get_gmail_session(state)
        except AppError:
            self._send_html("Unknown Gmail import session.", status=404)
            return

        error = (params.get("error") or [""])[0]
        if error:
            _update_gmail_session(
                session.state,
                status="error",
                error=error,
                message=f"Google denied the connection: {error}",
            )
            self._send_html("Connection failed. Return to Invest Tax Calc.")
            return

        code = (params.get("code") or [""])[0]
        if not code:
            _update_gmail_session(
                session.state,
                status="error",
                error="Missing OAuth code.",
                message="Google did not return an authorization code.",
            )
            self._send_html("Connection failed. Return to Invest Tax Calc.", status=400)
            return

        _update_gmail_session(
            session.state,
            status="importing",
            message="Connected. Importing Gmail attachments.",
        )
        thread = threading.Thread(
            target=_import_gmail_attachments,
            args=(session.state, code),
            daemon=True,
        )
        thread.start()
        self._send_html("Connected. Importing attachments. You can close this tab.")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            raise AppError("Missing request body.")
        if length > 25 * 1024 * 1024:
            raise AppError("Upload is too large for this local MVP.")

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AppError("Expected a JSON object.")
        return payload

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "Not found"}, status=404)
            return

        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, message: str, status: int = 200) -> None:
        html = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>Invest Tax Calc Gmail</title>"
            "<body style=\"font-family:system-ui,sans-serif;padding:32px\">"
            f"<h1>{_escape_html(message)}</h1>"
            "<p>Return to the Invest Tax Calc tab.</p>"
            "</body>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _base_url(self) -> str:
        host = self.headers.get("Host")
        if not host:
            server_host, server_port = self.server.server_address[:2]
            host = f"{server_host}:{server_port}"
        return f"http://{host}"


def _get_gmail_session(state: str) -> GmailImportSession:
    with GMAIL_IMPORT_LOCK:
        session = GMAIL_IMPORTS.get(state)
    if not session:
        raise AppError("Unknown Gmail import state.", status=404)
    return session


def _update_gmail_session(state: str, **changes: Any) -> None:
    with GMAIL_IMPORT_LOCK:
        session = GMAIL_IMPORTS.get(state)
        if not session:
            return
        for key, value in changes.items():
            setattr(session, key, value)


def _gmail_session_payload(session: GmailImportSession) -> dict[str, Any]:
    with GMAIL_IMPORT_LOCK:
        return {
            "state": session.state,
            "status": session.status,
            "message": session.message,
            "query": session.query,
            "maxMessages": session.max_messages,
            "output": str(session.output),
            "ledger": str(session.ledger),
            "saved": session.saved,
            "skipped": session.skipped,
            "files": list(session.files),
            "csvFiles": list(session.csv_files),
            "error": session.error,
        }


def _import_gmail_attachments(state: str, code: str) -> None:
    session = _get_gmail_session(state)
    try:
        token = exchange_code_for_token(
            GMAIL_OAUTH,
            client_id=session.client_id,
            client_secret=session.client_secret,
            code=code,
            code_verifier=session.code_verifier,
            redirect_uri=session.redirect_uri,
        )
        granted_scope = token.get("scope")
        if granted_scope:
            GMAIL_OAUTH.scope_policy.validate_granted(granted_scope)

        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError("OAuth token response did not include an access token.")

        ledger = ImportLedger(session.ledger)
        store = AttachmentStore(session.output, ledger)
        client = GmailClient(access_token)
        files: list[dict[str, Any]] = []
        csv_files: list[dict[str, Any]] = []
        saved = 0
        skipped = 0

        attachments = client.iter_report_attachments(
            query=session.query,
            max_messages=session.max_messages,
        )
        for attachment in attachments:
            result = store.save_attachment(attachment)
            if result.saved:
                saved += 1
            else:
                skipped += 1

            files.append(
                {
                    "filename": attachment.filename,
                    "path": str(result.path),
                    "saved": result.saved,
                    "sha256": result.sha256,
                    "contentType": attachment.content_type,
                }
            )
            csv_payload = _csv_payload(attachment, result.path)
            if csv_payload:
                csv_files.append(csv_payload)

        _update_gmail_session(
            state,
            status="done",
            message=f"Done. Saved {saved}; skipped {skipped} duplicate(s).",
            saved=saved,
            skipped=skipped,
            files=files,
            csv_files=csv_files,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network/provider boundary
        _update_gmail_session(
            state,
            status="error",
            message=f"Gmail import failed: {exc}",
            error=str(exc),
        )


def _csv_payload(attachment: Attachment, path: Path) -> dict[str, Any] | None:
    filename = attachment.filename.lower()
    content_type = (attachment.content_type or "").lower()
    if not filename.endswith(".csv") and "csv" not in content_type:
        return None
    if len(attachment.data) > 10 * 1024 * 1024:
        return {
            "filename": attachment.filename,
            "path": str(path),
            "content": "",
            "tooLarge": True,
        }

    for encoding in ("utf-8-sig", "utf-16", "cp1250", "latin-1"):
        try:
            content = attachment.data.decode(encoding)
            return {
                "filename": attachment.filename,
                "path": str(path),
                "content": content,
                "tooLarge": False,
            }
        except UnicodeDecodeError:
            continue
    return None


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def run_server(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Invest Tax Calc running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


def run_reloader(args: argparse.Namespace) -> None:
    print(f"Hot reload watching at http://{args.host}:{args.port}")
    snapshot = _watch_snapshot()
    child: subprocess.Popen[bytes] | None = None
    try:
        while True:
            child = _start_reload_child(args)
            restarting = False
            while child.poll() is None:
                time.sleep(args.reload_interval)
                next_snapshot = _watch_snapshot()
                if next_snapshot != snapshot:
                    snapshot = next_snapshot
                    restarting = True
                    print("Change detected. Restarting server.")
                    _stop_child(child)
                    break
            if not restarting:
                code = child.returncode or 0
                if code:
                    raise SystemExit(code)
                return
    except KeyboardInterrupt:
        print("\nStopping hot reload.")
    finally:
        if child and child.poll() is None:
            _stop_child(child)


def _start_reload_child(args: argparse.Namespace) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["INVEST_TAX_CALC_RELOAD_CHILD"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    return subprocess.Popen(command, cwd=str(ROOT), env=env)


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _watch_snapshot() -> dict[str, int]:
    snapshot: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if name not in WATCH_SKIP_DIRS]
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            if path.suffix.lower() not in WATCH_SUFFIXES:
                continue
            try:
                snapshot[str(path)] = path.stat().st_mtime_ns
            except OSError:
                continue
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Invest Tax Calc app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--reload", action="store_true", help="Restart the server when project files change.")
    parser.add_argument("--reload-interval", default=0.8, type=float, help="Hot-reload polling interval in seconds.")
    args = parser.parse_args()

    if args.reload and os.getenv("INVEST_TAX_CALC_RELOAD_CHILD") != "1":
        run_reloader(args)
        return

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
