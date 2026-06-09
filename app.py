from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import hashlib
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
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from invest_tax_calc.email_import.oauth import (
    GMAIL_OAUTH,
    _code_challenge,
    _new_code_verifier,
    build_authorization_url,
    exchange_code_for_token,
    refresh_access_token,
)
from invest_tax_calc.email_import.providers import Attachment, GmailClient
from invest_tax_calc.email_import.scopes import GMAIL_READONLY_SCOPE
from invest_tax_calc.email_import.storage import AttachmentStore, ImportLedger
from invest_tax_calc.models import Money, Trade
from invest_tax_calc.planner import plan_sale_batch, plan_target_proceeds
from invest_tax_calc.prices import (
    InstrumentRef,
    PriceCache,
    PriceProvider,
    YahooPriceProvider,
    quote_instruments,
)
from invest_tax_calc.t212_pdf import parse_trading212_pdf_job
from invest_tax_calc.tax import analyze_transactions, parse_rate_table, plan_sale


class ReloadFriendlyHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
GMAIL_CALLBACK_PATH = "/oauth/gmail/callback"
GMAIL_DEFAULT_QUERY = "from:trading212 has:attachment filename:pdf"
APP_DATA_DIR = ROOT / ".invest_tax_calc"
GMAIL_CONFIG_PATH = APP_DATA_DIR / "gmail_oauth_client.json"
GMAIL_TOKEN_PATH = APP_DATA_DIR / "gmail_token.json"
PDF_PARSE_CACHE_DIR = APP_DATA_DIR / "parsed_pdf_cache"
PDF_PARSE_CACHE_VERSION = "trading212_pdf_v1"
PRICE_CACHE_PATH = APP_DATA_DIR / "price_cache.json"
PRICE_PROVIDER: PriceProvider = YahooPriceProvider()
DEV_RELOAD_TOKEN = f"{os.getpid()}:{time.time_ns()}"
# Bundling a full history of Trading 212 PDF statements (hundreds of distinct
# reports, base64-encoded) easily exceeds tens of MB in a single analyze POST.
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
GMAIL_FETCH_WORKERS = 8
PDF_PARSE_WORKERS = max(1, min(4, os.cpu_count() or 1))
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
class GmailOAuthConfig:
    client_id: str
    client_secret: str | None = None
    source: str = "local"


@dataclass
class GmailImportSession:
    state: str
    code_verifier: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    query: str
    max_messages: int
    reparse_cached_pdfs: bool
    output: Path
    ledger: Path
    purpose: str = "import"
    status: str = "waiting"
    message: str = "Waiting for Google approval."
    progress_phase: str = "waiting"
    total_messages: int = 0
    processed_messages: int = 0
    total_attachments: int = 0
    processed_attachments: int = 0
    saved: int = 0
    skipped: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    pdf_files: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)


def _resolve_project_path(value: str) -> Path:
    raw = Path(value.strip() or ".")
    target = raw if raw.is_absolute() else ROOT / raw
    resolved = target.resolve()
    project_root = ROOT.resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise AppError("Import paths must stay inside this project.")
    return resolved


def _looks_like_pdf(filename: str, content_type: str, encoding: str) -> bool:
    return (
        filename.lower().endswith(".pdf")
        or content_type.lower() == "application/pdf"
        or encoding.lower() == "base64"
    )


def _decode_upload_bytes(content: str, encoding: str) -> bytes:
    if encoding.lower() != "base64":
        return content.encode("utf-8")

    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppError("Invalid PDF upload encoding.") from exc


def _parse_pdf_report_jobs(
    jobs: list[tuple[bytes, str, str]],
    *,
    reparse_cached_pdfs: bool = False,
) -> list:
    if not jobs:
        return []

    parsed_batches: list[list[Trade] | None] = [None] * len(jobs)
    pending: list[tuple[int, bytes, str, str]] = []
    for index, (data, filename, digest) in enumerate(jobs):
        if not reparse_cached_pdfs:
            cached = _load_cached_pdf_trades(digest)
            if cached is not None:
                parsed_batches[index] = cached
                continue
        pending.append((index, data, filename, digest))

    parse_jobs = [(data, filename) for _index, data, filename, _digest in pending]
    if not parse_jobs:
        parsed_pending: list[list[Trade]] = []
    elif len(parse_jobs) == 1 or PDF_PARSE_WORKERS <= 1:
        parsed_pending = [parse_trading212_pdf_job(job) for job in parse_jobs]
    else:
        workers = min(PDF_PARSE_WORKERS, len(parse_jobs))
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
                parsed_pending = list(executor.map(parse_trading212_pdf_job, parse_jobs))
        except (OSError, RuntimeError, BrokenProcessPool):
            parsed_pending = [parse_trading212_pdf_job(job) for job in parse_jobs]

    for (index, _data, filename, digest), parsed in zip(pending, parsed_pending):
        parsed_batches[index] = parsed
        _save_cached_pdf_trades(digest, filename, parsed)

    transactions = []
    for parsed in parsed_batches:
        if parsed:
            transactions.extend(parsed)
    return transactions


def _pdf_cache_path(digest: str) -> Path:
    return PDF_PARSE_CACHE_DIR / PDF_PARSE_CACHE_VERSION / f"{digest}.json"


def _load_cached_pdf_trades(digest: str) -> list[Trade] | None:
    path = _pdf_cache_path(digest)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != PDF_PARSE_CACHE_VERSION or payload.get("sha256") != digest:
            return None
        trades = payload.get("trades")
        if not isinstance(trades, list):
            return None
        return [_trade_from_cache(item) for item in trades if isinstance(item, dict)]
    except (AttributeError, OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_cached_pdf_trades(digest: str, filename: str, trades: list[Trade]) -> None:
    path = _pdf_cache_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PDF_PARSE_CACHE_VERSION,
        "sha256": digest,
        "filename": filename,
        "trades": [_trade_to_cache(trade) for trade in trades],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _trade_to_cache(trade: Trade) -> dict[str, Any]:
    return {
        "source": trade.source,
        "source_id": trade.source_id,
        "action": trade.action,
        "kind": trade.kind,
        "traded_at": trade.traded_at.isoformat(),
        "instrument_key": trade.instrument_key,
        "ticker": trade.ticker,
        "isin": trade.isin,
        "name": trade.name,
        "quantity": str(trade.quantity),
        "gross": {
            "amount": str(trade.gross.amount),
            "currency": trade.gross.currency,
        },
        "fees": [
            {
                "amount": str(fee.amount),
                "currency": fee.currency,
            }
            for fee in trade.fees
        ],
        "raw": dict(trade.raw),
    }


def _trade_from_cache(payload: dict[str, Any]) -> Trade:
    gross = payload.get("gross") if isinstance(payload.get("gross"), dict) else {}
    fees = payload.get("fees") if isinstance(payload.get("fees"), list) else []
    return Trade(
        source=str(payload.get("source") or ""),
        source_id=str(payload.get("source_id") or ""),
        action=str(payload.get("action") or ""),
        kind=str(payload.get("kind") or ""),
        traded_at=datetime.fromisoformat(str(payload["traded_at"])),
        instrument_key=str(payload.get("instrument_key") or ""),
        ticker=str(payload.get("ticker") or ""),
        isin=str(payload.get("isin") or ""),
        name=str(payload.get("name") or ""),
        quantity=_decimal_from_cache(payload.get("quantity")),
        gross=Money(
            _decimal_from_cache(gross.get("amount")),
            str(gross.get("currency") or "CZK"),
        ),
        fees=tuple(
            Money(
                _decimal_from_cache(fee.get("amount") if isinstance(fee, dict) else None),
                str(fee.get("currency") or "CZK") if isinstance(fee, dict) else "CZK",
            )
            for fee in fees
        ),
        raw=_raw_from_cache(payload.get("raw")),
    )


def _decimal_from_cache(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _raw_from_cache(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(raw_value) for key, raw_value in value.items()}


def _parse_gmail_oauth_credentials(text: str) -> GmailOAuthConfig:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid Google credentials JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AppError("Google credentials JSON must be an object.")

    source = str(data.get("source") or "raw")
    client_data: dict[str, Any] = data
    if isinstance(data.get("installed"), dict):
        source = "installed"
        client_data = data["installed"]
    elif isinstance(data.get("web"), dict):
        source = "web"
        client_data = data["web"]

    client_id = str(client_data.get("client_id") or "").strip()
    client_secret = str(client_data.get("client_secret") or "").strip() or None
    if not client_id:
        raise AppError("Google credentials JSON is missing client_id.")

    return GmailOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        source=source,
    )


def _load_gmail_oauth_config() -> GmailOAuthConfig | None:
    if GMAIL_CONFIG_PATH.exists():
        return _parse_gmail_oauth_credentials(GMAIL_CONFIG_PATH.read_text(encoding="utf-8"))

    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    if client_id:
        return GmailOAuthConfig(
            client_id=client_id,
            client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip() or None,
            source="env",
        )
    return None


def _save_gmail_oauth_config(config: GmailOAuthConfig) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": config.client_id,
        "client_secret": config.client_secret or "",
        "source": config.source,
    }
    GMAIL_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _gmail_config_payload() -> dict[str, Any]:
    config = _load_gmail_oauth_config()
    if not config:
        return {
            "configured": False,
            "message": "Gmail setup is missing.",
            "tokenCached": False,
        }

    return {
        "configured": True,
        "message": "Gmail setup is ready.",
        "clientIdPreview": _preview_client_id(config.client_id),
        "source": config.source,
        "tokenCached": bool(_load_gmail_refresh_token(config)),
    }


def _preview_client_id(client_id: str) -> str:
    if len(client_id) <= 18:
        return client_id
    return f"{client_id[:8]}...{client_id[-10:]}"


def _load_gmail_refresh_token(config: GmailOAuthConfig) -> str | None:
    if not GMAIL_TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(GMAIL_TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("client_id") != config.client_id:
        return None
    token = str(data.get("refresh_token") or "").strip()
    return token or None


def _save_gmail_refresh_token(config: GmailOAuthConfig, token: dict[str, Any]) -> None:
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not refresh_token:
        return

    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": config.client_id,
        "refresh_token": refresh_token,
        "scope": token.get("scope", ""),
        "saved_at_unix": time.time(),
    }
    GMAIL_TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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

            if path == "/api/email/gmail/config":
                self._send_json(_gmail_config_payload())
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

            if self.path == "/api/plan/batch":
                payload = self._read_json()
                result = self._handle_plan_batch(payload)
                self._send_json(result)
                return

            if self.path == "/api/plan/target-proceeds":
                payload = self._read_json()
                result = self._handle_plan_target(payload)
                self._send_json(result)
                return

            if self.path == "/api/prices/quote":
                payload = self._read_json()
                result = handle_price_quote(payload)
                self._send_json(result)
                return

            if self.path == "/api/email/gmail/start":
                payload = self._read_json()
                result = self._handle_gmail_start(payload)
                self._send_json(result)
                return

            if self.path == "/api/email/gmail/connect":
                payload = self._read_json()
                result = self._handle_gmail_connect(payload)
                self._send_json(result)
                return

            if self.path == "/api/email/gmail/cancel":
                payload = self._read_json()
                result = self._handle_gmail_cancel(payload)
                self._send_json(result)
                return

            if self.path == "/api/email/gmail/config":
                payload = self._read_json()
                result = self._handle_gmail_config_save(payload)
                self._send_json(result)
                return

            self._send_json({"error": "Not found"}, status=404)
        except AppError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json({"error": f"Unexpected error: {exc}"}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/api/dev/reload-state", "/api/email/gmail/status"}:
            return
        print(f"{self.address_string()} - {fmt % args}")

    def _handle_analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = parse_rate_table(str(payload.get("rates") or ""))
        tax_year = int(payload.get("taxYear") or 0) or None
        as_of = str(payload.get("asOf") or "")

        transactions = self._parse_report_transactions(payload)
        if not transactions:
            raise AppError("No Trading 212 buy or sell orders were found in this report.")

        return analyze_transactions(
            transactions,
            rates=rates,
            tax_year=tax_year,
            as_of=as_of or None,
        )

    def _handle_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = parse_rate_table(str(payload.get("rates") or ""))
        transactions = self._parse_report_transactions(payload)

        try:
            return plan_sale(
                transactions,
                rates=rates,
                instrument_key=str(payload.get("instrumentKey") or ""),
                quantity=str(payload.get("quantity") or ""),
                price_per_share_czk=str(payload.get("pricePerShareCzk") or ""),
                sale_date=str(payload.get("saleDate") or ""),
            )
        except ValueError as exc:
            raise AppError(str(exc)) from exc

    def _handle_plan_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = parse_rate_table(str(payload.get("rates") or ""))
        transactions = self._parse_report_transactions(payload)
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise AppError("Add at least one sale row to plan.")

        try:
            return plan_sale_batch(
                transactions,
                rates=rates,
                sale_date=str(payload.get("saleDate") or ""),
                rows=[row for row in rows if isinstance(row, dict)],
            )
        except ValueError as exc:
            raise AppError(str(exc)) from exc

    def _handle_plan_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = parse_rate_table(str(payload.get("rates") or ""))
        transactions = self._parse_report_transactions(payload)

        raw_keys = payload.get("candidateInstrumentKeys")
        candidate_keys = (
            [str(key) for key in raw_keys if str(key or "").strip()]
            if isinstance(raw_keys, list)
            else None
        )
        quotes = payload.get("quotes")
        if not isinstance(quotes, dict):
            quotes = {}

        try:
            return plan_target_proceeds(
                transactions,
                rates=rates,
                sale_date=str(payload.get("saleDate") or ""),
                target_proceeds_czk=str(payload.get("targetProceedsCzk") or ""),
                optimization_mode=str(payload.get("optimizationMode") or "min_tax"),
                candidate_instrument_keys=candidate_keys,
                quotes=quotes,
            )
        except ValueError as exc:
            raise AppError(str(exc)) from exc

    def _parse_report_transactions(self, payload: dict[str, Any]) -> list:
        jobs = self._pdf_report_jobs(payload)
        return _parse_pdf_report_jobs(
            jobs,
            reparse_cached_pdfs=bool(payload.get("reparseCachedPdfs")),
        )

    def _pdf_report_jobs(self, payload: dict[str, Any]) -> list[tuple[bytes, str, str]]:
        reports = payload.get("reports")
        jobs: list[tuple[bytes, str, str]] = []
        seen_hashes: set[str] = set()

        if isinstance(reports, list) and reports:
            for index, report in enumerate(reports, start=1):
                if not isinstance(report, dict):
                    raise AppError("Report uploads must be objects.")
                data, filename = self._read_pdf_report(report, fallback_name=f"statement-{index}.pdf")
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                jobs.append((data, filename, digest))
            return jobs

        data, filename = self._read_pdf_report(payload, fallback_name="trading212.pdf")
        return [(data, filename, hashlib.sha256(data).hexdigest())]

    def _read_pdf_report(self, payload: dict[str, Any], *, fallback_name: str) -> tuple[bytes, str]:
        content = str(payload.get("content") or "")
        filename = str(payload.get("filename") or fallback_name)
        content_type = str(payload.get("contentType") or "")
        encoding = str(payload.get("contentEncoding") or "text")
        path_text = str(payload.get("path") or payload.get("contentPath") or "")

        if not content.strip() and path_text.strip():
            path = _resolve_project_path(path_text)
            if not path.is_file():
                raise AppError("Imported PDF report was not found on disk.")
            filename = filename or path.name
            content_type = content_type or mimetypes.guess_type(path.name)[0] or ""
            if not _looks_like_pdf(filename, content_type, "file"):
                raise AppError("This app now expects Trading 212 PDF statements.")
            return path.read_bytes(), filename

        if not content.strip():
            raise AppError("Upload a Trading 212 PDF statement first.")

        if not _looks_like_pdf(filename, content_type, encoding):
            raise AppError("This app now expects Trading 212 PDF statements.")

        return _decode_upload_bytes(content, encoding), filename

    def _parse_one_pdf_report(self, payload: dict[str, Any], *, fallback_name: str) -> list:
        return parse_trading212_pdf_job(self._read_pdf_report(payload, fallback_name=fallback_name))

    def _handle_gmail_config_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_credentials = str(payload.get("credentialsJson") or "").strip()
        if not raw_credentials:
            raise AppError("Paste the Google OAuth credentials JSON first.")
        config = _parse_gmail_oauth_credentials(raw_credentials)
        _save_gmail_oauth_config(config)
        return _gmail_config_payload()

    def _handle_gmail_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = _load_gmail_oauth_config()
        if not config:
            raise AppError("Gmail is not set up yet. Save Google credentials.json once, then connect.")
        refresh_token = _load_gmail_refresh_token(config)
        if not refresh_token:
            raise AppError("Connect Gmail first, then fetch reports.")

        query = str(payload.get("query") or "").strip() or GMAIL_DEFAULT_QUERY
        try:
            max_messages = int(payload.get("maxMessages") or 500)
        except (TypeError, ValueError) as exc:
            raise AppError("Gmail max messages must be a number.") from exc
        if max_messages < 1 or max_messages > 500:
            raise AppError("Gmail max messages must be between 1 and 500.")

        reparse_cached_pdfs = bool(payload.get("reparseCachedPdfs"))
        output = _resolve_project_path(str(payload.get("output") or "imports/email_reports"))
        ledger = _resolve_project_path(str(payload.get("ledger") or ".invest_tax_calc/email_import_ledger.json"))
        state = secrets.token_urlsafe(32)
        session = GmailImportSession(
            state=state,
            code_verifier="",
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri="",
            query=query,
            max_messages=max_messages,
            reparse_cached_pdfs=reparse_cached_pdfs,
            output=output,
            ledger=ledger,
            purpose="import",
            status="importing",
            progress_phase="authorizing",
            message="Using saved Gmail login. Importing attachments.",
        )
        with GMAIL_IMPORT_LOCK:
            GMAIL_IMPORTS[state] = session

        thread = threading.Thread(
            target=_import_gmail_attachments_with_refresh_token,
            args=(state, refresh_token),
            daemon=True,
        )
        thread.start()
        return {
            "state": state,
            "purpose": "import",
            "authUrl": "",
            "requestedScope": GMAIL_READONLY_SCOPE,
            "status": "importing",
            "message": "Using saved Gmail login. Importing attachments.",
        }

    def _handle_gmail_connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = _load_gmail_oauth_config()
        if not config:
            raise AppError("Gmail is not set up yet. Save Google credentials.json once, then connect.")

        state = secrets.token_urlsafe(32)
        verifier = _new_code_verifier()
        redirect_uri = f"{self._base_url()}{GMAIL_CALLBACK_PATH}"
        session = GmailImportSession(
            state=state,
            code_verifier=verifier,
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri=redirect_uri,
            query=GMAIL_DEFAULT_QUERY,
            max_messages=0,
            reparse_cached_pdfs=False,
            output=_resolve_project_path("imports/email_reports"),
            ledger=_resolve_project_path(".invest_tax_calc/email_import_ledger.json"),
            purpose="connect",
            message="Waiting for Google approval.",
        )
        with GMAIL_IMPORT_LOCK:
            GMAIL_IMPORTS[state] = session

        auth_url = build_authorization_url(
            GMAIL_OAUTH,
            client_id=config.client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=_code_challenge(verifier),
        )
        return {
            "state": state,
            "purpose": "connect",
            "authUrl": auth_url,
            "requestedScope": GMAIL_READONLY_SCOPE,
            "status": session.status,
            "message": session.message,
        }

    def _handle_gmail_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = str(payload.get("state") or "").strip()
        if not state:
            raise AppError("Missing Gmail import state.")

        with GMAIL_IMPORT_LOCK:
            session = GMAIL_IMPORTS.get(state)
            if not session:
                raise AppError("Unknown Gmail import session.", status=404)
            if session.status not in {"done", "error", "canceled"}:
                session.cancel_requested = True
                session.status = "canceling"
                session.progress_phase = "canceling"
                session.message = "Stopping after current Gmail request(s)."
        return _gmail_session_payload(session)

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

        if session.cancel_requested or session.status == "canceled":
            self._send_html("Gmail action was canceled. Return to Invest Tax Calc.")
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
            status="authorizing",
            progress_phase="authorizing",
            message="Connected. Saving Gmail login.",
        )
        thread = threading.Thread(target=_connect_gmail, args=(session.state, code), daemon=True)
        thread.start()
        self._send_html("Connected. Gmail login is being saved. You can close this tab.")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            raise AppError("Missing request body.")
        if length > MAX_UPLOAD_BYTES:
            # Drain the incoming body first; otherwise the client is still
            # uploading when we respond and the reset connection surfaces in the
            # browser as an opaque "Failed to fetch" instead of this message.
            self._drain_request_body(length)
            megabytes = length // (1024 * 1024)
            raise AppError(
                f"Upload is too large for this local MVP ({megabytes} MB). "
                "Import fewer statements at once."
            )

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AppError("Expected a JSON object.")
        return payload

    def _drain_request_body(self, length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "Not found"}, status=404)
            return

        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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


def handle_price_quote(
    payload: dict[str, Any],
    *,
    provider: PriceProvider | None = None,
    cache: PriceCache | None = None,
) -> dict[str, Any]:
    raw_instruments = payload.get("instruments")
    if not isinstance(raw_instruments, list) or not raw_instruments:
        raise AppError("Send at least one instrument to quote.")

    instruments: list[InstrumentRef] = []
    for item in raw_instruments:
        if not isinstance(item, dict):
            raise AppError("Each instrument must be an object.")
        instrument_key = str(item.get("instrumentKey") or "").strip()
        if not instrument_key:
            raise AppError("Each instrument needs an instrumentKey.")
        instruments.append(
            InstrumentRef(
                instrument_key=instrument_key,
                ticker=str(item.get("ticker") or "").strip(),
                isin=str(item.get("isin") or "").strip(),
            )
        )

    rates = parse_rate_table(str(payload.get("rates") or ""))
    quotes, warnings, fx_rates = quote_instruments(
        instruments,
        provider=provider or PRICE_PROVIDER,
        rates=rates,
        cache=cache if cache is not None else PriceCache(PRICE_CACHE_PATH),
        force_refresh=bool(payload.get("forceRefresh")),
    )
    return {"quotes": quotes, "warnings": warnings, "fxRates": fx_rates}


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


def _gmail_cancel_requested(state: str) -> bool:
    with GMAIL_IMPORT_LOCK:
        session = GMAIL_IMPORTS.get(state)
        return bool(session and session.cancel_requested)


def _mark_gmail_canceled(state: str, *, message: str, progress_phase: str = "canceled") -> None:
    _update_gmail_session(
        state,
        status="canceled",
        progress_phase=progress_phase,
        message=message,
        error=None,
    )


def _gmail_session_payload(session: GmailImportSession) -> dict[str, Any]:
    with GMAIL_IMPORT_LOCK:
        return {
            "state": session.state,
            "purpose": session.purpose,
            "status": session.status,
            "message": session.message,
            "query": session.query,
            "maxMessages": session.max_messages,
            "reparseCachedPdfs": session.reparse_cached_pdfs,
            "cancelRequested": session.cancel_requested,
            "progress": {
                "phase": session.progress_phase,
                "totalMessages": session.total_messages,
                "processedMessages": session.processed_messages,
                "totalAttachments": session.total_attachments,
                "processedAttachments": session.processed_attachments,
                "saved": session.saved,
                "skipped": session.skipped,
            },
            "output": str(session.output),
            "ledger": str(session.ledger),
            "saved": session.saved,
            "skipped": session.skipped,
            "files": list(session.files),
            "pdfFiles": list(session.pdf_files) if session.status == "done" else [],
            "error": session.error,
        }


def _connect_gmail(state: str, code: str) -> None:
    session = _get_gmail_session(state)
    try:
        if _gmail_cancel_requested(state):
            _mark_gmail_canceled(
                state,
                message="Gmail connection was canceled.",
                progress_phase="canceled",
            )
            return

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

        _save_gmail_refresh_token(
            GmailOAuthConfig(
                client_id=session.client_id,
                client_secret=session.client_secret,
                source="session",
            ),
            token,
        )
        _update_gmail_session(
            state,
            status="done",
            progress_phase="done",
            message="Gmail connected. Fetch reports when you are ready.",
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network/provider boundary
        _update_gmail_session(
            state,
            status="error",
            message=f"Gmail connection failed: {exc}",
            error=str(exc),
        )


def _import_gmail_attachments_with_refresh_token(state: str, refresh_token: str) -> None:
    session = _get_gmail_session(state)
    try:
        if _gmail_cancel_requested(state):
            _mark_gmail_canceled(state, message="Gmail import was canceled before it started.")
            return

        token = refresh_access_token(
            GMAIL_OAUTH,
            client_id=session.client_id,
            client_secret=session.client_secret,
            refresh_token=refresh_token,
        )
        granted_scope = token.get("scope")
        if granted_scope:
            GMAIL_OAUTH.scope_policy.validate_granted(granted_scope)

        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError("Refresh response did not include an access token.")

        _import_gmail_attachments_with_access_token(state, access_token)
    except Exception as exc:  # pragma: no cover - network/provider boundary
        _update_gmail_session(
            state,
            status="error",
            message=f"Saved Gmail login failed: {exc}. Connect Gmail again.",
            error=str(exc),
        )


def _fetch_gmail_message_attachments(access_token: str, message_id: str) -> list[Attachment]:
    return list(GmailClient(access_token).iter_message_attachments(message_id))


def _import_gmail_attachments_with_access_token(state: str, access_token: str) -> None:
    session = _get_gmail_session(state)
    ledger_dirty = False
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    futures: dict[concurrent.futures.Future[list[Attachment]], str] = {}
    try:
        ledger = ImportLedger(session.ledger)
        store = AttachmentStore(session.output, ledger)
        client = GmailClient(access_token)
        files: list[dict[str, Any]] = []
        pdf_files: list[dict[str, Any]] = []
        saved = 0
        skipped = 0
        processed_messages = 0

        def save_ledger_if_dirty() -> None:
            nonlocal ledger_dirty
            if ledger_dirty:
                ledger.save()
                ledger_dirty = False

        def finish_canceled(message: str) -> None:
            save_ledger_if_dirty()
            _update_gmail_session(
                state,
                status="canceled",
                progress_phase="canceled",
                processed_messages=processed_messages,
                total_attachments=len(files),
                processed_attachments=len(files),
                saved=saved,
                skipped=skipped,
                files=list(files),
                pdf_files=list(pdf_files),
                message=message,
                error=None,
            )

        _update_gmail_session(
            state,
            progress_phase="listing",
            message="Finding matching Gmail messages.",
        )
        if _gmail_cancel_requested(state):
            finish_canceled("Gmail import was canceled before message lookup.")
            return

        message_ids = list(
            client.iter_message_ids(
                query=session.query,
                max_messages=session.max_messages,
            )
        )
        if _gmail_cancel_requested(state):
            finish_canceled("Gmail import was canceled after message lookup.")
            return

        _update_gmail_session(
            state,
            progress_phase="downloading",
            total_messages=len(message_ids),
            processed_messages=0,
            total_attachments=0,
            processed_attachments=0,
            message=f"Found {len(message_ids)} matching Gmail message(s). Downloading attachments.",
        )

        workers = min(GMAIL_FETCH_WORKERS, len(message_ids))
        next_index = 0

        def submit_next() -> None:
            nonlocal next_index
            if not executor or _gmail_cancel_requested(state):
                return
            if next_index >= len(message_ids):
                return
            message_id = message_ids[next_index]
            next_index += 1
            futures[executor.submit(_fetch_gmail_message_attachments, access_token, message_id)] = message_id

        if workers:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            for _ in range(workers):
                submit_next()

        while futures:
            if _gmail_cancel_requested(state):
                finish_canceled("Gmail import was canceled.")
                return

            done, _pending = concurrent.futures.wait(
                futures,
                timeout=0.25,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                continue

            for future in done:
                _message_id = futures.pop(future)
                if _gmail_cancel_requested(state):
                    finish_canceled("Gmail import was canceled.")
                    return

                attachments = future.result()
                processed_messages += 1

                for attachment in attachments:
                    if _gmail_cancel_requested(state):
                        finish_canceled("Gmail import was canceled.")
                        return

                    result = store.save_attachment(attachment, save_ledger=False)
                    if result.saved:
                        saved += 1
                        ledger_dirty = True
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
                    pdf_payload = _pdf_payload(attachment, result.path)
                    if pdf_payload:
                        pdf_files.append(pdf_payload)

                    _update_gmail_session(
                        state,
                        total_attachments=len(files),
                        processed_attachments=len(files),
                        saved=saved,
                        skipped=skipped,
                        files=list(files),
                        pdf_files=list(pdf_files),
                        message=(
                            f"Processed {processed_messages}/{len(message_ids)} message(s), "
                            f"{len(files)} attachment(s), reused {skipped} cached."
                        ),
                    )

                save_ledger_if_dirty()
                submit_next()

            _update_gmail_session(
                state,
                processed_messages=processed_messages,
                total_attachments=len(files),
                processed_attachments=len(files),
                saved=saved,
                skipped=skipped,
                message=(
                    f"Processed {processed_messages}/{len(message_ids)} message(s), "
                    f"{len(files)} attachment(s), reused {skipped} cached."
                ),
            )

        save_ledger_if_dirty()
        _update_gmail_session(
            state,
            status="done",
            progress_phase="done",
            processed_messages=len(message_ids),
            total_messages=len(message_ids),
            total_attachments=len(files),
            processed_attachments=len(files),
            message=f"Done. Saved {saved} new; reused {skipped} cached attachment(s).",
            saved=saved,
            skipped=skipped,
            files=files,
            pdf_files=pdf_files,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network/provider boundary
        _update_gmail_session(
            state,
            status="error",
            message=f"Gmail import failed: {exc}",
            error=str(exc),
        )
    finally:
        for future in futures:
            future.cancel()
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)


def _pdf_payload(attachment: Attachment, path: Path) -> dict[str, Any] | None:
    filename = attachment.filename.lower()
    content_type = (attachment.content_type or "").lower()
    if not filename.endswith(".pdf") and content_type != "application/pdf":
        return None
    return {
        "filename": attachment.filename,
        "path": str(path),
        "contentBase64": "",
        "contentType": attachment.content_type,
        "tooLarge": False,
        "parseable": True,
        "reason": "PDF report can be analyzed from its saved file.",
    }


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def run_server(host: str, port: int) -> None:
    server = ReloadFriendlyHTTPServer((host, port), Handler)
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
    parser.add_argument("--reload", action="store_true", help="Restart the server when project files change. This is the default.")
    parser.add_argument("--no-reload", action="store_true", help="Run one server process without watching files.")
    parser.add_argument("--reload-interval", default=0.8, type=float, help="Hot-reload polling interval in seconds.")
    args = parser.parse_args()

    reload_enabled = not args.no_reload or args.reload
    if reload_enabled and os.getenv("INVEST_TAX_CALC_RELOAD_CHILD") != "1":
        run_reloader(args)
        return

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
