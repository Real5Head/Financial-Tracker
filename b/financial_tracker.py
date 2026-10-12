#!/usr/bin/env python3
"""Finance Tracker 2.4.0 - a single-file, dark desktop ledger for macOS/Windows.

Replace the original financial_tracker.py with this file. The existing PostgreSQL
transactions table and ~/finance_tracker_db_config.json are supported. No new
third-party dependency is required: the UI uses standard-library Tkinter and the
database uses the original psycopg2 dependency. Existing CustomTkinter can remain
installed. Start with --demo to explore without touching a database.

Important upgrade rules:
* Replace the application on ALL devices before recording new transactions.
* Historical income uses its recorded net amount, including a legitimate zero.
* Former pending USD is combined with USD bank; historical settlement differences
  are preserved. A visible notice explains any pending balance carried over.
* Existing EUR belongs to EUR bank. EUR cash starts at zero.
* Historical payloads are not rewritten on startup. New amounts use decimal strings.
* New writes are locked, validated and committed atomically. Voiding is reversible.
* Existing USD/EUR display rates value your bank, cash and savings in DZD.
  They never change recorded amounts or add rate fields to the transfer form.
* The DZD total excludes unpaid loans, matching the original cash/savings total.
* Activity rows use type colors; voided entries stay muted.
* The interface uses a cleaner Apple-inspired dark workspace with system colors.
* USD and EUR values show their saved-rate DZD equivalent throughout the interface.
* SF Pro Text and SF Pro Display are preferred on both macOS and Windows.
* macOS startup uses native-safe cursors, Keychain access and persistent crash logs.

No money is moved by this application: it records transactions you already made.
"""
from __future__ import annotations

import base64
import copy
import csv
import ctypes
import faulthandler
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
except Exception as _tk_import_error:
    # A windowed macOS build otherwise exits with no visible explanation.
    _early_log = (Path.home() / "Library" / "Logs" / "FinanceTracker" / "finance_tracker.log"
                  if sys.platform == "darwin" else Path.home() / ".finance_tracker" / "logs" / "finance_tracker.log")
    try:
        _early_log.parent.mkdir(parents=True, exist_ok=True)
        with _early_log.open("a", encoding="utf-8") as _handle:
            _handle.write("\n[" + datetime.now().isoformat(timespec="seconds") + "] Tkinter import failed\n")
            traceback.print_exception(type(_tk_import_error), _tk_import_error, _tk_import_error.__traceback__, file=_handle)
    except Exception:
        pass
    if sys.platform == "darwin" and Path("/usr/bin/osascript").exists():
        try:
            subprocess.run(["/usr/bin/osascript", "-e",
                            'display alert "Finance could not start" message "The macOS Python/Tkinter runtime is missing or damaged. See ~/Library/Logs/FinanceTracker/finance_tracker.log" as critical'],
                           timeout=8, check=False)
        except Exception:
            pass
    raise

try:
    import psycopg2
    from psycopg2.extras import Json
    from psycopg2.extensions import parse_dsn
except ImportError:
    psycopg2 = None
    Json = None
    parse_dsn = None

APP_VERSION = "2.4.0"
APP_NAME = "Finance"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("999999999999.99")
CONFIG_FILE = Path.home() / "finance_tracker_db_config.json"
DATA_DIR = Path.home() / ".finance_tracker"
IS_MAC = sys.platform == "darwin"
LOG_DIR = (Path.home() / "Library" / "Logs" / "FinanceTracker") if IS_MAC else (DATA_DIR / "logs")
LOG_FILE = LOG_DIR / "finance_tracker.log"
_FAULT_LOG_HANDLE = None
CURRENCIES = ("USD", "EUR", "DZD")
KINDS = ("income", "expense", "transfer", "savings_deposit", "savings_withdraw",
         "loan_out", "loan_repaid", "opening", "adjustment")
CATEGORIES = ("Essentials", "Business", "Food", "Transport", "Shopping", "Debt", "Other")
# Keep the original setting keys. Rates are display estimates, not ledger money.
DISPLAY_RATE_KEYS = {"USD": "display_rate", "EUR": "display_rate_eur"}
RATE_UNIT = Decimal("0.000001")
MAX_DISPLAY_RATE = Decimal("1000000")
KEEP_BACKUPS = 15


def _redact_secrets(text: str) -> str:
    """Keep diagnostics useful without writing a PostgreSQL password to disk."""
    text = str(text)
    text = re.sub(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", text)
    text = re.sub(r"(?i)(password\s*[=:]\s*)[^\s,;]+", r"\1***", text)
    return text


def write_diagnostic(message: str, exc: Optional[BaseException] = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write("\n[" + datetime.now().isoformat(timespec="seconds") + "] " + _redact_secrets(message) + "\n")
            if exc is not None:
                rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                handle.write(_redact_secrets(rendered))
    except Exception:
        pass


def enable_crash_diagnostics() -> None:
    global _FAULT_LOG_HANDLE
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _FAULT_LOG_HANDLE = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        _FAULT_LOG_HANDLE.write("\n[" + datetime.now().isoformat(timespec="seconds") + "] Finance " + APP_VERSION + " starting on " + sys.platform + "\n")
        faulthandler.enable(_FAULT_LOG_HANDLE, all_threads=True)
    except Exception:
        _FAULT_LOG_HANDLE = None


def native_fatal_alert(title: str, message: str) -> None:
    """Show a useful error even from a --windowed .app with no Terminal."""
    safe_message = _redact_secrets(message)
    if IS_MAC and Path("/usr/bin/osascript").exists():
        try:
            def quote(value: str) -> str:
                return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            script = f'display alert "{quote(title)}" message "{quote(safe_message)}" as critical'
            subprocess.run(["/usr/bin/osascript", "-e", script], timeout=8, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, safe_message, parent=root)
        root.destroy()
    except Exception:
        try:
            print(title + ": " + safe_message, file=sys.stderr)
        except Exception:
            pass


def safe_cursor(widget: tk.Misc, enabled: bool = True) -> None:
    """Use cursor names accepted by native Aqua Tk as well as Windows Tk."""
    candidates = (["pointinghand", "arrow"] if IS_MAC else ["hand2", "arrow"]) if enabled else ["arrow"]
    for cursor_name in candidates:
        try:
            widget.configure(cursor=cursor_name)
            return
        except tk.TclError:
            continue


class ValidationError(Exception):
    """A definite rejection; nothing has been committed."""


class ConflictError(ValidationError):
    """Another device changed the record. Refresh before editing again."""


class ConnectionFailure(Exception):
    """An operation may have an uncertain outcome; retry its SAME operation ID."""


@dataclass(frozen=True)
class Account:
    key: str
    name: str
    currency: str
    location: str
    accent: str


ACCOUNTS: Dict[str, Account] = {
    "usd_bank": Account("usd_bank", "USD bank", "USD", "Bank", "#0A84FF"),
    "eur_bank": Account("eur_bank", "EUR bank", "EUR", "Bank", "#64D2FF"),
    "eur_cash": Account("eur_cash", "EUR cash", "EUR", "Cash", "#FF9F0A"),
    "dzd_cash": Account("dzd_cash", "DZD cash", "DZD", "Cash", "#30D158"),
}
ACCOUNT_NAMES = {a.name: a.key for a in ACCOUNTS.values()}
DEFAULT_ACCOUNT = {"USD": "usd_bank", "EUR": "eur_bank", "DZD": "dzd_cash"}
KIND_NAMES = {"income": "Income", "expense": "Expense", "transfer": "Transfer",
              "savings_deposit": "Save", "savings_withdraw": "Release savings",
              "loan_out": "Loan", "loan_repaid": "Repayment", "opening": "Opening balance",
              "adjustment": "Imported settlement"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def money(value: Any, label: str = "Amount", *, signed: bool = False,
          allow_zero: bool = False, legacy: bool = False) -> Decimal:
    """Parse money without binary floats. User input allows 1234.56 or 1234,56.

    Spaces may group thousands; a comma is a decimal mark, not a thousands mark.
    Scientific notation and non-finite values are never valid user amounts.
    Old numeric values are rounded once per recorded amount, to the nearest cent.
    """
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{label}: enter a valid amount.")
    s = str(value).strip()
    if not legacy:
        s = s.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
        pattern = r"[+-]?\d+(?:[.,]\d{1,2})?" if signed else r"\+?\d+(?:[.,]\d{1,2})?"
        if not re.fullmatch(pattern, s) or len(s) > 24:
            raise ValidationError(f"{label}: use up to 2 decimals, for example 1250.50 or 1250,50.")
        s = s.replace(",", ".")
    try:
        v = Decimal(s)
        if not v.is_finite() or abs(v) > MAX_AMOUNT:
            raise InvalidOperation
        v = v.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{label}: enter a finite amount below 1 trillion.") from None
    if not signed and (v < ZERO or (v == ZERO and not allow_zero)):
        raise ValidationError(f"{label} must be {'zero or greater' if allow_zero else 'greater than zero'}.")
    return v


def fmt(value: Decimal, currency: str, *, symbol: bool = True) -> str:
    v = value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = f"{abs(v):,.2f}"
    sign = "-" if v < ZERO else ""
    prefix = {"USD": "$", "EUR": "\u20ac", "DZD": ""}.get(currency, "")
    return f"{sign}{prefix}{text}" if symbol and prefix else f"{sign}{text} {currency}"


def checked_date(value: Any, *, historical: bool = False) -> str:
    s = str(value).strip()
    try:
        d = date.fromisoformat(s[:10] if historical else s)
    except (ValueError, TypeError):
        raise ValidationError("Date: use YYYY-MM-DD, for example 2026-09-12.") from None
    if not historical and (d > date.today() or d.year < 1900):
        raise ValidationError("Use a date from 1900 through today. Future transactions are not supported.")
    return d.isoformat()


def text_value(value: Any, label: str, required: bool = False, maximum: int = 300) -> str:
    s = str(value or "").strip()
    if required and not s:
        raise ValidationError(f"Enter {label.lower()}.")
    if len(s) > maximum:
        raise ValidationError(f"{label} must be {maximum} characters or fewer.")
    if "\x00" in s:
        raise ValidationError(f"{label} contains an unsupported character.")
    return s


def account_key(value: str) -> str:
    key = ACCOUNT_NAMES.get(value, value)
    if key not in ACCOUNTS:
        raise ValidationError("Select a valid bank or cash account.")
    return key


def rate_text(src: str, dst: str, sent: Decimal, received: Decimal) -> str:
    """Display only; the two recorded amounts are always the source of truth."""
    a, b = ACCOUNTS[src].currency, ACCOUNTS[dst].currency
    if a == b:
        difference = received - sent
        return "Same currency \u00b7 no conversion" if difference == ZERO else f"Net difference: {fmt(difference, a)}"
    if sent <= ZERO or received <= ZERO:
        return "The effective rate appears after you enter both amounts."
    # A dinar quote per dollar/euro is more readable than a tiny decimal.
    if a == "DZD":
        a, b, sent, received = b, a, received, sent
    rate = received / sent
    places = max(4, min(18, -rate.adjusted() + 3))
    rendered = f"{rate:,.{places}f}".rstrip("0").rstrip(".")
    return f"1 {a} = {rendered} {b}"



def display_rate(value: Any, label: str = "Display rate", *, stored: bool = False) -> Decimal:
    """Positive DZD per currency unit, up to six decimals; never a live quote.

    Original PostgreSQL settings use FLOAT. Canonical six-decimal strings make
    revisions stable when a rate is reread from that existing column. Financial
    transaction amounts and the money() parser are not changed by these settings.
    """
    if value is None or isinstance(value, bool):
        raise ValidationError(f"{label}: enter a positive DZD value.")
    text = str(value).strip()
    if not stored:
        text = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
        if len(text) > 24 or not re.fullmatch(r"\+?\d+(?:[.,]\d{1,6})?", text):
            raise ValidationError(f"{label}: use a positive number with up to 6 decimals.")
        text = text.replace(",", ".")
    try:
        result = Decimal(text)
        if not result.is_finite() or result <= 0 or result > MAX_DISPLAY_RATE:
            raise InvalidOperation
        result = result.quantize(RATE_UNIT, rounding=ROUND_HALF_UP)
        if result <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{label}: use a finite rate from 0.000001 to 1,000,000 DZD.") from None
    return result


def display_rates(settings: Dict[str, Any]) -> Dict[str, Optional[Decimal]]:
    """Read original rate settings without inventing defaults for missing quotes."""
    result: Dict[str, Optional[Decimal]] = {}
    for currency, key in DISPLAY_RATE_KEYS.items():
        try:
            result[currency] = display_rate(settings.get(key), stored=True)
        except ValidationError:
            result[currency] = None
    return result


def display_rate_revision(settings: Dict[str, Any]) -> str:
    """Optimistic concurrency check limited to the two editable display settings."""
    canonical = {}
    for key in DISPLAY_RATE_KEYS.values():
        value = settings.get(key)
        try:
            canonical[key] = str(display_rate(value, stored=True))
        except ValidationError:
            canonical[key] = None if value is None else "invalid:" + str(value)
    return fingerprint(canonical)


def display_rate_payload(values: Dict[str, Any]) -> Dict[str, str]:
    if set(values) != set(DISPLAY_RATE_KEYS.values()):
        raise ValidationError("Supply both USD and EUR display rates, and no other settings.")
    return {key: str(display_rate(values[key], f"1 {currency} in DZD"))
            for currency, key in DISPLAY_RATE_KEYS.items()}


def compact_rate(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def dzd_equivalent(amounts: Dict[str, Decimal], settings: Dict[str, Any]) -> Optional[Decimal]:
    """Value the supplied balances once. None means a necessary rate is missing."""
    rates = display_rates(settings)
    total = amounts.get("DZD", ZERO)
    for currency in ("USD", "EUR"):
        amount = amounts.get(currency, ZERO)
        if amount == ZERO:
            continue
        rate = rates[currency]
        if rate is None:
            return None
        total += amount * rate
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def amount_in_dinars(amount: Decimal, currency: str,
                      settings: Dict[str, Any]) -> Optional[Decimal]:
    """Return one amount's display-only DZD value using the saved quote."""
    value = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if currency == "DZD" or value == ZERO:
        return value
    rate = display_rates(settings).get(currency)
    if rate is None:
        return None
    return (value * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def dinar_equivalent_text(amount: Decimal, currency: str,
                           settings: Dict[str, Any], *,
                           include_dzd: bool = False) -> str:
    """Compact DZD equivalent text for cards, rows and previews."""
    if currency == "DZD" and not include_dzd:
        return ""
    value = amount_in_dinars(amount, currency, settings)
    if value is None:
        return f"Set {currency} display rate"
    # Match the original tracker: DZD estimates are display values rounded to
    # the nearest dinar, while recorded account amounts keep cent precision.
    return f"≈ {value:,.0f} DZD"


def record_dinar_equivalent(record: "Record",
                             settings: Dict[str, Any]) -> str:
    """DZD estimate for a ledger row without changing recorded amounts."""
    if record.kind != "transfer":
        return dinar_equivalent_text(record.amount, record.currency, settings)
    parts: List[str] = []
    source_currency = ACCOUNTS[record.account].currency
    destination_currency = ACCOUNTS[record.destination].currency
    source = dinar_equivalent_text(record.amount, source_currency, settings)
    destination = dinar_equivalent_text(record.received, destination_currency, settings)
    if source:
        parts.append(source)
    if destination:
        parts.append(destination)
    return "  →  ".join(parts)


@dataclass
class Record:
    id: str
    day: str
    kind: str
    account: str = "usd_bank"
    amount: Decimal = ZERO
    destination: Optional[str] = None
    received: Decimal = ZERO
    description: str = ""
    category: str = ""
    borrower: str = ""
    notes: str = ""
    loan_id: str = ""
    voided: bool = False
    legacy: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def currency(self) -> str:
        return ACCOUNTS[self.account].currency

    @property
    def title(self) -> str:
        if self.kind == "transfer":
            return self.description or f"{ACCOUNTS[self.account].name} \u2192 {ACCOUNTS[self.destination].name}"
        if self.kind == "loan_out":
            return f"Lent to {self.borrower}"
        if self.kind == "loan_repaid":
            return f"Repaid by {self.borrower}"
        return self.description or KIND_NAMES[self.kind]

    @property
    def route(self) -> str:
        if self.destination:
            return f"{ACCOUNTS[self.account].name} \u2192 {ACCOUNTS[self.destination].name}"
        return ACCOUNTS[self.account].name

    @property
    def amount_text(self) -> str:
        if self.kind == "transfer":
            return f"{fmt(self.amount, self.currency)} \u2192 {fmt(self.received, ACCOUNTS[self.destination].currency)}"
        prefix = "+ " if self.kind in ("income", "loan_repaid", "opening") else "- " if self.kind in ("expense", "loan_out") else ""
        return prefix + fmt(self.amount, self.currency)


def normalize(raw: Dict[str, Any]) -> Record:
    if not isinstance(raw, dict):
        raise ValidationError("Transaction payload is not an object.")
    if raw.get("_database_error"):
        raise ValidationError(raw["_database_error"])
    rid = text_value(raw.get("id"), "Transaction ID", True, 255)
    kind = str(raw.get("type", ""))
    legacy = raw.get("schema_version") != 2
    if raw.get("schema_version") not in (None, 1, 2):
        raise ValidationError("This record uses a newer format. Update all copies of Finance.")
    if "voided" in raw and not isinstance(raw["voided"], bool):
        raise ValidationError("The voided flag must be a boolean.")
    day = checked_date(raw.get("date", ""), historical=True)
    currency = str(raw.get("currency", "USD"))
    if currency not in CURRENCIES:
        raise ValidationError(f"Unsupported currency: {currency}.")
    account = account_key(raw.get("account", DEFAULT_ACCOUNT[currency]))
    if not legacy and "currency" in raw and currency != ACCOUNTS[account].currency:
        raise ValidationError("Transaction currency does not match its account.")
    rec = Record(rid, day, kind, account=account, voided=bool(raw.get("voided", False)),
                 legacy=legacy, raw=copy.deepcopy(raw),
                 description=str(raw.get("description", raw.get("category", ""))),
                 category=str(raw.get("category", "")), borrower=str(raw.get("borrower", "")),
                 notes=str(raw.get("notes", "")),
                 loan_id=str(raw.get("loan_id", raw.get("original_loan_id", ""))))
    old_transfers = {
        "transfer_usd_dzd": ("usd_bank", "dzd_cash", "amount_usd", "amount_dzd"),
        "transfer_eur_dzd": ("eur_bank", "dzd_cash", "amount_eur", "amount_dzd"),
        "transfer_dzd_eur": ("dzd_cash", "eur_bank", "amount_dzd", "amount_eur"),
        "transfer_dzd_usd": ("dzd_cash", "usd_bank", "amount_dzd", "amount_usd"),
        "transfer_eur_usd": ("eur_bank", "usd_bank", "amount_eur", "amount_usd"),
        "transfer_usd_eur": ("usd_bank", "eur_bank", "amount_usd", "amount_eur"),
    }
    if kind in old_transfers:
        rec.account, rec.destination, sent_key, received_key = old_transfers[kind]
        rec.kind = "transfer"
        rec.amount = money(raw.get(sent_key), legacy=True)
        rec.received = money(raw.get(received_key), legacy=True)
    elif kind == "transfer_paypal_bank":
        rec.kind, rec.account = "adjustment", "usd_bank"
        rec.amount = money(raw.get("amount_received"), legacy=True, allow_zero=True) - money(raw.get("amount_sent"), legacy=True)
        rec.description = "Imported USD settlement"
        rec.notes = "Preserves the recorded difference between USD sent and received before the upgrade."
    elif kind == "transfer":
        rec.account = account_key(raw.get("from_account", ""))
        rec.destination = account_key(raw.get("to_account", ""))
        if rec.account == rec.destination:
            raise ValidationError("Source and destination must be different accounts.")
        rec.amount = money(raw.get("amount_sent"), legacy=legacy)
        rec.received = money(raw.get("amount_received"), legacy=legacy)
    elif kind in KINDS:
        value = raw.get("net_amount") if legacy and kind == "income" and "net_amount" in raw else raw.get("amount")
        rec.amount = money(value, legacy=legacy, allow_zero=legacy and kind == "income", signed=kind == "adjustment")
    else:
        raise ValidationError(f"Unknown transaction type: {kind or '(missing)'}. No balance was assumed.")
    if rec.kind in ("loan_out", "loan_repaid") and not rec.borrower.strip():
        raise ValidationError("A loan or repayment has no borrower.")
    if rec.kind == "loan_repaid" and not rec.loan_id:
        raise ValidationError("A repayment has no linked loan.")
    return rec


@dataclass
class Snapshot:
    raw: List[Dict[str, Any]]
    records: List[Record] = field(default_factory=list)
    available: Dict[str, Decimal] = field(default_factory=lambda: {k: ZERO for k in ACCOUNTS})
    saved: Dict[str, Decimal] = field(default_factory=lambda: {k: ZERO for k in ACCOUNTS})
    outstanding: Dict[str, Decimal] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    legacy_pending: Decimal = ZERO
    fetched_at: str = field(default_factory=now_utc)
    settings: Dict[str, Any] = field(default_factory=dict)
    # Review marks: transaction id -> {"fp": review fingerprint, "at": ISO time}.
    checks: Dict[str, Dict[str, str]] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors

    def by_id(self, rid: str) -> Optional[Record]:
        return next((r for r in self.records if r.id == rid), None)

    def monthly(self, month: str, kind: str) -> Dict[str, Decimal]:
        result = {c: ZERO for c in CURRENCIES}
        for r in self.records:
            if not r.voided and r.kind == kind and r.day.startswith(month):
                result[r.currency] += r.amount
        return result

    def currency_totals(self, include_loans: bool = False) -> Dict[str, Decimal]:
        result = {c: ZERO for c in CURRENCIES}
        for k, a in ACCOUNTS.items():
            result[a.currency] += self.available[k] + self.saved[k]
        if include_loans:
            for rid, remaining in self.outstanding.items():
                r = self.by_id(rid)
                if r:
                    result[r.currency] += remaining
        return result


def project(raw_records: List[Dict[str, Any]], settings: Optional[Dict[str, Any]] = None) -> Snapshot:
    """Pure ledger projection. Bad data is reported, never silently counted as zero."""
    snap = Snapshot(copy.deepcopy(raw_records), settings=copy.deepcopy(settings or {}))
    seen = set()
    for i, raw in enumerate(raw_records):
        try:
            rec = normalize(raw)
            if rec.id in seen:
                raise ValidationError("Duplicate transaction ID.")
            seen.add(rec.id)
            snap.records.append(rec)
        except (ValidationError, TypeError, KeyError, InvalidOperation) as exc:
            rid = raw.get("id", f"row {i + 1}") if isinstance(raw, dict) else f"row {i + 1}"
            snap.errors.append(f"{rid}: {exc}")
    active = [r for r in snap.records if not r.voided]
    loans = {r.id: r for r in active if r.kind == "loan_out"}
    repaid = {rid: ZERO for rid in loans}
    for r in active:
        if r.kind in ("income", "opening", "adjustment", "loan_repaid"):
            snap.available[r.account] += r.amount
        elif r.kind in ("expense", "loan_out"):
            snap.available[r.account] -= r.amount
        elif r.kind == "transfer":
            snap.available[r.account] -= r.amount
            snap.available[r.destination] += r.received
        elif r.kind == "savings_deposit":
            snap.available[r.account] -= r.amount
            snap.saved[r.account] += r.amount
        elif r.kind == "savings_withdraw":
            snap.available[r.account] += r.amount
            snap.saved[r.account] -= r.amount
        if r.kind == "loan_repaid":
            loan = loans.get(r.loan_id)
            if not loan:
                snap.errors.append(f"{r.id}: repayment points to a missing or voided loan.")
            elif loan.currency != r.currency:
                snap.errors.append(f"{r.id}: repayment currency differs from the loan.")
            elif r.day < loan.day:
                snap.errors.append(f"{r.id}: repayment is dated before the loan.")
            else:
                repaid[r.loan_id] += r.amount
        if r.legacy and r.kind == "income" and r.currency == "USD" and r.raw.get("to_paypal"):
            snap.legacy_pending += r.amount
        elif r.legacy and r.raw.get("type") == "transfer_paypal_bank":
            snap.legacy_pending -= money(r.raw.get("amount_sent"), legacy=True)
    for rid, loan in loans.items():
        remaining = loan.amount - repaid[rid]
        if remaining < ZERO:
            snap.errors.append(f"{rid}: linked repayments exceed the original loan. Review the imported records.")
        snap.outstanding[rid] = max(ZERO, remaining)
        stored_status = loan.raw.get("status", "active")
        if stored_status == "repaid" and remaining > ZERO:
            snap.warnings.append(f"Loan to {loan.borrower}: marked repaid in the old app, but {fmt(remaining, loan.currency)} has no linked repayment. No cash was invented.")
    for k in ACCOUNTS:
        if snap.available[k] < ZERO or snap.saved[k] < ZERO:
            snap.warnings.append(f"{ACCOUNTS[k].name} has an existing negative balance. New transactions cannot worsen it.")
    return snap


def total_in_dinars(snapshot: Snapshot) -> Optional[Decimal]:
    """Available + saved in all four accounts. Unpaid loans are NOT held cash."""
    if not snapshot.valid:
        return None
    return dzd_equivalent(snapshot.currency_totals(include_loans=False), snapshot.settings)


def record_effects(r: "Record") -> List[Tuple[str, str, Decimal]]:
    """(account, bucket, delta) pairs for one active record, matching project()."""
    if r.kind in ("income", "opening", "adjustment", "loan_repaid"):
        return [(r.account, "available", r.amount)]
    if r.kind in ("expense", "loan_out"):
        return [(r.account, "available", -r.amount)]
    if r.kind == "transfer":
        return [(r.account, "available", -r.amount), (r.destination, "available", r.received)]
    if r.kind == "savings_deposit":
        return [(r.account, "available", -r.amount), (r.account, "saved", r.amount)]
    if r.kind == "savings_withdraw":
        return [(r.account, "available", r.amount), (r.account, "saved", -r.amount)]
    return []


def lowest_points(snapshot: Snapshot) -> Dict[Tuple[str, str], Tuple[Decimal, str]]:
    """Lowest end-of-day balance per (account, bucket) across the whole timeline."""
    by_day: Dict[str, List[Tuple[str, str, Decimal]]] = {}
    for r in snapshot.records:
        if not r.voided:
            by_day.setdefault(r.day, []).extend(record_effects(r))
    running: Dict[Tuple[str, str], Decimal] = {}
    lowest: Dict[Tuple[str, str], Tuple[Decimal, str]] = {}
    for day in sorted(by_day):
        for account, bucket, delta in by_day[day]:
            running[(account, bucket)] = running.get((account, bucket), ZERO) + delta
        for slot, value in running.items():
            if slot not in lowest or value < lowest[slot][0]:
                lowest[slot] = (value, day)
    return lowest


def month_add(month: str, delta: int) -> str:
    year, number = map(int, month.split("-"))
    total = year * 12 + number - 1 + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def expense_category(record: "Record") -> str:
    category = (record.category or "Other").strip() or "Other"
    # The original application concatenated category and description.
    if record.legacy and " - " in category:
        category = category.split(" - ", 1)[0]
    return category


def monthly_dinar_series(snapshot: Snapshot, end_month: str, months: int = 6
                         ) -> Optional[List[Tuple[str, Decimal, Decimal]]]:
    """(month, income, expenses) valued in DZD. None if a needed rate is missing."""
    result = []
    for offset in range(months - 1, -1, -1):
        month = month_add(end_month, -offset)
        values = []
        for kind in ("income", "expense"):
            value = dzd_equivalent(snapshot.monthly(month, kind), snapshot.settings)
            if value is None:
                return None
            values.append(value)
        result.append((month, values[0], values[1]))
    return result


def category_dinar_totals(snapshot: Snapshot, month: str, limit: int = 6
                          ) -> Optional[List[Tuple[str, Decimal]]]:
    """Spending per category for one month in DZD, largest first; the tail folds into Other."""
    totals: Dict[str, Decimal] = {}
    for r in snapshot.records:
        if r.voided or r.kind != "expense" or not r.day.startswith(month):
            continue
        value = amount_in_dinars(r.amount, r.currency, snapshot.settings)
        if value is None:
            return None
        key = expense_category(r)
        totals[key] = totals.get(key, ZERO) + value
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > limit:
        head = [item for item in ranked[:limit - 1] if item[0] != "Other"]
        kept = {name for name, _ in head}
        other = sum((v for name, v in ranked if name not in kept), ZERO)
        ranked = head + [("Other", other)]
    return ranked


def record_payload(kind: str, values: Dict[str, Any], *, rid: Optional[str] = None) -> Dict[str, Any]:
    """The only constructor for new financial entries (no rate/fee input)."""
    if kind not in KINDS or kind == "adjustment":
        raise ValidationError("Choose a supported transaction type.")
    result: Dict[str, Any] = {"schema_version": 2, "id": rid or str(uuid.uuid4()),
                              "type": kind, "date": checked_date(values.get("date", date.today().isoformat())),
                              "created_at": now_utc(), "voided": False}
    result["description"] = text_value(values.get("description"), "Description",
                                       required=kind in ("income", "expense"), maximum=160)
    result["notes"] = text_value(values.get("notes"), "Notes", maximum=2000)
    if kind == "transfer":
        result["from_account"] = account_key(values.get("from_account", ""))
        result["to_account"] = account_key(values.get("to_account", ""))
        if result["from_account"] == result["to_account"]:
            raise ValidationError("Choose two different accounts.")
        result["amount_sent"] = str(money(values.get("amount_sent"), "Amount sent"))
        result["amount_received"] = str(money(values.get("amount_received"), "Amount received"))
    else:
        result["account"] = account_key(values.get("account", ""))
        result["currency"] = ACCOUNTS[result["account"]].currency
        result["amount"] = str(money(values.get("amount")))
        if kind == "expense":
            result["category"] = text_value(values.get("category", "Other"), "Category", True, 80)
        if kind in ("loan_out", "loan_repaid"):
            result["borrower"] = text_value(values.get("borrower"), "Borrower", True, 160)
            result["status"] = "active" if kind == "loan_out" else "recorded"
        if kind == "loan_repaid":
            result["loan_id"] = text_value(values.get("loan_id"), "Loan ID", True, 255)
    normalize(result)
    return result


# ---------------------------------------------------------------------------
# Review helpers: pure functions behind the All activity checking table.
# ---------------------------------------------------------------------------
# Keys the app itself rewrites (loan status, edit history) do not void a tick.
REVIEW_VOLATILE_KEYS = {"status", "repaid_date", "_history", "updated_at"}


def review_fingerprint(record: "Record") -> str:
    return fingerprint({k: v for k, v in record.raw.items() if k not in REVIEW_VOLATILE_KEYS})


def check_state(record: "Record", checks: Dict[str, Dict[str, str]]) -> str:
    """'checked', 'changed' (edited after it was ticked) or ''."""
    mark = checks.get(record.id)
    if not mark:
        return ""
    return "checked" if mark.get("fp") == review_fingerprint(record) else "changed"


def chrono_key(record: "Record") -> Tuple[str, str, str]:
    return (record.day, str(record.raw.get("created_at") or record.day), record.id)


def held_delta(record: "Record", account: str) -> Decimal:
    """Change in money physically held by one account (available + saved).

    Moving money into or out of savings stays inside the same account, so it
    does not change what the bank or wallet really holds.
    """
    if record.kind == "transfer":
        delta = ZERO
        if record.account == account:
            delta -= record.amount
        if record.destination == account:
            delta += record.received
        return delta
    if record.account != account:
        return ZERO
    if record.kind in ("income", "opening", "adjustment", "loan_repaid"):
        return record.amount
    if record.kind in ("expense", "loan_out"):
        return -record.amount
    return ZERO


def touches(record: "Record", account: str) -> bool:
    return record.account == account or record.destination == account


def running_balances(snapshot: Snapshot, account: str) -> Dict[str, Decimal]:
    """Balance held by an account right after each active record, oldest first."""
    balance, result = ZERO, {}
    for record in sorted((r for r in snapshot.records if not r.voided and touches(r, account)),
                         key=chrono_key):
        balance += held_delta(record, account)
        result[record.id] = balance
    return result


def balance_through(snapshot: Snapshot, account: str, last_day: Optional[str]) -> Decimal:
    """Held balance at the end of last_day (None means everything recorded)."""
    return sum((held_delta(r, account) for r in snapshot.records
                if not r.voided and (last_day is None or r.day <= last_day)), ZERO)


def duplicate_partners(snapshot: Snapshot, window_days: int = 3) -> Dict[str, List[str]]:
    """Active records that look entered twice: same type, accounts and amounts within a few days."""
    groups: Dict[Tuple[Any, ...], List["Record"]] = {}
    for r in snapshot.records:
        if r.voided or r.kind in ("opening", "adjustment"):
            continue
        key = (r.kind, r.account, r.destination, r.amount, r.received if r.destination else ZERO)
        groups.setdefault(key, []).append(r)
    partners: Dict[str, List[str]] = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=chrono_key)
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if (date.fromisoformat(b.day) - date.fromisoformat(a.day)).days > window_days:
                    break
                partners.setdefault(a.id, []).append(b.id)
                partners.setdefault(b.id, []).append(a.id)
    return partners


def month_end(day: date) -> date:
    return date.fromordinal(date.fromisoformat(month_add(day.isoformat()[:7], 1) + "-01").toordinal() - 1)


PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def period_for(unit: str, anchor: date) -> Tuple[Optional[date], Optional[date]]:
    if unit == "all":
        return None, None
    if unit == "week":
        start = date.fromordinal(anchor.toordinal() - anchor.weekday())
        return start, date.fromordinal(start.toordinal() + 6)
    if unit == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    if unit == "quarter":
        start = date.fromisoformat(month_add(anchor.isoformat()[:7], -2) + "-01")
        return start, month_end(anchor)
    start = anchor.replace(day=1)
    return start, month_end(start)


def shift_period(start: date, end: date, unit: str, delta: int) -> Tuple[date, date]:
    if unit in PERIOD_MONTHS:
        months = PERIOD_MONTHS[unit]
        new_start = date.fromisoformat(month_add(start.isoformat()[:7], delta * months) + "-01")
        last = date.fromisoformat(month_add(new_start.isoformat()[:7], months - 1) + "-01")
        return new_start, month_end(last)
    span = end.toordinal() - start.toordinal() + 1
    return (date.fromordinal(start.toordinal() + delta * span),
            date.fromordinal(end.toordinal() + delta * span))


def period_text(start: Optional[date], end: Optional[date], unit: str) -> str:
    if start is None or end is None:
        return "All time"
    if unit == "month":
        return f"{start:%B %Y}"
    if unit == "year":
        return str(start.year)
    if unit == "quarter":
        return f"{start:%b} – {end:%b %Y}" if start.year == end.year else f"{start:%b %Y} – {end:%b %Y}"
    if start == end:
        return f"{start:%a} {start.day} {start:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


@dataclass
class Command:
    action: str
    record: Dict[str, Any]
    expected: str = ""
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def digest(self) -> str:
        return fingerprint({"action": self.action, "record": self.record, "expected": self.expected})


def apply_display_rates(settings: Dict[str, Any], command: Command) -> Dict[str, Any]:
    """Change valuation preferences only; no transaction or balance is modified."""
    if command.action != "display_rates":
        raise ValidationError("Unsupported settings operation.")
    if command.expected != display_rate_revision(settings):
        raise ConflictError("Display rates changed on another device. Close this window, refresh, and reopen it.")
    updated = copy.deepcopy(settings)
    updated.update(display_rate_payload(command.record))
    return updated


def apply_command(raw: List[Dict[str, Any]], cmd: Command) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate against authoritative records while the repository holds its lock."""
    before = project(raw)
    if not before.valid:
        raise ValidationError("Existing records need review before new writes. Open Settings > Data checks and export a backup. " + before.errors[0])
    data = copy.deepcopy(raw)
    rid = cmd.record.get("id")
    index = next((i for i, r in enumerate(data) if r.get("id") == rid), None)
    changed: List[str] = []
    if cmd.action == "add":
        if index is not None:
            raise ConflictError("This transaction ID already exists. Refresh your history.")
        item = copy.deepcopy(cmd.record)
        if item.get("schema_version") != 2 or item.get("voided"):
            raise ValidationError("Invalid new transaction.")
        rec = normalize(item)
        checked_date(rec.day)
        if rec.kind == "adjustment":
            raise ValidationError("Imported settlements cannot be added manually.")
        if rec.kind == "loan_repaid":
            loan = before.by_id(rec.loan_id)
            if not loan or loan.voided or loan.kind != "loan_out":
                raise ValidationError("That loan is no longer active. Refresh first.")
            if before.outstanding.get(loan.id, ZERO) <= ZERO:
                raise ValidationError("This loan is already fully repaid.")
            item["borrower"] = loan.borrower
        data.append(item)
        changed.append(rid)
    elif cmd.action in ("replace", "void", "restore"):
        if index is None:
            raise ConflictError("This transaction no longer exists. Refresh first.")
        old = data[index]
        if fingerprint(old) != cmd.expected:
            raise ConflictError("This record changed on another device. Close this window, refresh, and reopen it.")
        old_rec = normalize(old)
        if cmd.action == "replace":
            if old_rec.voided:
                raise ValidationError("Restore a voided transaction before editing it.")
            if old_rec.kind in ("loan_repaid", "adjustment"):
                raise ValidationError("This record cannot be edited. Void a repayment and record the correction instead.")
            if old_rec.kind == "loan_out" and any(r.kind == "loan_repaid" and r.loan_id == rid and not r.voided for r in before.records):
                raise ValidationError("A loan with repayments cannot be edited. Correct its repayments first.")
            item = copy.deepcopy(cmd.record)
            new_rec = normalize(item)
            if new_rec.kind != old_rec.kind:
                raise ValidationError("Editing cannot change the transaction type.")
            checked_date(new_rec.day)
            item["created_at"] = old.get("created_at", old.get("date", now_utc()))
        else:
            item = copy.deepcopy(old)
            target = cmd.action == "void"
            if bool(old.get("voided", False)) == target:
                raise ConflictError("This record is already in that state. Refresh first.")
            if target and old_rec.kind == "loan_out" and any(
                    r.kind == "loan_repaid" and r.loan_id == rid and not r.voided for r in before.records):
                raise ValidationError("This loan has repayments. Void its repayments first, then void the loan.")
            if not target and old_rec.kind == "loan_repaid":
                linked = before.by_id(old_rec.loan_id)
                if not linked or linked.voided:
                    raise ValidationError("The loan for this repayment is voided. Restore the loan first.")
            item["voided"] = target
            item["voided_at"] = now_utc() if target else None
        # Preserve every previous revision. Do not nest its previous history again.
        prior = {k: v for k, v in old.items() if k != "_history"}
        item["_history"] = list(old.get("_history", [])) + [{"at": now_utc(), "action": cmd.action, "before": prior}]
        item["updated_at"] = now_utc()
        data[index] = item
        changed.append(rid)
    else:
        raise ValidationError("Unknown operation.")
    after = project(data)
    if not after.valid:
        raise ValidationError(after.errors[0])
    for bucket_name in ("available", "saved"):
        old_b, new_b = getattr(before, bucket_name), getattr(after, bucket_name)
        for k in ACCOUNTS:
            if new_b[k] < min(ZERO, old_b[k]):
                raise ValidationError(f"Not enough {'saved funds' if bucket_name == 'saved' else 'available funds'} in {ACCOUNTS[k].name}. Available: {fmt(old_b[k], ACCOUNTS[k].currency)}. This change would leave {fmt(new_b[k], ACCOUNTS[k].currency)}.")
    # Balances must also hold on every past day, so a backdated entry cannot
    # spend money that had not arrived yet. Existing dips are never made worse.
    old_low, new_low = lowest_points(before), lowest_points(after)
    for slot, (new_value, new_day) in new_low.items():
        old_value = old_low.get(slot, (ZERO, ""))[0]
        if new_value < min(ZERO, old_value):
            k, bucket_name = slot
            raise ValidationError(
                f"On {new_day}, {ACCOUNTS[k].name} would drop to {fmt(new_value, ACCOUNTS[k].currency)}"
                f"{' in savings' if bucket_name == 'saved' else ''}. Check the date, or record the money "
                "that came in first (an opening balance should be dated before your first expense).")
    # Loan status and repayment are saved in the SAME database transaction.
    affected_loans = set()
    for rid_changed in changed:
        for snap in (before, after):
            r = snap.by_id(rid_changed)
            if r and r.kind == "loan_repaid":
                affected_loans.add(r.loan_id)
            elif r and r.kind == "loan_out":
                affected_loans.add(r.id)
    for i, item in enumerate(data):
        if item.get("id") in affected_loans and not item.get("voided"):
            remaining = after.outstanding.get(item["id"], ZERO)
            item["status"] = "repaid" if remaining == ZERO else "active"
            if remaining == ZERO:
                repayments = [r.day for r in after.records if r.kind == "loan_repaid" and r.loan_id == item["id"] and not r.voided]
                item["repaid_date"] = max(repayments) if repayments else item.get("date")
            else:
                item.pop("repaid_date", None)
            if item["id"] not in changed:
                changed.append(item["id"])
    return data, [item for item in data if item.get("id") in changed]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".finance-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def backup_document(snapshot: Snapshot) -> Dict[str, Any]:
    return {"application": APP_NAME, "version": APP_VERSION, "exported_at": now_utc(),
            "notice": "Contains private financial records, but no database URL or password.",
            "account_definitions": {k: {"name": a.name, "currency": a.currency, "location": a.location} for k, a in ACCOUNTS.items()},
            "transactions": snapshot.raw, "legacy_settings": snapshot.settings,
            "review_checks": snapshot.checks,
            "data_checks": {"errors": snapshot.errors, "warnings": snapshot.warnings}}


class CredentialStore:
    """No extra package: Windows DPAPI, macOS Keychain, or session-only fallback.

    The original plaintext config is accepted. It is replaced only AFTER secure
    storage succeeds. We never make a plaintext copy of that credential file.
    """
    SERVICE = b"FinanceTracker.Database"
    ACCOUNT = b"default"

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path

    @staticmethod
    def _windows(value: bytes, decrypt: bool = False) -> bytes:
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
        source_buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        source = Blob(len(value), source_buffer)
        target = Blob()
        api = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = api.CryptUnprotectData if decrypt else api.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise OSError("Windows credential protection failed.")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))

    @classmethod
    def _mac(cls, password: Optional[str] = None) -> str:
        """Read/write macOS Keychain through Apple's security tool.

        The previous raw ctypes bridge could terminate a windowed app before Tk
        could display an exception. /usr/bin/security is present on supported
        macOS versions and fails as a normal Python exception instead of a hard
        process crash.
        """
        tool = Path("/usr/bin/security")
        if not tool.exists():
            raise OSError("The macOS Keychain command is unavailable.")
        service = cls.SERVICE.decode("utf-8")
        account = cls.ACCOUNT.decode("utf-8")
        if password is None:
            command = [str(tool), "find-generic-password", "-s", service, "-a", account, "-w"]
        else:
            command = [str(tool), "add-generic-password", "-U", "-s", service,
                       "-a", account, "-w", password]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError("macOS Keychain could not be opened.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Keychain rejected the request.").strip()
            write_diagnostic("macOS Keychain request failed: " + detail)
            raise OSError("The database credential could not be " +
                          ("read from" if password is None else "stored in") + " Keychain.")
        if password is None:
            value = result.stdout.rstrip("\r\n")
            if not value:
                raise OSError("The database credential in Keychain is empty.")
            return value
        return password

    def load(self) -> Tuple[str, str]:
        env = os.environ.get("FINANCE_DATABASE_URL", "").strip()
        if env:
            return env, "environment"
        if not self.path.exists():
            return "", "session"
        with self.path.open(encoding="utf-8") as handle:
            conf = json.load(handle)
        method = conf.get("credential_storage", "legacy")
        if method == "windows-dpapi" and sys.platform == "win32":
            return self._windows(base64.b64decode(conf["credential"]), True).decode("utf-8"), method
        if method == "macos-keychain" and sys.platform == "darwin":
            return self._mac(), method
        return str(conf.get("db_url", "")).strip(), "legacy" if conf.get("db_url") else "session"

    def save(self, url: str) -> str:
        if sys.platform == "win32":
            protected = base64.b64encode(self._windows(url.encode("utf-8"))).decode("ascii")
            atomic_json(self.path, {"credential_storage": "windows-dpapi", "credential": protected})
            return "windows-dpapi"
        if sys.platform == "darwin":
            self._mac(url)
            atomic_json(self.path, {"credential_storage": "macos-keychain"})
            return "macos-keychain"
        raise OSError("Secure credential storage is available on macOS and Windows. This connection will be used for the current session.")


class Repository:
    """Each task owns its connection. No Tk access and no long-lived idle transaction."""
    def __init__(self, url: str, directory: Path = DATA_DIR):
        if psycopg2 is None:
            raise ValidationError("The existing psycopg2 dependency is missing. Install psycopg2-binary in your Python environment.")
        try:
            self.parameters = parse_dsn(url)
        except Exception:
            raise ValidationError("The database connection URL is not valid.") from None
        host = self.parameters.get("host", "")
        local = host in ("localhost", "127.0.0.1", "::1") or host.startswith("/")
        mode = self.parameters.get("sslmode")
        if not local and mode in ("disable", "allow", "prefer"):
            self.parameters["sslmode"] = "require"
        elif not mode:
            self.parameters["sslmode"] = "prefer" if local else "require"
        # Never downgrade verify-ca / verify-full supplied by the user.
        self.parameters["connect_timeout"] = "10"
        self.parameters["application_name"] = "FinanceTracker/" + APP_VERSION
        identity = {k: self.parameters.get(k, "") for k in ("host", "port", "dbname", "user")}
        self.identity = fingerprint(identity)[:16]
        self.folder = directory / self.identity
        self.cache_path = self.folder / "last-successful-snapshot.json"
        self.backup_path: Optional[Path] = None
        self.storage_warning = ""

    def _connect(self):
        return psycopg2.connect(**self.parameters)

    @staticmethod
    def _limits(cur) -> None:
        cur.execute("SET LOCAL statement_timeout = '20000ms'")
        cur.execute("SET LOCAL lock_timeout = '8000ms'")

    @staticmethod
    def _read(cur) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        cur.execute("SELECT id, t_date, t_type, payload FROM transactions ORDER BY t_date, id")
        records = []
        for rid, t_date, t_type, payload in cur.fetchall():
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {"_database_error": "Invalid JSON payload."}
            if not isinstance(payload, dict):
                payload = {"_database_error": "Transaction payload is not an object."}
            item = copy.deepcopy(payload)
            if item.get("id") != rid or item.get("date") != t_date or item.get("type") != t_type:
                item["_database_error"] = "Database ID/date/type columns disagree with the transaction payload. Export a backup before repairing."
            item.setdefault("id", rid)
            records.append(item)
        cur.execute("SELECT key, value FROM settings")
        settings = {key: value for key, value in cur.fetchall()}
        # Non-finite, obsolete display settings must not break JSON backups.
        for key, value in list(settings.items()):
            if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                settings[key] = str(value)
        return records, settings

    def initialize(self) -> Snapshot:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cur:
                    self._limits(cur)
                    cur.execute("SELECT pg_advisory_xact_lock(7392146081200)")
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value FLOAT)")
                    cur.execute("CREATE TABLE IF NOT EXISTS transactions (id VARCHAR(255) PRIMARY KEY, t_date VARCHAR(50), t_type VARCHAR(50), payload JSONB)")
                    cur.execute("CREATE TABLE IF NOT EXISTS finance_tracker_operations (operation_id VARCHAR(64) PRIMARY KEY, request_hash VARCHAR(64) NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                    # Review ticks live beside the ledger, never inside a transaction's payload.
                    cur.execute("CREATE TABLE IF NOT EXISTS finance_tracker_checks (transaction_id VARCHAR(255) PRIMARY KEY, record_fingerprint VARCHAR(64) NOT NULL, checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                    records, settings = self._read(cur)
                    checks = self._read_checks(cur)
            snapshot = project(records, settings)
            snapshot.checks = checks
            # A baseline backup is REQUIRED before this client is allowed to write.
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = self.folder / "backups" / f"before-v2-{stamp}-{uuid.uuid4().hex[:6]}.json"
            atomic_json(backup_path, backup_document(snapshot))
            self.backup_path = backup_path
            self._prune_backups()
            self._cache(snapshot)
            return snapshot
        finally:
            connection.close()

    @staticmethod
    def _read_checks(cur) -> Dict[str, Dict[str, str]]:
        cur.execute("SELECT transaction_id, record_fingerprint, checked_at FROM finance_tracker_checks")
        result = {}
        for rid, fp, at in cur.fetchall():
            result[str(rid)] = {"fp": str(fp), "at": at.isoformat() if hasattr(at, "isoformat") else str(at)}
        return result

    def set_checks(self, changes: Dict[str, Optional[str]]) -> Dict[str, Dict[str, str]]:
        """Tick (fingerprint) or untick (None) rows. Touches no financial data."""
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cur:
                    self._limits(cur)
                    for rid, fp in changes.items():
                        if fp is None:
                            cur.execute("DELETE FROM finance_tracker_checks WHERE transaction_id = %s", (rid,))
                        else:
                            cur.execute("INSERT INTO finance_tracker_checks (transaction_id, record_fingerprint, checked_at) "
                                        "VALUES (%s, %s, NOW()) ON CONFLICT (transaction_id) DO UPDATE SET "
                                        "record_fingerprint = EXCLUDED.record_fingerprint, checked_at = NOW()", (rid, fp))
                    return self._read_checks(cur)
        finally:
            connection.close()

    def _prune_backups(self) -> None:
        """Keep the newest startup backups; never touch exports saved elsewhere."""
        try:
            folder = self.folder / "backups"
            files = sorted(folder.glob("before-v2-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[KEEP_BACKUPS:]:
                if old != self.backup_path:
                    old.unlink()
        except OSError as exc:
            write_diagnostic("Old startup backups could not be pruned.", exc)

    def _cache(self, snapshot: Snapshot) -> None:
        try:
            atomic_json(self.cache_path, backup_document(snapshot))
        except OSError:
            self.storage_warning = "The last-successful local cache could not be updated. Export a backup to a writable folder."

    def read_cache(self) -> Optional[Snapshot]:
        try:
            with self.cache_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            snapshot = project(value["transactions"], value.get("legacy_settings", {}))
            snapshot.fetched_at = value.get("exported_at", "Unknown")
            checks = value.get("review_checks", {})
            snapshot.checks = checks if isinstance(checks, dict) else {}
            return snapshot
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def fetch(self) -> Snapshot:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cur:
                    self._limits(cur)
                    records, settings = self._read(cur)
                    checks = self._read_checks(cur)
            snapshot = project(records, settings)
            snapshot.checks = checks
            self._cache(snapshot)
            return snapshot
        finally:
            connection.close()

    def write(self, command: Command) -> Snapshot:
        if self.backup_path is None or not self.backup_path.is_file():
            raise ValidationError("Reconnect first so a baseline backup can be created before writing.")
        connection = self._connect()
        try:
            # Read committed means the SELECT after acquiring the lock sees the
            # latest committed writer, not a stale snapshot taken before waiting.
            connection.set_session(isolation_level="READ COMMITTED", readonly=False, autocommit=False)
            with connection:
                with connection.cursor() as cur:
                    self._limits(cur)
                    cur.execute("LOCK TABLE transactions IN SHARE ROW EXCLUSIVE MODE")
                    if command.action == "display_rates":
                        # Lock in the same order on every client. Keep the two
                        # display settings atomic, including writes by older apps.
                        cur.execute("LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE")
                    cur.execute("SELECT request_hash FROM finance_tracker_operations WHERE operation_id = %s", (command.operation_id,))
                    existing = cur.fetchone()
                    records, settings = self._read(cur)
                    if existing:
                        if existing[0] != command.digest:
                            raise ConflictError("An operation ID was reused for different data. Reopen the form.")
                        snapshot = project(records, settings)
                    else:
                        if command.action == "display_rates":
                            settings = apply_display_rates(settings, command)
                            for key in DISPLAY_RATE_KEYS.values():
                                cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) "
                                            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                                            (key, Decimal(settings[key])))
                            proposed = records
                        else:
                            proposed, changed = apply_command(records, command)
                            existing_ids = {r["id"] for r in records}
                            for item in changed:
                                if item["id"] in existing_ids:
                                    cur.execute("UPDATE transactions SET t_date=%s, t_type=%s, payload=%s WHERE id=%s",
                                                (item["date"], item["type"], Json(item, dumps=canonical_json), item["id"]))
                                    if cur.rowcount != 1:
                                        raise ConflictError("The record changed while saving. Refresh and retry.")
                                else:
                                    cur.execute("INSERT INTO transactions (id, t_date, t_type, payload) VALUES (%s, %s, %s, %s)",
                                                (item["id"], item["date"], item["type"], Json(item, dumps=canonical_json)))
                        cur.execute("INSERT INTO finance_tracker_operations (operation_id, request_hash) VALUES (%s, %s)",
                                    (command.operation_id, command.digest))
                        snapshot = project(proposed, settings)
                    snapshot.checks = self._read_checks(cur)
            # Do not treat a cache failure as a failed committed financial write.
            self._cache(snapshot)
            return snapshot
        finally:
            connection.close()


class DemoRepository:
    """Explicitly isolated preview mode: no real connection, files or money."""
    def __init__(self, records: Optional[List[Dict[str, Any]]] = None,
                 settings: Optional[Dict[str, Any]] = None):
        self.records = copy.deepcopy(records if records is not None else demo_records())
        # Explicitly fictional demo rates, never copied into a real database.
        self.settings = copy.deepcopy(settings if settings is not None else
                                      {"display_rate": 237.0, "display_rate_eur": 240.0})
        self.operations: Dict[str, str] = {}
        self.checks: Dict[str, Dict[str, str]] = {}
        self.lock = threading.Lock()
        self.parameters = {"host": "DEMO - no database", "dbname": "Sample records", "sslmode": "not applicable"}
        self.backup_path = None
        self.storage_warning = ""
        self.identity = "demo"

    def initialize(self) -> Snapshot:
        return self.fetch()

    def fetch(self) -> Snapshot:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> Snapshot:
        snapshot = project(self.records, self.settings)
        snapshot.checks = copy.deepcopy(self.checks)
        return snapshot

    def set_checks(self, changes: Dict[str, Optional[str]]) -> Dict[str, Dict[str, str]]:
        with self.lock:
            for rid, fp in changes.items():
                if fp is None:
                    self.checks.pop(rid, None)
                else:
                    self.checks[rid] = {"fp": fp, "at": now_utc()}
            return copy.deepcopy(self.checks)

    def write(self, command: Command) -> Snapshot:
        with self.lock:
            if command.operation_id in self.operations:
                if self.operations[command.operation_id] != command.digest:
                    raise ConflictError("Operation ID reused with different data.")
                return self._snapshot()
            if command.action == "display_rates":
                self.settings = apply_display_rates(self.settings, command)
            else:
                self.records, _ = apply_command(self.records, command)
            self.operations[command.operation_id] = command.digest
            return self._snapshot()


def demo_records() -> List[Dict[str, Any]]:
    today = date.today()
    month = today.replace(day=1).isoformat()
    rows: List[Dict[str, Any]] = []
    def add(kind, **kw):
        kw.setdefault("date", month)
        item = record_payload(kind, kw)
        rows.append(item)
        return item["id"]
    start = date.fromisoformat(month_add(month[:7], -5) + "-01").isoformat()
    add("opening", account="usd_bank", amount="4200", description="Starting USD balance", date=start)
    add("opening", account="eur_bank", amount="1950", description="Starting EUR balance", date=start)
    add("opening", account="dzd_cash", amount="185000", description="Starting dinar balance", date=start)
    # Five earlier months of lighter activity so the charts have a history.
    history = [("900", "34000", "12000"), ("1400", "41000", "9500"), ("650", "29000", "15000"),
               ("1800", "52000", "11000"), ("1100", "38000", "8000")]
    for offset, (usd, dzd_spent, food) in zip(range(5, 0, -1), history):
        day = month_add(month[:7], -offset) + "-05"
        add("income", account="usd_bank", amount=usd, description="Client project", date=day)
        add("income", account="dzd_cash", amount="48000", description="Local project", date=day)
        add("expense", account="usd_bank", amount=str(int(usd) // 4), category="Business",
            description="Subscriptions and gear", date=day)
        add("expense", account="dzd_cash", amount=dzd_spent, category="Essentials",
            description="Rent and bills", date=day)
        add("expense", account="dzd_cash", amount=food, category="Food", description="Groceries", date=day)
    add("expense", account="dzd_cash", amount="6400", category="Food", description="Groceries")
    add("expense", account="dzd_cash", amount="3500", category="Shopping", description="Clothes")
    add("income", account="usd_bank", amount="1250", description="Brand video project")
    add("income", account="eur_bank", amount="680", description="Monthly editing retainer")
    add("income", account="dzd_cash", amount="18000", description="Local project")
    add("transfer", from_account="dzd_cash", to_account="eur_cash", amount_sent="72000", amount_received="300", description="Bought euros in cash")
    add("transfer", from_account="usd_bank", to_account="dzd_cash", amount_sent="400", amount_received="94800", description="USD exchange")
    add("expense", account="usd_bank", amount="59.99", category="Business", description="Editing software")
    add("expense", account="dzd_cash", amount="9200", category="Essentials", description="Internet and utilities")
    add("expense", account="eur_cash", amount="35", category="Transport", description="Train tickets")
    add("savings_deposit", account="usd_bank", amount="1000", description="Emergency fund")
    add("savings_deposit", account="eur_bank", amount="400", description="Equipment reserve")
    loan = add("loan_out", account="eur_bank", amount="200", borrower="Samir", description="Short-term loan")
    add("loan_repaid", account="eur_cash", amount="50", borrower="Samir", loan_id=loan, description="First repayment")
    return rows


class Worker:
    """Queue-based worker; all callbacks are dispatched on Tk's main thread."""
    def __init__(self):
        self.tasks: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="finance-database", daemon=True)
        self.thread.start()

    def submit(self, function: Callable, callback: Callable) -> None:
        self.tasks.put((function, callback))

    def _run(self) -> None:
        while True:
            task = self.tasks.get()
            if task is None:
                return
            function, callback = task
            try:
                self.results.put((callback, function(), None))
            except Exception as exc:
                self.results.put((callback, None, exc))

    def close(self) -> None:
        self.tasks.put(None)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return str(exc)
    if isinstance(exc, OSError):
        return "A required local file could not be read or saved. Check folder permissions and free disk space."
    code = getattr(exc, "pgcode", "")
    if code == "42501":
        return "The database role lacks a required permission. This version needs access to transactions/settings and permission to create its operation-log table."
    if code in ("55P03", "57014"):
        return "The database is busy or took too long to respond. Retry; the same operation ID prevents a duplicate save."
    if psycopg2 is not None and isinstance(exc, psycopg2.Error):
        return "The database could not confirm this request. Check your internet connection and database access, then retry. Previously loaded balances have been kept."
    return "This request could not be completed. No new balance has been assumed. Refresh or reconnect, then retry."

# ---------------------------------------------------------------------------
# Apple-inspired dark desktop UI. Standard-library Tkinter only.
# ---------------------------------------------------------------------------
P = {
    "bg": "#0B0B0D",
    "sidebar": "#101012",
    "surface": "#161618",
    "card": "#1C1C1E",
    "card_alt": "#202023",
    "card_hover": "#26262A",
    "sheet": "#141416",
    "field": "#242426",
    "border": "#303034",
    "hairline": "#28282B",
    "hover": "#2C2C2E",
    "nav_active": "#23232A",
    "nav_hover": "#1B1B1F",
    "hero": "#111E2C",
    "hero_border": "#1F3A55",
    "text": "#F5F5F7",
    "muted": "#A1A1AA",
    "dim": "#6E6E73",
    "accent": "#0A84FF",
    "accent_dark": "#409CFF",
    "green": "#30D158",
    "red": "#FF453A",
    "amber": "#FF9F0A",
    "purple": "#BF5AF2",
    "cyan": "#64D2FF",
    "selection": "#17446A",
    "white": "#FFFFFF",
}

ACTIVITY_COLORS = {
    "income": P["green"],
    "expense": P["red"],
    "transfer": P["accent_dark"],
    "savings_deposit": P["cyan"],
    "savings_withdraw": "#5DEB8B",
    "loan_out": P["amber"],
    "loan_repaid": P["green"],
    "opening": P["purple"],
    "adjustment": P["purple"],
}
# Chart pair validated for dark surfaces and colour-vision deficiency
# (income teal-green vs expense red); both always carry a legend label.
CHART_INCOME = "#1E9E8F"
CHART_EXPENSE = "#FF453A"
FONT_TEXT_FAMILY = "SF Pro Text"
FONT_DISPLAY_FAMILY = "SF Pro Display"


def activity_color(record: Record) -> str:
    return P["dim"] if record.voided else ACTIVITY_COLORS.get(record.kind, P["text"])


def activity_tags(record: Record, index: int) -> Tuple[str, ...]:
    stripe = ("alternate",) if index % 2 else ()
    return stripe + (("voided",) if record.voided else (record.kind,))


def font(size: int = 14, bold: bool = False) -> Tuple[str, int, str]:
    family = FONT_DISPLAY_FAMILY if size >= 20 else FONT_TEXT_FAMILY
    return family, -size, "bold" if bold else "normal"


def label(parent, text="", size=14, color=None, bold=False, bg=None, **kwargs):
    kwargs.setdefault("anchor", "w")
    return tk.Label(parent, text=text, font=font(size, bold), fg=color or P["text"],
                    bg=bg or parent.cget("bg"), bd=0, **kwargs)


def card(parent, *, tone=None, border=True, border_color=None, **kwargs):
    background = tone or P["card"]
    return tk.Frame(parent, bg=background,
                    highlightbackground=border_color or P["border"],
                    highlightthickness=1 if border else 0, bd=0, **kwargs)


def line(parent, pady=16, color=None):
    tk.Frame(parent, bg=color or P["hairline"], height=1).pack(fill="x", pady=pady)


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                 radius: float, *, fill: str, outline: str = "", width: int = 1,
                 tags=None):
    radius = max(1, min(radius, (x2-x1)/2, (y2-y1)/2))
    points = [
        x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
        x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
        x1, y2, x1, y2-radius, x1, y1+radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24,
                                 fill=fill, outline=outline, width=width, tags=tags)


class Button(tk.Canvas):
    """Small native-safe rounded button with Apple system-color variants."""
    def __init__(self, parent, text, command=None, *, variant="primary", width=None,
                 height=40, small=False):
        self.text, self.command, self.variant = text, command, variant
        self.enabled = True
        self.hover = False
        self.focused = False
        self.font_spec = font(11 if small else 13, True)
        calculated = tkfont.Font(font=self.font_spec).measure(text) + (24 if small else 32)
        super().__init__(parent, width=width or calculated, height=height,
                         bg=parent.cget("bg"), highlightthickness=0, bd=0, takefocus=1)
        safe_cursor(self, True)
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))

    def _focus(self, focused):
        self.focused = focused
        self._draw()

    def _hover(self, hover):
        self.hover = hover
        self._draw()

    def _click(self, event=None):
        if self.enabled and self.command:
            self.focus_set()
            self.command()
        return "break"

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(takefocus=1 if enabled else 0)
        safe_cursor(self, self.enabled)
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def _draw(self, event=None):
        if not self.winfo_exists():
            return
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        parent_bg = self.master.cget("bg")
        if self.variant == "primary":
            fill = P["accent_dark"] if self.hover else P["accent"]
            ink, edge = P["white"], fill
        elif self.variant == "danger":
            fill = "#4B2020" if self.hover else "#351B1B"
            ink, edge = P["red"], "#66302F"
        elif self.variant == "ghost":
            fill = P["hover"] if self.hover else parent_bg
            ink, edge = P["text"], fill
        else:
            fill = P["hover"] if self.hover else P["surface"]
            ink, edge = P["text"], P["border"]
        if not self.enabled:
            fill, ink, edge = P["surface"], P["dim"], P["hairline"]
        if self.focused and self.enabled:
            edge = P["accent_dark"]
        self.delete("all")
        rounded_rect(self, 1, 1, w-1, h-1, min(12, h/2), fill=fill,
                     outline=edge, width=1)
        self.create_text(w/2, h/2, text=self.text, fill=ink, font=self.font_spec)


class Field(tk.Frame):
    def __init__(self, parent, title, value="", *, choices=None, hint="", secret=False,
                 large=False, placeholder=""):
        super().__init__(parent, bg=parent.cget("bg"))
        self.title_label = label(self, title, 12, P["muted"])
        self.title_label.pack(fill="x", pady=(0, 7))
        self.var = tk.StringVar(value=value)
        self.is_combo = choices is not None
        self.box = None
        if choices is not None:
            self.input = ttk.Combobox(self, textvariable=self.var, values=list(choices),
                                      state="readonly", style="Finance.TCombobox",
                                      font=font(14))
            self.input.pack(fill="x", ipady=7)
        else:
            self.box = tk.Frame(self, bg=P["field"], highlightthickness=1,
                                highlightbackground=P["border"])
            self.box.pack(fill="x")
            self.input = tk.Entry(
                self.box, textvariable=self.var, font=font(25 if large else 14),
                bg=P["field"], fg=P["text"], insertbackground=P["text"],
                readonlybackground=P["field"], disabledbackground=P["field"],
                disabledforeground=P["dim"], relief="flat", bd=0,
                highlightthickness=0, show="•" if secret else "",
                selectbackground=P["selection"], selectforeground=P["white"])
            self.input.pack(fill="x", padx=13, pady=12 if large else 11)
            self.input.bind("<FocusIn>", lambda e: self.box.configure(
                highlightbackground=P["accent"]))
            self.input.bind("<FocusOut>", lambda e: self.box.configure(
                highlightbackground=P["border"]))
            if placeholder:
                ghost = tk.Label(self.box, text=placeholder, font=font(25 if large else 14),
                                 bg=P["field"], fg=P["dim"], bd=0)
                ghost.bind("<Button-1>", lambda e: self.input.focus_set())

                def show_ghost(*args):
                    if self.var.get():
                        ghost.place_forget()
                    else:
                        ghost.place(x=13, rely=.5, anchor="w")
                self.var.trace_add("write", show_ghost)
                show_ghost()
        self.hint = label(self, hint, 11, P["dim"], wraplength=540, justify="left")
        if hint:
            self.hint.pack(fill="x", pady=(6, 0))

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value)

    def enable(self, enabled=True):
        self.input.configure(state=("readonly" if self.is_combo else "normal") if enabled else "disabled")

    def options(self, options):
        if self.is_combo:
            self.input.configure(values=list(options))


class ScrollArea(tk.Frame):
    def __init__(self, parent, *, bg=None):
        super().__init__(parent, bg=bg or parent.cget("bg"))
        self.canvas = tk.Canvas(self, bg=self.cget("bg"), bd=0, highlightthickness=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Finance.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.body = tk.Frame(self.canvas, bg=self.cget("bg"))
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._content)
        self.canvas.bind("<Configure>", self._resize)

    def _content(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._toggle_bar()

    def _resize(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)
        self._toggle_bar()

    def _toggle_bar(self):
        """Show the scrollbar only when there is something to scroll."""
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height() + 1
        shown = bool(self.bar.winfo_manager())
        if needed and not shown:
            self.bar.pack(side="right", fill="y", before=self.canvas)
        elif not needed and shown and self.canvas.winfo_height() > 1:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def wheel(self, event):
        if self.body.winfo_height() <= self.canvas.winfo_height():
            return
        if getattr(event, "num", 0) in (4, 5):
            units = -3 if event.num == 4 else 3
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) * 3 if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")


_FONT_CACHE: Dict[Tuple[int, bool, str, str], Any] = {}


def text_width(text: str, size: int, bold: bool = False) -> int:
    key = (size, bold, FONT_TEXT_FAMILY, FONT_DISPLAY_FAMILY)
    measured = _FONT_CACHE.get(key)
    if measured is None:
        measured = _FONT_CACHE[key] = tkfont.Font(font=font(size, bold))
    return measured.measure(text)


def fit_label(parent, text, size, color=None, bold=True, minimum=13, **kwargs):
    """A label that shrinks its font to fit the space it is given, never clipping."""
    widget = label(parent, text, size, color, bold, width=1, **kwargs)
    state = {"size": size, "width": 0}

    def refit(event=None):
        width = widget.winfo_width()
        if width <= 4 or width == state["width"]:
            return
        state["width"] = width
        best = minimum
        for candidate in range(size, minimum - 1, -1):
            if text_width(text, candidate, bold) <= width - 4:
                best = candidate
                break
        if best != state["size"]:
            state["size"] = best
            widget.configure(font=font(best, bold))
    widget.bind("<Configure>", refit, add="+")
    return widget


def short_amount(value: Decimal) -> str:
    """Compact axis text: 1.2M, 350K, 900."""
    v = float(value)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(v) >= limit:
            text = f"{v / limit:.1f}".rstrip("0").rstrip(".")
            return text + suffix
    return f"{v:,.0f}"


def nice_ceiling(value: float, steps: int = 4) -> Tuple[float, float]:
    """A round axis maximum and step so gridlines land on readable numbers."""
    if value <= 0:
        return 1.0, 0.25
    raw = value / steps
    magnitude = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * magnitude
        if step >= raw:
            break
    return step * steps, step


def month_label(month: str, long: bool = False) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y" if long else "%b")


def day_heading(day: str) -> str:
    value = date.fromisoformat(day)
    delta = (date.today() - value).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{value:%A} {value.day} {value:%B %Y}"


class TrendChart(tk.Canvas):
    """Monthly income and expenses in DZD, grouped bars with a hover tooltip."""
    def __init__(self, parent, series, *, show=("income", "expense"), height=230,
                 selected=None):
        super().__init__(parent, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self.series, self.show, self.selected = series, show, selected
        self.hover_index: Optional[int] = None
        self.groups: List[Tuple[float, float]] = []
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))

    def _set_hover(self, index):
        if index != self.hover_index:
            self.hover_index = index
            self.draw()

    def _motion(self, event):
        for i, (x1, x2) in enumerate(self.groups):
            if x1 <= event.x <= x2:
                self._set_hover(i)
                return
        self._set_hover(None)

    def draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = max(40, self.winfo_width()), max(40, self.winfo_height())
        colors = {"income": CHART_INCOME, "expense": CHART_EXPENSE}
        names = {"income": "Income", "expense": "Expenses"}
        top = 34 if len(self.show) > 1 else 14
        left, right, bottom = 54, 8, 28
        if len(self.show) > 1:
            x = left
            for kind in self.show:
                self.create_rectangle(x, 9, x + 10, 19, fill=colors[kind], outline="")
                self.create_text(x + 16, 14, text=names[kind], anchor="w",
                                 fill=P["muted"], font=font(11))
                x += 28 + text_width(names[kind], 11)
        peak = max([float(v) for _, inc, exp in self.series
                    for v, kind in ((inc, "income"), (exp, "expense")) if kind in self.show] + [0])
        axis_max, step = nice_ceiling(peak)
        plot_h = h - top - bottom

        def y_of(value):
            return top + plot_h - (float(value) / axis_max) * plot_h

        tick = 0.0
        while tick <= axis_max + 1e-9:
            y = y_of(tick)
            self.create_line(left, y, w - right, y, fill=P["hairline"])
            self.create_text(left - 10, y, text=short_amount(Decimal(str(tick))), anchor="e",
                             fill=P["dim"], font=font(10))
            tick += step
        count = max(1, len(self.series))
        group_w = (w - left - right) / count
        bar_w = max(6, min(26, group_w * (0.28 if len(self.show) > 1 else 0.42)))
        self.groups = []
        for i, (month, inc, exp) in enumerate(self.series):
            gx1 = left + i * group_w
            self.groups.append((gx1, gx1 + group_w))
            if i == self.hover_index:
                rounded_rect(self, gx1 + 4, top - 6, gx1 + group_w - 4, h - bottom + 2, 8,
                             fill=P["card_hover"])
            center = gx1 + group_w / 2
            values = [(kind, inc if kind == "income" else exp) for kind in self.show]
            total_w = len(values) * bar_w + (len(values) - 1) * 2
            x = center - total_w / 2
            for kind, value in values:
                y1, y2 = y_of(value), top + plot_h
                if y2 - y1 >= 1:
                    radius = min(4, (y2 - y1) / 2, bar_w / 2)
                    rounded_rect(self, x, y1, x + bar_w, min(y2, y1 + 2 * radius + 1), radius,
                                 fill=colors[kind])
                    self.create_rectangle(x, y1 + radius, x + bar_w, y2, fill=colors[kind], outline="")
                x += bar_w + 2
            chosen = month == self.selected
            self.create_text(center, h - bottom + 15, text=month_label(month),
                             fill=P["text"] if chosen else P["dim"], font=font(11, chosen))
        self.create_line(left, top + plot_h, w - right, top + plot_h, fill=P["border"])
        if self.hover_index is not None and self.hover_index < len(self.series):
            month, inc, exp = self.series[self.hover_index]
            lines = [month_label(month, True)]
            if "income" in self.show:
                lines.append(f"Income   ≈ {inc:,.0f} DZD")
            if "expense" in self.show:
                lines.append(f"Expenses ≈ {exp:,.0f} DZD")
            if len(self.show) > 1:
                lines.append(f"Net      ≈ {inc - exp:,.0f} DZD")
            box_w = max(text_width(t, 11, i == 0) for i, t in enumerate(lines)) + 24
            box_h = 12 + 18 * len(lines)
            gx1, gx2 = self.groups[self.hover_index]
            bx = gx2 + 6 if gx2 + 6 + box_w <= w else gx1 - 6 - box_w
            bx = max(0, bx)
            by = top
            rounded_rect(self, bx, by, bx + box_w, by + box_h, 8, fill=P["sheet"],
                         outline=P["border"])
            for i, text in enumerate(lines):
                self.create_text(bx + 12, by + 15 + 18 * i, text=text, anchor="w",
                                 fill=P["text"] if i == 0 else P["muted"], font=font(11, i == 0))


class CategoryChart(tk.Canvas):
    """Ranked horizontal bars of spending per category, valued in DZD."""
    ROW = 34

    def __init__(self, parent, items):
        self.items = items
        super().__init__(parent, height=max(1, len(items)) * self.ROW + 6,
                         bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.hover: Optional[int] = None
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set(None))

    def _set(self, index):
        if index != self.hover:
            self.hover = index
            self.draw()

    def _motion(self, event):
        index = int(event.y // self.ROW)
        self._set(index if 0 <= index < len(self.items) else None)

    def draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w = max(80, self.winfo_width())
        total = sum((v for _, v in self.items), ZERO) or Decimal(1)
        peak = max((v for _, v in self.items), default=Decimal(1)) or Decimal(1)
        name_w = min(130, max(text_width(n, 12) for n, _ in self.items) + 14)
        value_w = max(text_width(f"{v:,.0f} DZD", 12, True) for _, v in self.items) + 58
        bar_x, bar_max = name_w, max(20, w - name_w - value_w - 10)
        for i, (name, value) in enumerate(self.items):
            y = i * self.ROW + self.ROW / 2
            if i == self.hover:
                rounded_rect(self, 0, i * self.ROW + 2, w, (i + 1) * self.ROW, 8,
                             fill=P["card_hover"])
            self.create_text(0 + 8, y, text=name, anchor="w", fill=P["text"], font=font(12))
            length = max(4, float(value / peak) * bar_max)
            rounded_rect(self, bar_x, y - 5, bar_x + length, y + 5, 4, fill=CHART_EXPENSE)
            share = (value / total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            self.create_text(w - 8, y, text=f"{share}%", anchor="e", fill=P["dim"], font=font(11))
            self.create_text(w - 48, y, text=f"{value:,.0f} DZD", anchor="e",
                             fill=P["text"], font=font(12, True))


class DateField(tk.Frame):
    """Date entry with Today / Yesterday shortcuts and an inline month calendar."""
    def __init__(self, parent, title, value=""):
        super().__init__(parent, bg=parent.cget("bg"))
        bg = self.cget("bg")
        self.title_label = label(self, title, 12, P["muted"])
        self.title_label.pack(fill="x", pady=(0, 7))
        row = tk.Frame(self, bg=bg)
        row.pack(fill="x")
        self.box = tk.Frame(row, bg=P["field"], highlightthickness=1,
                            highlightbackground=P["border"])
        self.box.pack(side="left", fill="x", expand=True)
        self.var = tk.StringVar(value=value)
        self.input = tk.Entry(
            self.box, textvariable=self.var, font=font(14), bg=P["field"], fg=P["text"],
            insertbackground=P["text"], disabledbackground=P["field"],
            disabledforeground=P["dim"], relief="flat", bd=0, highlightthickness=0,
            width=12, selectbackground=P["selection"], selectforeground=P["white"])
        self.input.pack(fill="x", padx=13, pady=11)
        self.input.bind("<FocusIn>", lambda e: self.box.configure(highlightbackground=P["accent"]))
        self.input.bind("<FocusOut>", lambda e: self.box.configure(highlightbackground=P["border"]))
        self.chips = [
            Button(row, "Today", lambda: self.pick(date.today()), variant="secondary", small=True, height=44),
            Button(row, "Yesterday", lambda: self.pick(date.fromordinal(date.today().toordinal() - 1)),
                   variant="secondary", small=True, height=44),
            Button(row, "Calendar", self.toggle, variant="secondary", small=True, height=44)]
        for chip in self.chips:
            chip.pack(side="left", padx=(8, 0))
        self.hint = label(self, "", 11, P["dim"])
        self.hint.pack(fill="x", pady=(6, 0))
        self.calendar = tk.Frame(self, bg=P["card"], highlightthickness=1,
                                 highlightbackground=P["border"])
        self.calendar_open = False
        self.month = date.today().replace(day=1)
        self.var.trace_add("write", lambda *a: self._describe())
        self._describe()

    def _parsed(self) -> Optional[date]:
        try:
            return date.fromisoformat(self.var.get().strip())
        except ValueError:
            return None

    def _describe(self):
        value = self._parsed()
        if value is None:
            self.hint.configure(text="Use YYYY-MM-DD, or pick a day.", fg=P["amber"])
        elif value > date.today():
            self.hint.configure(text="Future dates are not supported.", fg=P["amber"])
        else:
            self.hint.configure(text=day_heading(value.isoformat()), fg=P["dim"])
        if self.calendar_open:
            self._draw_calendar()

    def pick(self, value: date):
        self.var.set(value.isoformat())
        if self.calendar_open:
            self.toggle()

    def toggle(self):
        self.calendar_open = not self.calendar_open
        if self.calendar_open:
            self.month = (self._parsed() or date.today()).replace(day=1)
            self._draw_calendar()
            self.calendar.pack(fill="x", pady=(10, 0))
            self.chips[2].set_text("Close")
        else:
            self.calendar.pack_forget()
            self.chips[2].set_text("Calendar")
        dialog = self.winfo_toplevel()
        if hasattr(dialog, "fit"):
            dialog.fit()

    def _shift(self, delta):
        self.month = date.fromisoformat(month_add(self.month.isoformat()[:7], delta) + "-01")
        self._draw_calendar()

    def _draw_calendar(self):
        for child in self.calendar.winfo_children():
            child.destroy()
        surface = P["card"]
        head = tk.Frame(self.calendar, bg=surface)
        head.pack(fill="x", padx=12, pady=(10, 6))
        Button(head, "‹", lambda: self._shift(-1), variant="ghost", width=32, height=30,
               small=True).pack(side="left")
        future = self.month > date.today().replace(day=1)
        next_button = Button(head, "›", lambda: self._shift(1), variant="ghost", width=32,
                             height=30, small=True)
        next_button.pack(side="right")
        next_button.set_enabled(self.month < date.today().replace(day=1))
        label(head, f"{self.month:%B %Y}", 13, P["text"], True, bg=surface,
              anchor="center").pack(side="left", fill="x", expand=True)
        grid = tk.Frame(self.calendar, bg=surface)
        grid.pack(padx=12, pady=(0, 12))
        for c, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            label(grid, name, 10, P["dim"], True, bg=surface, anchor="center",
                  width=4).grid(row=0, column=c, pady=(0, 4))
        chosen, today = self._parsed(), date.today()
        first = self.month
        offset = first.weekday()
        day_count = (date.fromisoformat(month_add(first.isoformat()[:7], 1) + "-01") - first).days
        for n in range(day_count):
            value = first.replace(day=n + 1)
            r, c = divmod(offset + n, 7)
            is_future = value > today or future
            selected = value == chosen
            cell = tk.Label(grid, text=str(n + 1), width=4, pady=3, font=font(12, selected),
                            bg=P["accent"] if selected else surface,
                            fg=P["white"] if selected else P["dim"] if is_future else
                            P["accent_dark"] if value == today else P["text"])
            cell.grid(row=r + 1, column=c, padx=1, pady=1)
            if not is_future:
                safe_cursor(cell, True)
                cell.bind("<Button-1>", lambda e, v=value: self.pick(v))
                if not selected:
                    cell.bind("<Enter>", lambda e, w=cell: w.configure(bg=P["hover"]))
                    cell.bind("<Leave>", lambda e, w=cell: w.configure(bg=surface))

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value)

    def enable(self, enabled=True):
        self.input.configure(state="normal" if enabled else "disabled")
        for chip in self.chips:
            chip.set_enabled(enabled)
        if not enabled and self.calendar_open:
            self.toggle()


class Scrim(tk.Toplevel):
    """A translucent layer over the main window while a dialog is open."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg="#000000")
        self.attributes("-alpha", 0.5)
        self.bind("<Button-1>", lambda e: app.lift_modal())

    def cover(self):
        self.app.update_idletasks()
        self.geometry(f"{self.app.winfo_width()}x{self.app.winfo_height()}"
                      f"+{self.app.winfo_rootx()}+{self.app.winfo_rooty()}")


def draw_icon(canvas, name, color, ox=0, oy=0):
    """Minimal line icons, intentionally close to the weight of SF Symbols."""
    def l(points, width=1.7):
        coords = []
        for i, value in enumerate(points):
            coords.append(value + (ox if i % 2 == 0 else oy))
        canvas.create_line(*coords, fill=color, width=width, capstyle="round",
                           joinstyle="round")
    def r(x, y, a, b, radius=False):
        if radius:
            canvas.create_oval(x+ox, y+oy, a+ox, b+oy, outline=color, width=1.6)
        else:
            canvas.create_rectangle(x+ox, y+oy, a+ox, b+oy, outline=color, width=1.5)
    if name == "overview":
        r(3, 3, 9, 9); r(13, 3, 19, 9); r(3, 13, 9, 19); r(13, 13, 19, 19)
    elif name == "income":
        l([11, 3, 11, 19]); l([5, 13, 11, 19, 17, 13])
    elif name == "expenses":
        l([11, 19, 11, 3]); l([5, 9, 11, 3, 17, 9])
    elif name == "transfers":
        l([3, 7, 19, 7, 15, 3]); l([19, 16, 3, 16, 7, 20])
    elif name == "savings":
        l([4, 7, 11, 3, 18, 7, 18, 13, 15, 18, 11, 20, 7, 18, 4, 13, 4, 7])
        l([8, 11, 10, 14, 15, 9])
    elif name == "lending":
        r(7, 2, 15, 10, True); l([3, 20, 3, 17, 7, 14, 15, 14, 19, 17, 19, 20])
    elif name == "settings":
        for y, x in [(5, 8), (11, 16), (18, 7)]:
            l([3, y, 20, y], 1.4); r(x-2, y-2, x+2, y+2, True)
    else:
        for y in (5, 11, 18):
            l([7, y, 20, y], 1.5); l([2, y, 3, y], 2.2)


class NavButton(tk.Canvas):
    def __init__(self, parent, page, text, command):
        self.page, self.text, self.command = page, text, command
        self.active = False
        self.hovered = False
        super().__init__(parent, height=44, bg=parent.cget("bg"), bd=0,
                         highlightthickness=0, takefocus=1)
        safe_cursor(self, True)
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self.set_hover(True))
        self.bind("<Leave>", lambda e: self.set_hover(False))
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())

    def set_active(self, value):
        self.active = bool(value)
        self._draw()

    def set_hover(self, value):
        self.hovered = bool(value)
        self._draw()

    def _draw(self, event=None):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        if self.active or self.hovered:
            rounded_rect(self, 4, 3, w-4, h-3, 11,
                         fill=P["nav_active"] if self.active else P["nav_hover"])
        if self.active:
            rounded_rect(self, 4, 12, 7, h-12, 2, fill=P["accent"])
        color = P["accent_dark"] if self.active else P["muted"]
        draw_icon(self, self.page, color, ox=18, oy=11)
        self.create_text(54, h/2, text=self.text, anchor="w",
                         fill=P["text"] if self.active else P["muted"],
                         font=font(13, self.active))


class Modal(tk.Toplevel):
    def __init__(self, app, title, subtitle="", width=680, height=680):
        app.show_scrim()
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.title(title + " | Finance")
        self.configure(bg=P["bg"])
        self.transient(app)
        self.resizable(True, True)
        actual_height = min(height, self.winfo_screenheight() - 90)
        actual_width = min(width, self.winfo_screenwidth() - 50)
        x = app.winfo_rootx() + max(0, (app.winfo_width() - actual_width)//2)
        y = app.winfo_rooty() + max(0, (app.winfo_height() - actual_height)//2)
        self.geometry(f"{actual_width}x{actual_height}+{x}+{y}")
        self._width = actual_width
        self.minsize(480, 470)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=P["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=32, pady=(29, 18))
        label(header, title, 28, bold=True).pack(fill="x")
        if subtitle:
            label(header, subtitle, 13, P["muted"], wraplength=width-80,
                  justify="left").pack(fill="x", pady=(8, 0))
        self.scroll = ScrollArea(self, bg=P["bg"])
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=(32, 17))
        self.body = self.scroll.body
        self.footer = tk.Frame(self, bg=P["bg"])
        self.footer.grid(row=2, column=0, sticky="ew", padx=32, pady=(12, 26))
        self.error = label(self.footer, "", 12, P["red"], wraplength=width-80,
                           justify="left")
        self.error.pack(fill="x", pady=(0, 12))
        self.buttons = tk.Frame(self.footer, bg=P["bg"])
        self.buttons.pack(fill="x")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.after(20, self._show)
        app.modals.add(self)

    def fit(self):
        """Size the dialog to its content so short forms never need to scroll."""
        try:
            self.update_idletasks()
            needed = (self.grid_slaves(row=0)[0].winfo_reqheight() + self.body.winfo_reqheight()
                      + self.footer.winfo_reqheight() + 29 + 18 + 12 + 26 + 6)
            height = max(470, min(needed, self.winfo_screenheight() - 90))
            width = self._width
            x = self.app.winfo_rootx() + max(0, (self.app.winfo_width() - width) // 2)
            y = self.app.winfo_rooty() + max(0, (self.app.winfo_height() - height) // 2)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except (tk.TclError, IndexError, ValueError):
            pass

    def _show(self):
        if self.winfo_exists():
            self.deiconify()
            self.lift()
            self.grab_set()

    def close(self):
        if getattr(self, "saving", False):
            self.error.configure(text="Saving is still in progress. Close this window after the result is confirmed.")
            return
        if getattr(self, "pending", None):
            if not messagebox.askyesno(
                    "Unconfirmed save",
                    "The last save was not confirmed. Refresh the history before entering it again. Close this form?",
                    parent=self):
                return
        self.app.modals.discard(self)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if not self.app.modals:
            self.app.hide_scrim()


class DisplayRatesDialog(Modal):
    """Separate display preferences. Transfers never ask the user for a rate."""
    def __init__(self, app):
        self.fields: Dict[str, Field] = {}
        self.pending: Optional[Command] = None
        self.saving = False
        self.expected = display_rate_revision(app.snapshot.settings)
        super().__init__(app, "Dinar display rates",
                         "For your combined DZD estimate only. Your recorded balances and transfer amounts stay unchanged.",
                         height=615)
        rates = display_rates(app.snapshot.settings)
        for currency, key in DISPLAY_RATE_KEYS.items():
            value = compact_rate(rates[currency]) if rates[currency] is not None else ""
            field_ = Field(self.body, f"1 {currency} = how many DZD?", value,
                           hint="Use your preferred valuation rate, not an amount to transfer.")
            field_.pack(fill="x", pady=(0, 18))
            self.fields[key] = field_
            field_.var.trace_add("write", self.update_preview)
        box = card(self.body)
        box.pack(fill="x", pady=(2, 16))
        label(box, "TOTAL IN DINARS  /  PREVIEW", 10, P["dim"], True).pack(fill="x", padx=18, pady=(16, 8))
        self.preview = label(box, "", 25, P["accent"], True, wraplength=535, justify="left")
        self.preview.pack(fill="x", padx=18, pady=(0, 16))
        label(self.body, "Saved for this database and reused on its other devices. Existing rates from the original app are kept. No live exchange-rate service is used.",
              12, P["muted"], wraplength=550, justify="left").pack(fill="x", pady=(0, 12))
        self.cancel_button = Button(self.buttons, "Cancel", self.close, variant="secondary")
        self.cancel_button.pack(side="left")
        self.save_button = Button(self.buttons, "Save display rates", self.save, width=180)
        self.save_button.pack(side="right")
        self.update_preview()
        self.bind("<Control-Return>", lambda e: self.save())
        if sys.platform == "darwin":
            self.bind("<Command-Return>", lambda e: self.save())
        self.fit()
        self.after(80, lambda: self.fields["display_rate"].input.focus_set())

    def update_preview(self, *args):
        if not hasattr(self, "preview"):
            return
        try:
            settings = display_rate_payload({key: f.get() for key, f in self.fields.items()})
            total = dzd_equivalent(self.app.snapshot.currency_totals(), settings) if self.app.snapshot.valid else None
            self.preview.configure(text=("\u2248 " + fmt(total, "DZD")) if total is not None else "Review ledger data checks.")
        except ValidationError:
            self.preview.configure(text="Enter both display rates.")

    def freeze(self, frozen):
        for field_ in self.fields.values():
            field_.enable(not frozen)

    def save(self):
        if self.saving:
            return
        if self.pending is None:
            try:
                if not self.app.can_write():
                    raise ValidationError("Reconnect or refresh successfully before changing display rates.")
                values = display_rate_payload({key: f.get() for key, f in self.fields.items()})
                self.pending = Command("display_rates", values, expected=self.expected)
            except ValidationError as exc:
                self.error.configure(text=str(exc))
                return
        self.saving = True
        self.freeze(True)
        self.save_button.set_enabled(False)
        self.save_button.set_text("Saving...")
        self.cancel_button.set_enabled(False)
        self.error.configure(text="")
        self.app.commit(self.pending, self._saved)

    def _saved(self, snapshot, error):
        self.saving = False
        if error is None:
            self.pending = None
            self.close()
            return
        self.cancel_button.set_enabled(True)
        self.save_button.set_enabled(True)
        if isinstance(error, ValidationError):
            self.pending = None
            self.freeze(False)
            self.save_button.set_text("Save display rates")
        else:
            self.save_button.set_text("Retry safely")
        suffix = "\nThe form is locked so a retry uses the same operation." if self.pending else ""
        self.error.configure(text=friendly_error(error) + suffix)


class TransactionDialog(Modal):
    def __init__(self, app, kind, record=None, loan=None, account=None):
        self.kind, self.record, self.loan = kind, record, loan
        self.fields: Dict[str, Field] = {}
        self.pending: Optional[Command] = None
        self.saving = False
        self._auto_received = ""
        self._preview_guard = False
        titles = {"income": "Record income", "expense": "Record an expense", "transfer": "Move money",
                  "savings_deposit": "Add to savings", "savings_withdraw": "Release savings",
                  "loan_out": "Record a loan", "loan_repaid": "Record a repayment", "opening": "Add an opening balance"}
        subtitles = {
            "income": "Enter the amount that actually arrived. No fees or pending balance.",
            "expense": "Choose the account you paid from and record the actual amount.",
            "transfer": "Enter what you sent and what you received. The rate is calculated for you.",
            "savings_deposit": "Reserve money within its bank or cash account. It stops being available to spend.",
            "savings_withdraw": "Return reserved money to the same account's available balance.",
            "loan_out": "Track money lent to someone without counting it as an expense.",
            "loan_repaid": "Record a partial or full repayment in the loan's original currency.",
            "opening": "Record money you already had before tracking. It is not counted as income."}
        super().__init__(app, ("Edit " + KIND_NAMES[kind].lower()) if record else titles[kind], subtitles[kind], height=750 if kind == "transfer" else 710)
        chosen = account or (record.account if record else loan.account if loan else "usd_bank")
        if kind == "expense" and account is None and record is None:
            chosen = "dzd_cash"
        if kind == "transfer":
            self._build_transfer(record, chosen)
        else:
            if kind in ("income", "expense", "opening"):
                self.add_field("description", "Source / description" if kind == "income" else "Description",
                               record.description if record else "Opening balance" if kind == "opening" else "")
            if kind in ("loan_out", "loan_repaid"):
                self.add_field("borrower", "Who?", record.borrower if record else loan.borrower if loan else "")
                if kind == "loan_repaid":
                    self.fields["borrower"].input.configure(state="readonly")
            amount = str(record.amount) if record else str(app.snapshot.outstanding.get(loan.id, ZERO)) if loan else ""
            self.add_field("amount", "Amount", amount, large=True, placeholder="0.00")
            self.fields["amount"].var.trace_add("write", self.update_preview)
            names = [a.name for a in ACCOUNTS.values() if not loan or a.currency == loan.currency]
            left, right = self.columns() if kind == "expense" else (self.body, None)
            self.add_field("account", "Account", ACCOUNTS[chosen].name, choices=names, parent=left)
            self.fields["account"].input.bind("<<ComboboxSelected>>", self.update_preview)
            if kind == "expense":
                category = expense_category(record) if record else "Other"
                options = list(CATEGORIES)
                if category not in options:
                    options.append(category)
                self.add_field("category", "Category", category, choices=options, parent=right)
            hint_row = tk.Frame(self.body, bg=P["bg"])
            hint_row.pack(fill="x", pady=(0, 16))
            if kind in ("savings_deposit", "savings_withdraw", "loan_repaid"):
                Button(hint_row, "Use remaining" if loan else "Use all available", self.fill_max,
                       variant="secondary", small=True, height=32).pack(side="right", anchor="n", padx=(12, 0))
            self.balance_hint = label(hint_row, "", 12, P["muted"], wraplength=470, justify="left")
            self.balance_hint.pack(side="left", fill="x", expand=True)
        date_field = DateField(self.body, "Date", record.day if record else date.today().isoformat())
        date_field.pack(fill="x", pady=(0, 15))
        self.fields["date"] = date_field
        if kind == "transfer":
            left, right = self.columns()
            self.add_field("description", "Description (optional)", record.description if record else "", parent=left)
            self.add_field("notes", "Notes (optional)", record.notes if record else "", parent=right)
        else:
            self.add_field("notes", "Notes (optional)", record.notes if record else "")
        self.cancel_button = Button(self.buttons, "Cancel", self.close, variant="secondary")
        self.cancel_button.pack(side="left")
        self.save_button = Button(self.buttons, "Save changes" if record else "Save transaction", self.save, width=172)
        self.save_button.pack(side="right")
        self.update_preview()
        self.bind("<Control-Return>", lambda e: self.save())
        if sys.platform == "darwin":
            self.bind("<Command-Return>", lambda e: self.save())
        self.fit()
        focus_key = "amount_sent" if kind == "transfer" else "description" if "description" in self.fields else "amount"
        if kind == "loan_repaid":
            focus_key = "amount"
        self.after(80, self.fields[focus_key].input.focus_set)

    def columns(self):
        row = tk.Frame(self.body, bg=P["bg"])
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1, uniform="form")
        left, right = tk.Frame(row, bg=P["bg"]), tk.Frame(row, bg=P["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        return left, right

    def add_field(self, key, title, value="", parent=None, **kwargs):
        field_ = Field(parent or self.body, title, value, **kwargs)
        field_.pack(fill="x", pady=(0, 15))
        self.fields[key] = field_
        return field_

    def _build_transfer(self, record, chosen):
        panel = tk.Frame(self.body, bg=P["bg"])
        panel.pack(fill="x", pady=(0, 12))
        panel.grid_columnconfigure((0, 1), weight=1, uniform="transfer")
        source = chosen
        dest = record.destination if record else ("eur_cash" if chosen == "dzd_cash" else "dzd_cash")
        for col, prefix, name, account_id, amount in [
            (0, "from", "YOU SEND", source, str(record.amount) if record else ""),
            (1, "to", "YOU RECEIVE", dest, str(record.received) if record else "")]:
            box = card(panel)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 7) if col == 0 else (7, 0))
            inside = tk.Frame(box, bg=P["card"])
            inside.pack(fill="both", expand=True, padx=16, pady=14)
            label(inside, name, 10, P["accent"] if col == 0 else P["green"], True).pack(fill="x", pady=(0, 10))
            field_ = Field(inside, "From account" if col == 0 else "To account", ACCOUNTS[account_id].name,
                           choices=[a.name for a in ACCOUNTS.values()])
            field_.pack(fill="x", pady=(0, 12))
            self.fields[prefix + "_account"] = field_
            field_.input.bind("<<ComboboxSelected>>", self.on_accounts)
            key = "amount_sent" if col == 0 else "amount_received"
            self.fields[key] = Field(inside, "Amount (" + ACCOUNTS[account_id].currency + ")", amount, large=True,
                                     placeholder="0.00")
            self.fields[key].pack(fill="x")
            self.fields[key].var.trace_add("write", self.update_preview)
        controls = tk.Frame(self.body, bg=P["bg"])
        controls.pack(fill="x", pady=(0, 12))
        self.max_button = Button(controls, "Send available balance", self.fill_max, variant="secondary", small=True, height=32)
        self.max_button.pack(side="left")
        self.swap_button = Button(controls, "Swap direction", self.swap, variant="secondary", small=True, height=32)
        self.swap_button.pack(side="right")
        rate_card = card(self.body)
        rate_card.pack(fill="x", pady=(0, 15))
        label(rate_card, "EFFECTIVE RATE", 10, P["dim"], True).pack(fill="x", padx=16, pady=(12, 4))
        self.rate_label = label(rate_card, "Enter both amounts", 18, P["text"], True, wraplength=530, justify="left")
        self.rate_label.pack(fill="x", padx=16)
        self.balance_hint = label(rate_card, "", 12, P["muted"], wraplength=530, justify="left")
        self.balance_hint.pack(fill="x", padx=16, pady=(6, 12))
        self.on_accounts()

    def on_accounts(self, event=None):
        src = account_key(self.fields["from_account"].get())
        destinations = [a.name for key, a in ACCOUNTS.items() if key != src]
        self.fields["to_account"].options(destinations)
        if self.fields["to_account"].get() not in destinations:
            self.fields["to_account"].set(destinations[0])
        dst = account_key(self.fields["to_account"].get())
        self.fields["amount_sent"].title_label.configure(text=f"Amount ({ACCOUNTS[src].currency})")
        self.fields["amount_received"].title_label.configure(text=f"Amount ({ACCOUNTS[dst].currency})")
        self.update_preview()

    def swap(self):
        if self.pending or self.saving:
            return
        a, b = self.fields["from_account"].get(), self.fields["to_account"].get()
        sent, received = self.fields["amount_sent"].get(), self.fields["amount_received"].get()
        self._preview_guard = True
        self.fields["from_account"].set(b)
        self.fields["to_account"].set(a)
        self.fields["amount_sent"].set(received)
        self.fields["amount_received"].set(sent)
        self._auto_received = ""
        self._preview_guard = False
        self.on_accounts()

    def preview_balances(self):
        available, saved = dict(self.app.snapshot.available), dict(self.app.snapshot.saved)
        # An edit replaces the old effect; it is not an additional transaction.
        rec = self.record
        if rec and not rec.voided:
            if rec.kind in ("income", "opening", "adjustment", "loan_repaid"):
                available[rec.account] -= rec.amount
            elif rec.kind in ("expense", "loan_out"):
                available[rec.account] += rec.amount
            elif rec.kind == "transfer":
                available[rec.account] += rec.amount
                available[rec.destination] -= rec.received
            elif rec.kind == "savings_deposit":
                available[rec.account] += rec.amount
                saved[rec.account] -= rec.amount
            elif rec.kind == "savings_withdraw":
                available[rec.account] -= rec.amount
                saved[rec.account] += rec.amount
        return available, saved

    def fill_max(self):
        if self.pending or self.saving:
            return
        key = "from_account" if self.kind == "transfer" else "account"
        account = account_key(self.fields[key].get())
        if self.loan:
            amount = self.app.snapshot.outstanding.get(self.loan.id, ZERO)
        else:
            available, saved = self.preview_balances()
            bucket = saved if self.kind == "savings_withdraw" else available
            amount = max(ZERO, bucket[account])
        self.fields["amount_sent" if self.kind == "transfer" else "amount"].set(str(amount))

    def update_preview(self, *args):
        if self._preview_guard or not hasattr(self, "balance_hint"):
            return
        try:
            key = "from_account" if self.kind == "transfer" else "account"
            src = account_key(self.fields[key].get())
            available, saved = self.preview_balances()
            current = (saved if self.kind == "savings_withdraw" else available)[src]
            current_equivalent = dinar_equivalent_text(
                current, ACCOUNTS[src].currency, self.app.snapshot.settings)
            current_line = (f"{ACCOUNTS[src].name}: {fmt(current, ACCOUNTS[src].currency)} "
                            f"{'saved' if self.kind == 'savings_withdraw' else 'available'}"
                            f"{' before this record' if self.record else ''}.")
            if current_equivalent:
                current_line += "  " + current_equivalent
            if self.kind != "transfer" and "amount" in self.fields:
                try:
                    entered = money(self.fields["amount"].get())
                    entered_equivalent = dinar_equivalent_text(
                        entered, ACCOUNTS[src].currency, self.app.snapshot.settings)
                    if entered_equivalent:
                        current_line += "\nEntered amount: " + entered_equivalent
                except ValidationError:
                    pass
            self.balance_hint.configure(text=current_line, fg=P["muted"])
            if self.kind == "transfer":
                dst = account_key(self.fields["to_account"].get())
                sent_text = self.fields["amount_sent"].get()
                received_text = self.fields["amount_received"].get()
                if ACCOUNTS[src].currency == ACCOUNTS[dst].currency and (not received_text or received_text == self._auto_received):
                    self._preview_guard = True
                    self.fields["amount_received"].set(sent_text)
                    self._auto_received = sent_text
                    self._preview_guard = False
                try:
                    sent = money(sent_text)
                    received = money(self.fields["amount_received"].get())
                    self.rate_label.configure(text=rate_text(src, dst, sent, received))
                    source_after = current - sent
                    destination_after = available[dst] + received
                    source_after_equivalent = dinar_equivalent_text(
                        source_after, ACCOUNTS[src].currency,
                        self.app.snapshot.settings)
                    destination_after_equivalent = dinar_equivalent_text(
                        destination_after, ACCOUNTS[dst].currency,
                        self.app.snapshot.settings)
                    source_line = f"{ACCOUNTS[src].name} {fmt(source_after, ACCOUNTS[src].currency)}"
                    destination_line = f"{ACCOUNTS[dst].name} {fmt(destination_after, ACCOUNTS[dst].currency)}"
                    if source_after_equivalent:
                        source_line += "  " + source_after_equivalent
                    if destination_after_equivalent:
                        destination_line += "  " + destination_after_equivalent
                    sent_equivalent = dinar_equivalent_text(
                        sent, ACCOUNTS[src].currency, self.app.snapshot.settings)
                    received_equivalent = dinar_equivalent_text(
                        received, ACCOUNTS[dst].currency, self.app.snapshot.settings)
                    value_parts = []
                    if sent_equivalent:
                        value_parts.append("Sent " + sent_equivalent)
                    if received_equivalent:
                        value_parts.append("Received " + received_equivalent)
                    value_line = ("\nDisplay value: " + "  •  ".join(value_parts)) if value_parts else ""
                    self.balance_hint.configure(
                        text=(f"After {'changes' if self.record else 'transfer'}: "
                              f"{source_line}\n{destination_line}{value_line}"),
                        fg=P["red"] if sent > current else P["muted"])
                except ValidationError:
                    self.rate_label.configure(text="Enter both amounts")
            elif self.loan:
                outstanding = self.app.snapshot.outstanding.get(self.loan.id, ZERO)
                outstanding_equivalent = dinar_equivalent_text(
                    outstanding, self.loan.currency, self.app.snapshot.settings)
                text = (f"Still owed by {self.loan.borrower}: "
                        f"{fmt(outstanding, self.loan.currency)}")
                if outstanding_equivalent:
                    text += "  " + outstanding_equivalent
                text += f". Repayment goes to {ACCOUNTS[src].name}."
                self.balance_hint.configure(text=text)
        except (ValidationError, KeyError):
            pass
        finally:
            self._preview_guard = False

    def freeze(self, frozen):
        for field_ in self.fields.values():
            field_.enable(not frozen)
        if self.kind == "loan_repaid" and not frozen:
            self.fields["borrower"].input.configure(state="readonly")
        if hasattr(self, "max_button"):
            self.max_button.set_enabled(not frozen)
            self.swap_button.set_enabled(not frozen)

    def save(self):
        if self.saving:
            return
        if self.pending is None:
            try:
                if not self.app.can_write():
                    raise ValidationError("Reconnect or refresh successfully before saving a new transaction.")
                values = {key: field_.get() for key, field_ in self.fields.items()}
                if self.loan:
                    values["loan_id"] = self.loan.id
                    values["borrower"] = self.loan.borrower
                payload = record_payload(self.kind, values, rid=self.record.id if self.record else None)
                self.pending = Command("replace" if self.record else "add", payload,
                                       expected=fingerprint(self.record.raw) if self.record else "")
            except ValidationError as exc:
                self.error.configure(text=str(exc))
                return
        self.saving = True
        self.freeze(True)
        self.save_button.set_enabled(False)
        self.save_button.set_text("Saving...")
        self.cancel_button.set_enabled(False)
        self.error.configure(text="")
        self.app.commit(self.pending, self._saved)

    def _saved(self, snapshot, error):
        self.saving = False
        if error is None:
            self.pending = None
            self.close()
            return
        self.cancel_button.set_enabled(True)
        self.save_button.set_enabled(True)
        if isinstance(error, ValidationError):
            self.pending = None
            self.freeze(False)
            self.save_button.set_text("Save changes" if self.record else "Save transaction")
        else:
            self.save_button.set_text("Retry safely")
        suffix = "\nYour form is locked so a retry uses exactly the same operation." if self.pending else ""
        self.error.configure(text=friendly_error(error) + suffix)


class DetailDialog(Modal):
    def __init__(self, app, record):
        self.record = record
        self.saving = False
        self.pending = None
        super().__init__(app, "Transaction details", "The original record is retained when you edit or void it.", height=680)
        summary = card(self.body)
        summary.pack(fill="x", pady=(0, 18))
        label(summary, KIND_NAMES[record.kind].upper() + ("  /  VOIDED" if record.voided else ""), 10, P["dim"], True).pack(fill="x", padx=20, pady=(18, 8))
        equivalent = record_dinar_equivalent(record, app.snapshot.settings)
        label(summary, record.amount_text, 22, activity_color(record), True,
              wraplength=550, justify="left").pack(
                  fill="x", padx=20, pady=(0, 4 if equivalent else 18))
        if equivalent:
            label(summary, equivalent, 12, P["muted"],
                  wraplength=550, justify="left").pack(
                      fill="x", padx=20, pady=(0, 18))
        fields = [("Description", record.title), ("Date", record.day), ("Account", record.route)]
        if record.kind == "transfer":
            fields.append(("Calculated rate", rate_text(record.account, record.destination, record.amount, record.received)))
        if record.kind == "loan_out":
            remaining = app.snapshot.outstanding.get(record.id, ZERO)
            fields.append(("Remaining", fmt(remaining, record.currency)))
            remaining_equivalent = dinar_equivalent_text(
                remaining, record.currency, app.snapshot.settings)
            if remaining_equivalent:
                fields.append(("Remaining in dinars", remaining_equivalent))
        if record.kind == "expense":
            fields.append(("Category", record.category or "Other"))
        if record.notes:
            fields.append(("Notes", record.notes))
        fields.append(("Record ID", record.id))
        fields.append(("Revisions retained", str(len(record.raw.get("_history", [])))))
        for heading, value in fields:
            label(self.body, heading.upper(), 10, P["dim"], True).pack(fill="x", pady=(0, 5))
            label(self.body, value, 14, P["muted"] if heading == "Record ID" else P["text"],
                  wraplength=565, justify="left").pack(fill="x", pady=(0, 15))
        if record.legacy:
            label(self.body, "Imported from your original tracker. Historical net amounts and settlement differences are preserved.", 12, P["muted"], wraplength=560, justify="left").pack(fill="x", pady=(0, 16))
        self.close_button = Button(self.buttons, "Close", self.close, variant="secondary")
        self.close_button.pack(side="left")
        self.void_button = Button(self.buttons, "Restore" if record.voided else "Void record", self.change_state,
                                  variant="secondary" if record.voided else "danger")
        self.void_button.pack(side="right")
        self.edit_button = None
        if not record.voided and record.kind not in ("loan_repaid", "adjustment"):
            self.edit_button = Button(self.buttons, "Edit", self.edit, variant="secondary")
            self.edit_button.pack(side="right", padx=(0, 8))
        if record.kind == "loan_out" and not record.voided and app.snapshot.outstanding.get(record.id, ZERO) > ZERO:
            Button(self.body, "Record repayment", self.repay, width=190).pack(anchor="w", pady=(0, 16))
        self.void_button.set_enabled(app.can_write())
        if self.edit_button:
            self.edit_button.set_enabled(app.can_write())
        self.fit()

    def edit(self):
        if self.saving:
            return
        record = self.record
        self.close()
        self.app.open_form(record.kind, record=record)

    def repay(self):
        if self.saving or self.pending:
            return
        record = self.record
        self.close()
        self.app.open_form("loan_repaid", loan=record)

    def change_state(self):
        if self.saving:
            return
        if self.pending is None:
            action = "restore" if self.record.voided else "void"
            verb = "Restore" if self.record.voided else "Void"
            if not messagebox.askyesno(verb + " transaction", f"{verb} this transaction? Balances will be recalculated. The record stays in your history and can be restored. A change that creates an unfunded balance will be rejected.", parent=self):
                return
            self.pending = Command(action, {"id": self.record.id}, expected=fingerprint(self.record.raw))
        self.saving = True
        self.void_button.set_enabled(False)
        self.close_button.set_enabled(False)
        if self.edit_button:
            self.edit_button.set_enabled(False)
        self.app.commit(self.pending, self._saved)

    def _saved(self, snapshot, error):
        self.saving = False
        if error is None:
            self.pending = None
            self.close()
        else:
            if isinstance(error, ValidationError):
                self.pending = None
            self.error.configure(text=friendly_error(error))
            self.void_button.set_text("Retry safely" if self.pending else "Restore" if self.record.voided else "Void record")
            self.void_button.set_enabled(True)
            self.close_button.set_enabled(True)
            if self.edit_button:
                self.edit_button.set_enabled(self.pending is None)


QUICK_ADD = [("expense", "Expense"), ("income", "Income"), ("transfer", "Transfer"), (None, None),
             ("savings_deposit", "Add to savings"), ("savings_withdraw", "Release savings"), (None, None),
             ("loan_out", "Loan"), ("opening", "Opening balance")]
QUICK_KEYS = {"e": "expense", "i": "income", "t": "transfer"}


class FinanceApp(tk.Tk):
    PAGES = [("overview", "Overview"), ("income", "Income"), ("expenses", "Expenses"),
             ("transfers", "Transfers"), ("savings", "Savings"), ("lending", "Lending"),
             ("activity", "All activity"), ("settings", "Settings")]

    def __init__(self, demo=False):
        super().__init__()
        global FONT_TEXT_FAMILY, FONT_DISPLAY_FAMILY
        try:
            families = tuple(tkfont.families(self))
        except tk.TclError:
            families = ()
        lookup = {name.casefold(): name for name in families}

        def installed_family(*candidates):
            for candidate in candidates:
                exact = lookup.get(candidate.casefold())
                if exact:
                    return exact
            # Some Windows installs expose a localized or slightly different
            # family name. Prefer any installed SF Pro family before falling back.
            for candidate in candidates:
                token = candidate.casefold().replace(" ", "")
                for actual in families:
                    normalized = actual.casefold().replace(" ", "")
                    if token in normalized or normalized in token:
                        return actual
            return None

        sf_text = installed_family("SF Pro Text", "SF Pro")
        sf_display = installed_family("SF Pro Display", "SF Pro")
        if IS_MAC or sys.platform == "win32":
            # Use SF Pro on both platforms. Literal family names are retained as
            # the requested family even when a second computer lacks the font;
            # Tk will substitute rather than preventing the app from opening.
            FONT_TEXT_FAMILY = sf_text or sf_display or "SF Pro Text"
            FONT_DISPLAY_FAMILY = sf_display or sf_text or "SF Pro Display"
        else:
            FONT_TEXT_FAMILY = (sf_text or installed_family("Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", "Arial")
                                or "TkDefaultFont")
            FONT_DISPLAY_FAMILY = (sf_display or installed_family("Segoe UI Variable Display", "Segoe UI", "DejaVu Sans", "Arial")
                                   or FONT_TEXT_FAMILY)
        self.title("Finance" + (" — Demo" if demo else ""))
        self.configure(bg=P["bg"])
        width = min(1480, max(1020, self.winfo_screenwidth() - 60))
        height = min(950, max(680, self.winfo_screenheight() - 85))
        self.geometry(f"{width}x{height}+30+30")
        self.minsize(min(1020, width), min(680, height))
        self.demo = demo
        self.worker = Worker()
        self.repo = DemoRepository() if demo else None
        self.snapshot = project([])
        self.has_snapshot = False
        self.online = False
        self.fetching = False
        self.busy_write = False
        self.closed = False
        self.storage_method = "demo" if demo else "session"
        self.storage_note = ""
        self.last_refresh = 0.0
        self.modals = set()
        self.current_page = "overview"
        self.selected_month = date.today().strftime("%Y-%m")
        self.nav = {}
        self.filter_states = {}
        self._search_job = None
        self._table_context = None
        self._style()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", self._wheel, add="+")
        self.bind_all("<Button-5>", self._wheel, add="+")
        self.scrim: Optional[Scrim] = None
        self._rendered_page = None
        self.page_scroll = None
        modifiers = ("Control", "Command") if IS_MAC else ("Control",)
        for modifier in modifiers:
            self.bind(f"<{modifier}-r>", lambda e: self.refresh())
            self.bind(f"<{modifier}-n>", lambda e: self.show_quick_add())
            for key, kind in QUICK_KEYS.items():
                self.bind(f"<{modifier}-{key}>", lambda e, k=kind: (self.open_form(k), "break")[1])
        self.bind("<FocusIn>", self._on_focus, add="+")
        for sequence, delta in (("<Alt-Left>", -1), ("<Alt-Right>", 1)) + (
                (("<Option-Left>", -1), ("<Option-Right>", 1)) if IS_MAC else ()):
            try:
                self.bind(sequence, lambda e, d=delta: self.review_step(d))
            except tk.TclError:
                pass
        for modifier in modifiers:
            self.bind(f"<{modifier}-f>", lambda e: self._focus_review_search())
        self._review = None
        self._review_tree = None
        self._review_job = None
        self.bind("<Configure>", self._follow_scrim, add="+")
        self.bind("<Unmap>", lambda e: e.widget is self and self.scrim and self.scrim.withdraw(), add="+")
        self.bind("<Map>", lambda e: e.widget is self and self.modals and self.show_scrim(), add="+")
        self.after(60, self._drain)
        self.show_connecting()
        self.after_idle(self._ensure_visible)
        self.after(80, self.startup)

    def _focus_review_search(self):
        if self.current_page == "activity" and not self.modals:
            search = getattr(self, "_review_search", None)
            if search is not None and search.winfo_exists():
                search.focus_set()
                search.select_range(0, "end")
        return "break"

    def show_scrim(self):
        if os.environ.get("FINANCE_NO_DIM") == "1":
            return
        try:
            if self.scrim is None or not self.scrim.winfo_exists():
                self.scrim = Scrim(self)
            self.scrim.cover()
            self.scrim.deiconify()
            self.scrim.lift()
            for dialog in self.modals:
                dialog.lift()
        except tk.TclError as exc:
            write_diagnostic("The dialog backdrop could not be shown.", exc)
            self.scrim = None

    def hide_scrim(self):
        if self.scrim is not None:
            try:
                self.scrim.withdraw()
            except tk.TclError:
                self.scrim = None

    def _follow_scrim(self, event):
        if event.widget is self and self.modals and self.scrim is not None:
            try:
                self.scrim.cover()
                self.lift_modal()
            except tk.TclError:
                pass

    def lift_modal(self):
        for dialog in list(self.modals):
            try:
                dialog.lift()
                dialog.focus_force()
            except tk.TclError:
                pass

    def show_quick_add(self, anchor=None):
        if self.modals:
            self.lift_modal()
            return
        if not self.can_write():
            self.toast("Changes are disabled until your database is connected and data checks pass.", error=True)
            return
        menu = tk.Menu(self, tearoff=0, bg=P["card"], fg=P["text"], activebackground=P["selection"],
                       activeforeground=P["white"], bd=0, font=font(13))
        key_name = "⌘" if IS_MAC else "Ctrl+"
        shortcut = {kind: key.upper() for key, kind in QUICK_KEYS.items()}
        for kind, name in QUICK_ADD:
            if kind is None:
                menu.add_separator()
                continue
            menu.add_command(label=name, command=lambda k=kind: self.open_form(k),
                             accelerator=(key_name + shortcut[kind]) if kind in shortcut else "")
        widget = anchor or getattr(self, "new_button", None)
        try:
            if widget is not None and widget.winfo_exists():
                x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 4
            else:
                x, y = self.winfo_pointerx(), self.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _ensure_visible(self):
        try:
            self.deiconify()
            self.lift()
            self.update_idletasks()
        except tk.TclError:
            pass

    def _style(self):
        style = ttk.Style(self)
        try:
            available = set(style.theme_names())
            style.theme_use("clam" if "clam" in available else "aqua" if IS_MAC and "aqua" in available else "default")
        except tk.TclError as exc:
            write_diagnostic("Tk theme selection failed; using the active theme.", exc)
        style.configure("Finance.Treeview", background=P["card"], foreground=P["text"],
                        fieldbackground=P["card"], borderwidth=0, relief="flat",
                        rowheight=58, font=font(13))
        style.configure("Finance.Treeview.Heading", background=P["surface"],
                        foreground=P["dim"], font=font(10, True), relief="flat",
                        borderwidth=0, padding=(10, 15))
        style.map("Finance.Treeview",
                  background=[("selected", P["selection"])],
                  foreground=[("selected", P["white"])])
        style.layout("Finance.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Ledger.Treeview", background=P["card"], foreground=P["text"],
                        fieldbackground=P["card"], borderwidth=0, relief="flat",
                        rowheight=30, font=font(12))
        style.configure("Ledger.Treeview.Heading", background=P["surface"], foreground=P["muted"],
                        font=font(11, True), relief="flat", borderwidth=0, padding=(8, 8))
        style.map("Ledger.Treeview.Heading", background=[("active", P["hover"])],
                  foreground=[("active", P["text"])])
        style.map("Ledger.Treeview", background=[("selected", P["selection"])],
                  foreground=[("selected", P["white"])])
        style.layout("Ledger.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Finance.TCombobox", fieldbackground=P["field"], background=P["field"],
                        foreground=P["text"], arrowcolor=P["muted"], bordercolor=P["border"],
                        lightcolor=P["field"], darkcolor=P["field"], padding=(12, 7),
                        insertcolor=P["text"], relief="flat")
        style.map("Finance.TCombobox",
                  fieldbackground=[("readonly", P["field"]), ("disabled", P["field"])],
                  foreground=[("readonly", P["text"]), ("disabled", P["dim"])],
                  selectbackground=[("readonly", P["field"])],
                  selectforeground=[("readonly", P["text"])],
                  background=[("active", P["hover"]), ("readonly", P["field"])],
                  bordercolor=[("focus", P["accent"]), ("!focus", P["border"])])
        style.configure("Finance.Vertical.TScrollbar", troughcolor=P["bg"],
                        background=P["border"], darkcolor=P["border"],
                        lightcolor=P["border"], bordercolor=P["bg"],
                        arrowcolor=P["dim"], gripcount=0, width=9)
        style.map("Finance.Vertical.TScrollbar",
                  background=[("active", P["muted"]), ("!active", P["border"])],
                  troughcolor=[("!disabled", P["bg"])])
        self.option_add("*TCombobox*Listbox.background", P["field"])
        self.option_add("*TCombobox*Listbox.foreground", P["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", P["selection"])
        self.option_add("*TCombobox*Listbox.selectForeground", P["white"])
        self.option_add("*TCombobox*Listbox.font", font(14))

    def _wheel(self, event):
        widget = event.widget
        if not isinstance(widget, tk.Misc) or isinstance(widget, (ttk.Treeview, ttk.Combobox, tk.Text)):
            return
        while widget is not None:
            if isinstance(widget, ScrollArea):
                widget.wheel(event)
                return
            widget = getattr(widget, "master", None)

    def _drain(self):
        if self.closed:
            return
        while True:
            try:
                callback, result, error = self.worker.results.get_nowait()
            except queue.Empty:
                break
            try:
                callback(result, error)
            except Exception as exc:
                write_diagnostic("A background result could not be displayed.", exc)
                messagebox.showerror("Interface error", "The interface could not display the result. Refresh before repeating any financial operation.\n\nA diagnostic was saved to:\n" + str(LOG_FILE), parent=self)
        self.after(60, self._drain)

    def _on_focus(self, event):
        if event.widget is self and self.repo and self.has_snapshot and not self.modals and time.monotonic() - self.last_refresh > 60:
            self.refresh(silent=True)

    def clear_root(self):
        for child in self.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()

    def show_connecting(self):
        self.clear_root()
        area = tk.Frame(self, bg=P["bg"])
        area.place(relx=.5, rely=.46, anchor="center")
        mark = tk.Canvas(area, width=64, height=64, bg=P["bg"], highlightthickness=0)
        mark.pack()
        rounded_rect(mark, 2, 2, 62, 62, 17, fill=P["accent"])
        mark.create_line(20, 25, 44, 25, fill=P["white"], width=4, capstyle="round")
        mark.create_line(20, 39, 38, 39, fill=P["white"], width=4, capstyle="round")
        label(area, "Finance", 34, bold=True).pack(pady=(20, 7))
        label(area, "Opening your private workspace…", 14, P["muted"]).pack()

    def startup(self):
        if self.demo:
            self.worker.submit(self.repo.initialize, self._connected)
            return
        def load():
            return CredentialStore().load()
        def loaded(result, error):
            if error:
                self.show_setup("Your saved connection could not be opened. Enter your database URL again.")
            elif result[0]:
                self.storage_method = result[1]
                self.connect(result[0], remember=result[1] != "environment")
            else:
                self.show_setup()
        self.worker.submit(load, loaded)

    def show_setup(self, error_text=""):
        self.clear_root()
        outer = tk.Frame(self, bg=P["bg"])
        current_width = max(self.winfo_width(), self.winfo_reqwidth(), 680)
        outer_width = min(570, max(390, current_width - 70))
        outer.place(relx=.5, rely=.5, anchor="center", width=outer_width)

        brand = tk.Frame(outer, bg=P["bg"])
        brand.pack(fill="x", pady=(0, 25))
        mark = tk.Canvas(brand, width=48, height=48, bg=P["bg"], highlightthickness=0)
        mark.pack(side="left", padx=(0, 14))
        rounded_rect(mark, 1, 1, 47, 47, 13, fill=P["accent"])
        mark.create_line(14, 19, 34, 19, fill=P["white"], width=3, capstyle="round")
        mark.create_line(14, 30, 29, 30, fill=P["white"], width=3, capstyle="round")
        brand_text = tk.Frame(brand, bg=P["bg"])
        brand_text.pack(side="left", fill="x", expand=True)
        label(brand_text, "Finance", 26, bold=True).pack(fill="x")
        label(brand_text, "Personal ledger", 12, P["muted"]).pack(fill="x", pady=(2, 0))

        label(outer, "Connect your workspace", 34, bold=True).pack(fill="x")
        label(outer, "Use your own PostgreSQL connection. Existing records stay exactly where they are.",
              14, P["muted"], wraplength=540, justify="left").pack(fill="x", pady=(10, 26))

        box = card(outer, tone=P["card"])
        box.pack(fill="x")
        inside = tk.Frame(box, bg=P["card"])
        inside.pack(fill="x", padx=25, pady=25)
        self.url_field = Field(
            inside, "PostgreSQL connection URL", secret=True,
            hint="Each computer can keep its own saved database connection.")
        self.url_field.pack(fill="x")
        self.remember_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            inside, text="Remember securely on this computer",
            variable=self.remember_var, font=font(12), bg=P["card"], fg=P["muted"],
            activebackground=P["card"], activeforeground=P["text"],
            selectcolor=P["field"], relief="flat", bd=0,
            highlightthickness=0).pack(anchor="w", pady=(16, 2))
        self.setup_error = label(inside, error_text, 12, P["red"],
                                 wraplength=500, justify="left")
        self.setup_error.pack(fill="x", pady=(10, 14))
        self.connect_button = Button(
            inside, "Connect", lambda: self.connect(
                self.url_field.get(), self.remember_var.get()), height=44)
        self.connect_button.pack(fill="x")
        label(outer, "Encrypted connection • local credentials • no shared app account",
              11, P["dim"], wraplength=540, justify="left").pack(fill="x", pady=(16, 0))
        self.url_field.input.bind(
            "<Return>", lambda e: self.connect(
                self.url_field.get(), self.remember_var.get()))
        self.url_field.input.focus_set()

    def connect(self, url, remember=True):
        if self.fetching:
            return
        if not url:
            if hasattr(self, "setup_error") and self.setup_error.winfo_exists():
                self.setup_error.configure(text="Enter your database connection URL.")
            return
        self.fetching = True
        self.repo = None
        if hasattr(self, "connect_button") and self.connect_button.winfo_exists():
            self.connect_button.set_enabled(False)
            self.setup_error.configure(text="Connecting and creating a local safety backup...", fg=P["muted"])
        def work():
            repo = Repository(url)
            self.repo = repo  # Repository never touches any Tk objects.
            snapshot = repo.initialize()
            method, note = ("environment" if self.storage_method == "environment" else "session"), ""
            if remember and method != "environment":
                try:
                    method = CredentialStore().save(url)
                except Exception:
                    note = "Secure credential storage was not available. The existing config was left unchanged; a new URL is session-only."
            return snapshot, method, note
        def done(result, error):
            self.fetching = False
            if error:
                cache = self.repo.read_cache() if isinstance(self.repo, Repository) else None
                if cache:
                    self.snapshot, self.has_snapshot, self.online = cache, True, False
                    self.build_shell()
                    self.toast("Offline: showing the last successful local snapshot. Reconnect to enable changes.", error=True)
                else:
                    self.show_setup(friendly_error(error))
            else:
                snapshot, self.storage_method, self.storage_note = result
                self._connected(snapshot, None)
        self.worker.submit(work, done)

    def _connected(self, snapshot, error):
        self.fetching = False
        if error:
            self.show_setup(friendly_error(error))
            return
        self.snapshot, self.has_snapshot, self.online = snapshot, True, True
        self.last_refresh = time.monotonic()
        self.build_shell()

    def build_shell(self):
        self.clear_root()
        self.sidebar = tk.Frame(self, bg=P["sidebar"], width=226)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        logo = tk.Frame(self.sidebar, bg=P["sidebar"])
        logo.pack(fill="x", padx=22, pady=(22, 19))
        mark = tk.Canvas(logo, width=34, height=34, bg=P["sidebar"], highlightthickness=0)
        mark.pack(side="left", padx=(0, 11))
        rounded_rect(mark, 1, 1, 33, 33, 9, fill=P["accent"])
        mark.create_line(10, 13, 24, 13, fill=P["white"], width=2.5, capstyle="round")
        mark.create_line(10, 21, 20, 21, fill=P["white"], width=2.5, capstyle="round")
        label(logo, "Finance", 21, bold=True).pack(side="left")
        new_host = tk.Frame(self.sidebar, bg=P["sidebar"])
        new_host.pack(fill="x", padx=16, pady=(0, 16))
        self.new_button = Button(new_host, "+  New transaction", lambda: self.show_quick_add(), height=40)
        self.new_button.pack(fill="x")
        label(self.sidebar, ("⌘N" if IS_MAC else "Ctrl+N") + " anywhere", 10, P["dim"],
              anchor="center").pack(fill="x", pady=(0, 14))

        self.nav = {}
        nav_host = tk.Frame(self.sidebar, bg=P["sidebar"])
        nav_host.pack(fill="x", padx=10)
        for page, name in self.PAGES:
            if page == "activity":
                tk.Frame(nav_host, bg=P["hairline"], height=1).pack(
                    fill="x", padx=14, pady=(11, 8))
            button = NavButton(nav_host, page, name,
                               lambda p=page: self.navigate(p))
            button.pack(fill="x", pady=1)
            self.nav[page] = button

        bottom = tk.Frame(self.sidebar, bg=P["sidebar"])
        bottom.pack(side="bottom", fill="x", padx=16, pady=16)
        status = card(bottom, tone=P["surface"], border=False)
        status.pack(fill="x", pady=(0, 10))
        status_inner = tk.Frame(status, bg=P["surface"])
        status_inner.pack(fill="x", padx=13, pady=12)
        top = tk.Frame(status_inner, bg=P["surface"])
        top.pack(fill="x")
        self.sync_dot = tk.Canvas(top, width=10, height=10, bg=P["surface"],
                                  highlightthickness=0)
        self.sync_dot.pack(side="left", padx=(0, 8))
        self.sync_dot_item = self.sync_dot.create_oval(2, 2, 8, 8,
                                                       fill=P["green"], outline="")
        self.sync_title = label(top, "", 12, P["green"], True, bg=P["surface"])
        self.sync_title.pack(side="left")
        self.sync_detail = label(status_inner, "", 10, P["dim"], bg=P["surface"],
                                 wraplength=166, justify="left")
        self.sync_detail.pack(fill="x", pady=(6, 0))
        Button(bottom, "Refresh", self.refresh, variant="secondary",
               width=194, height=36, small=True).pack(fill="x")
        label(bottom, "Version " + APP_VERSION + (" • Demo" if self.demo else ""),
              9, P["dim"]).pack(fill="x", pady=(12, 0))

        self.main = tk.Frame(self, bg=P["bg"])
        self.main.pack(side="left", fill="both", expand=True)
        self.toast_label = label(self.main, "", 12, P["green"],
                                 wraplength=980, justify="left")
        self.toast_label.pack(side="bottom", fill="x", padx=34, pady=(0, 9))
        self.content = tk.Frame(self.main, bg=P["bg"])
        self.content.pack(fill="both", expand=True)
        self.render()
        self.update_status()

    def nav_hover(self, page, hovered):
        button = self.nav.get(page)
        if button:
            button.set_hover(hovered)

    def navigate(self, page):
        if self.modals:
            next(iter(self.modals)).lift()
            return
        self.current_page = page
        self.render()
        if not self.demo and time.monotonic() - self.last_refresh > 30:
            self.refresh(silent=True)

    def update_status(self):
        if not hasattr(self, "sync_title") or not self.sync_title.winfo_exists():
            return
        if self.demo:
            title, color, detail = "Demo", P["amber"], "Sample data only"
        elif self.busy_write:
            title, color, detail = "Saving", P["amber"], "Waiting for confirmation"
        elif self.fetching:
            title, color, detail = "Refreshing", P["muted"], "Keeping the last snapshot"
        elif not self.online:
            title, color, detail = "Offline", P["red"], "Read-only cached data"
        elif not self.snapshot.valid:
            title, color, detail = "Review needed", P["amber"], "Open Data checks"
        else:
            title, color = "Connected", P["green"]
            try:
                stamp = datetime.fromisoformat(
                    self.snapshot.fetched_at).astimezone().strftime("%H:%M")
            except ValueError:
                stamp = "earlier"
            detail = "Synced at " + stamp
        self.sync_title.configure(text=title, fg=color)
        self.sync_detail.configure(text=detail)
        if hasattr(self, "sync_dot") and self.sync_dot.winfo_exists():
            self.sync_dot.itemconfigure(self.sync_dot_item, fill=color)

    def can_write(self):
        return bool(self.repo and self.online and self.has_snapshot and self.snapshot.valid and not self.busy_write)

    def refresh(self, silent=False):
        if not self.repo or self.fetching or self.busy_write or self.modals:
            return
        self.fetching = True
        self.update_status()
        # A cached startup must initialize before writes, including its baseline backup.
        function = self.repo.initialize if isinstance(self.repo, Repository) and self.repo.backup_path is None else self.repo.fetch
        def done(snapshot, error):
            self.fetching = False
            self.last_refresh = time.monotonic()
            if error:
                self.online = False
                self.update_status()
                if not silent:
                    self.toast(friendly_error(error), error=True)
            else:
                self.snapshot, self.online, self.has_snapshot = snapshot, True, True
                if not self.modals:
                    self.render()
                self.update_status()
                if not silent:
                    self.toast("Your records are up to date.")
        self.worker.submit(function, done)

    def commit(self, command, callback):
        if self.busy_write:
            callback(None, ValidationError("Another save is still running."))
            return
        self.busy_write = True
        self.update_status()
        def done(snapshot, error):
            self.busy_write = False
            if error is None:
                self.snapshot, self.online, self.has_snapshot = snapshot, True, True
                self.last_refresh = time.monotonic()
                self.render()
                self.toast("Display rates saved. Only the DZD estimate changed." if command.action == "display_rates"
                           else "Saved. Your balances have been updated.")
            elif not isinstance(error, ValidationError):
                self.online = False
            self.update_status()
            callback(snapshot, error)
        self.worker.submit(lambda: self.repo.write(command), done)

    def toast(self, text, error=False):
        if hasattr(self, "toast_label") and self.toast_label.winfo_exists():
            self.toast_label.configure(text=text, fg=P["red"] if error else P["green"])

    def render(self):
        if not hasattr(self, "content"):
            return
        if self._search_job:
            self.after_cancel(self._search_job)
            self._search_job = None
        if getattr(self, "_review_job", None):
            self.after_cancel(self._review_job)
            self._review_job = None
        self._table_context = None
        # Re-rendering the same page (after a save, refresh or filter change)
        # keeps the reader where they were instead of jumping to the top.
        keep = None
        if self._rendered_page == self.current_page and self.page_scroll is not None:
            try:
                keep = self.page_scroll.canvas.yview()[0]
            except tk.TclError:
                keep = None
        for widget in self.content.winfo_children():
            widget.destroy()
        self.page_scroll = None
        for page, button in self.nav.items():
            button.set_active(page == self.current_page)
        if self.current_page == "settings":
            self.render_settings()
        else:
            self.render_page()
        self._rendered_page = self.current_page
        if keep:
            scroll = self.page_scroll
            def restore():
                if scroll is not None and scroll.winfo_exists():
                    scroll._content()
                    scroll.canvas.yview_moveto(keep)
            self.after(40, restore)

    def page_header(self, title, subtitle, actions=()):
        header = tk.Frame(self.content, bg=P["bg"])
        header.pack(fill="x", padx=34, pady=(26, 18))
        right = tk.Frame(header, bg=P["bg"])
        right.pack(side="right", anchor="n", pady=(4, 0))
        for text, command, variant in actions:
            button = Button(right, text, command, variant=variant, height=40)
            button.pack(side="left", padx=(9, 0))
        left = tk.Frame(header, bg=P["bg"])
        left.pack(side="left", fill="x", expand=True)
        label(left, title, 30, bold=True).pack(fill="x")
        if subtitle:
            label(left, subtitle, 13, P["muted"], wraplength=650,
                  justify="left").pack(fill="x", pady=(7, 0))

    def page_body(self):
        scroll = ScrollArea(self.content)
        scroll.pack(fill="both", expand=True, padx=(34, 17))
        body = tk.Frame(scroll.body, bg=P["bg"])
        body.pack(fill="x", padx=(0, 17), pady=(0, 26))
        self.page_scroll = scroll
        return body

    def notice(self, parent, text, color=None):
        accent = color or P["amber"]
        box = card(parent, tone=P["surface"], border=False)
        box.pack(fill="x", pady=(0, 18))
        stripe = tk.Frame(box, bg=accent, width=3)
        stripe.pack(side="left", fill="y")
        message = label(box, text, 12, P["text"], bg=P["surface"],
                        wraplength=930, justify="left")
        message.pack(side="left", fill="x", expand=True, padx=16, pady=14)
        box.bind("<Configure>", lambda e: message.configure(
            wraplength=max(240, e.width-50)))

    def month_bar(self, parent, title="This month"):
        row = tk.Frame(parent, bg=P["bg"])
        row.pack(fill="x", pady=(7, 15))
        label(row, title, 19, bold=True).pack(side="left")
        chooser = card(row, tone=P["surface"], border=False)
        chooser.pack(side="right")
        Button(chooser, "‹", lambda: self.change_month(-1), variant="ghost",
               width=34, height=32, small=True).pack(side="left", padx=(3, 0), pady=3)
        current = datetime.strptime(self.selected_month, "%Y-%m")
        label(chooser, current.strftime("%B %Y"), 12, P["text"], True,
              bg=P["surface"], width=16, anchor="center").pack(side="left", padx=2)
        Button(chooser, "›", lambda: self.change_month(1), variant="ghost",
               width=34, height=32, small=True).pack(side="left", padx=(0, 3), pady=3)
        Button(row, "This month", self.today, variant="secondary", height=34,
               small=True).pack(side="right", padx=(0, 9))

    def today(self):
        self.selected_month = date.today().strftime("%Y-%m")
        self.render()

    def change_month(self, delta):
        year, month = map(int, self.selected_month.split("-"))
        total = year * 12 + month - 1 + delta
        y, m = divmod(total, 12)
        if y < 1900 or y > 9998:
            return
        self.selected_month = f"{y:04d}-{m+1:02d}"
        for state in self.filter_states.values():
            state["page"] = 0
        self.render()

    def open_display_rates(self):
        if self.modals:
            next(iter(self.modals)).lift()
            return
        if not self.can_write():
            self.toast("Connect and resolve any data checks before changing display rates.", error=True)
            return
        DisplayRatesDialog(self)

    def dinar_total_card(self, parent):
        box = card(parent, tone=P["hero"], border_color=P["hero_border"])
        box.pack(fill="x", pady=(0, 22))
        inner = tk.Frame(box, bg=P["hero"])
        inner.pack(fill="x", padx=24, pady=20)
        inner.grid_columnconfigure(0, weight=3, uniform="hero")
        inner.grid_columnconfigure(1, weight=2, uniform="hero")

        left = tk.Frame(inner, bg=P["hero"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 28))
        label(left, "Estimated total balance", 12, P["cyan"], True, bg=P["hero"]).pack(fill="x")
        total = total_in_dinars(self.snapshot)
        if not self.snapshot.valid:
            text = "Unavailable"
        elif total is None:
            text = "Set display rates"
        else:
            text = fmt(total, "DZD")
        self.dinar_total_label = fit_label(
            left, text, 38, P["red"] if total is not None and total < ZERO else P["text"],
            minimum=22, bg=P["hero"])
        self.dinar_total_label.pack(fill="x", pady=(6, 10))
        foot = tk.Frame(left, bg=P["hero"])
        foot.pack(fill="x")
        rates = display_rates(self.snapshot.settings)
        quotes = "  •  ".join(
            f"1 {currency} = {compact_rate(rates[currency])} DZD"
            if rates[currency] is not None else f"{currency} rate not set"
            for currency in DISPLAY_RATE_KEYS)
        Button(foot, "Edit rates", self.open_display_rates, variant="secondary",
               height=30, small=True).pack(side="left")
        self.dinar_rates_label = label(foot, quotes + "  •  loans not included", 11, P["dim"],
                                       bg=P["hero"])
        self.dinar_rates_label.pack(side="left", padx=(12, 0))

        right = tk.Frame(inner, bg=P["hero"])
        right.grid(row=0, column=1, sticky="nsew")
        totals = self.snapshot.currency_totals()
        for currency in CURRENCIES:
            row = tk.Frame(right, bg=P["hero"])
            row.pack(fill="x", pady=(0, 8))
            dot = tk.Canvas(row, width=10, height=10, bg=P["hero"], highlightthickness=0)
            dot.pack(side="left", padx=(0, 9))
            color = {"USD": ACCOUNTS["usd_bank"].accent, "EUR": ACCOUNTS["eur_bank"].accent,
                     "DZD": ACCOUNTS["dzd_cash"].accent}[currency]
            dot.create_oval(2, 2, 8, 8, fill=color, outline="")
            label(row, currency, 12, P["muted"], bg=P["hero"]).pack(side="left")
            value_host = tk.Frame(row, bg=P["hero"])
            value_host.pack(side="right")
            label(value_host, fmt(totals[currency], currency) if self.snapshot.valid else "—",
                  13, P["text"], True, bg=P["hero"], anchor="e").pack(fill="x")
            equivalent = (dinar_equivalent_text(totals[currency], currency, self.snapshot.settings)
                          if self.snapshot.valid else "")
            if equivalent:
                label(value_host, equivalent, 10, P["dim"], bg=P["hero"],
                      anchor="e").pack(fill="x", pady=(1, 0))

    def account_cards(self, parent, mode="available"):
        grid = tk.Frame(parent, bg=P["bg"])
        grid.pack(fill="x", pady=(0, 24))
        widgets = []
        symbols = {"usd_bank": "$", "eur_bank": "€", "eur_cash": "€", "dzd_cash": "DA"}
        for key, account in ACCOUNTS.items():
            box = card(grid, tone=P["card"])
            inside = tk.Frame(box, bg=P["card"])
            inside.pack(fill="both", expand=True, padx=18, pady=16)
            top = tk.Frame(inside, bg=P["card"])
            top.pack(fill="x")
            icon = tk.Canvas(top, width=34, height=34, bg=P["card"], highlightthickness=0)
            icon.pack(side="left", padx=(0, 11))
            rounded_rect(icon, 1, 1, 33, 33, 10, fill=P["surface"], outline=account.accent)
            icon.create_text(17, 17, text=symbols[key], fill=account.accent, font=font(12, True))
            names = tk.Frame(top, bg=P["card"])
            names.pack(side="left", fill="x", expand=True)
            label(names, account.name, 13, P["text"], True, bg=P["card"]).pack(fill="x")
            label(names, ("Saved" if mode == "saved" else "Available") + "  ·  " + account.location,
                  10, P["dim"], bg=P["card"]).pack(fill="x", pady=(2, 0))
            value = (self.snapshot.saved if mode == "saved" else self.snapshot.available)[key]
            text = fmt(value, account.currency) if self.snapshot.valid else "Unavailable"
            equivalent = (dinar_equivalent_text(value, account.currency, self.snapshot.settings)
                          if self.snapshot.valid else "")
            fit_label(inside, text, 26, P["red"] if value < ZERO else P["text"], minimum=15,
                      bg=P["card"]).pack(fill="x", pady=(16, 2))
            label(inside, equivalent or " ", 11, P["muted"], bg=P["card"]).pack(fill="x")
            tk.Frame(inside, bg=P["hairline"], height=1).pack(fill="x", pady=(12, 10))
            if mode == "saved":
                secondary_value = self.snapshot.available[key]
                bottom_text = f"Available  {fmt(secondary_value, account.currency)}"
            elif self.snapshot.saved[key]:
                bottom_text = f"Saved  {fmt(self.snapshot.saved[key], account.currency)}"
            else:
                held = self.snapshot.available[key] + self.snapshot.saved[key]
                bottom_text = f"Total held  {fmt(held, account.currency)}"
            label(inside, bottom_text if self.snapshot.valid else "Review data checks",
                  11, P["muted"], bg=P["card"], wraplength=240, justify="left").pack(fill="x")
            widgets.append(box)

        last_cols = [0]
        def arrange(event):
            columns = 4 if event.width >= 900 else 2
            if columns == last_cols[0]:
                return
            last_cols[0] = columns
            for c in range(4):
                grid.grid_columnconfigure(c, weight=1 if c < columns else 0,
                                          uniform="accounts" if c < columns else "")
            for i, box in enumerate(widgets):
                box.grid(row=i // columns, column=i % columns, sticky="nsew",
                         padx=(0 if i % columns == 0 else 6, 0 if i % columns == columns - 1 else 6),
                         pady=(0, 12 if columns == 2 and i < len(widgets) - 2 else 0))
        grid.bind("<Configure>", arrange)
        for i, box in enumerate(widgets):
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
            grid.grid_columnconfigure(i, weight=1, uniform="accounts")

    def triple_summary(self, parent, title, totals, color=None, dinars=True):
        accent = color or P["text"]
        box = card(parent, tone=P["card"])
        header = tk.Frame(box, bg=P["card"])
        header.pack(fill="x", padx=18, pady=(15, 4))
        dot = tk.Canvas(header, width=10, height=10, bg=P["card"], highlightthickness=0)
        dot.pack(side="left", padx=(0, 8))
        dot.create_oval(2, 2, 8, 8, fill=accent, outline="")
        label(header, title, 13, P["text"], True, bg=P["card"]).pack(side="left")
        combined = dzd_equivalent(totals, self.snapshot.settings) if self.snapshot.valid else None
        headline = ("≈ " + fmt(combined, "DZD")) if combined is not None else "—"
        fit_label(box, headline, 22, P["red"] if combined is not None and combined < ZERO else P["text"],
                  minimum=14, bg=P["card"]).pack(fill="x", padx=18, pady=(4, 10))
        parts = [fmt(totals[c], c) for c in CURRENCIES if totals[c] != ZERO]
        label(box, "  ·  ".join(parts) if parts and self.snapshot.valid else "Nothing recorded",
              11, P["muted"], bg=P["card"], wraplength=300, justify="left").pack(
                  fill="x", padx=18, pady=(0, 15))
        return box

    def chart_card(self, parent, title, subtitle=""):
        box = card(parent, tone=P["card"])
        head = tk.Frame(box, bg=P["card"])
        head.pack(fill="x", padx=18, pady=(15, 8))
        label(head, title, 14, P["text"], True, bg=P["card"]).pack(side="left")
        if subtitle:
            label(head, subtitle, 11, P["dim"], bg=P["card"]).pack(side="right")
        inner = tk.Frame(box, bg=P["card"])
        inner.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        return box, inner

    def chart_message(self, parent, text):
        label(parent, text, 12, P["dim"], bg=P["card"], anchor="center", wraplength=320,
              justify="center").pack(fill="both", expand=True, pady=40)

    def trend_chart(self, parent, show=("income", "expense")):
        series = monthly_dinar_series(self.snapshot, self.selected_month) if self.snapshot.valid else None
        if series is None:
            self.chart_message(parent, "Set USD and EUR display rates to chart every currency in dinars.")
        elif not any(inc or exp for _, inc, exp in series):
            self.chart_message(parent, "Nothing recorded in these six months yet.")
        else:
            TrendChart(parent, series, show=show, selected=self.selected_month).pack(fill="x")

    def category_chart(self, parent):
        items = category_dinar_totals(self.snapshot, self.selected_month) if self.snapshot.valid else None
        if items is None:
            self.chart_message(parent, "Set USD and EUR display rates to chart every currency in dinars.")
        elif not items:
            self.chart_message(parent, "No expenses recorded in " + month_label(self.selected_month, True) + ".")
        else:
            CategoryChart(parent, items).pack(fill="x")

    def currency_cards(self, parent, kind):
        totals = self.snapshot.monthly(self.selected_month, kind)
        row = tk.Frame(parent, bg=P["bg"])
        row.pack(fill="x", pady=(0, 18))
        color = P["green"] if kind == "income" else P["red"]
        for i, currency in enumerate(CURRENCIES):
            row.grid_columnconfigure(i, weight=1, uniform="currency")
            box = card(row, tone=P["card"])
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
            label(box, currency, 11, P["dim"], True, bg=P["card"]).pack(fill="x", padx=18, pady=(16, 8))
            value = totals[currency]
            fit_label(box, fmt(value, currency) if self.snapshot.valid else "—", 26, color,
                      minimum=15, bg=P["card"]).pack(fill="x", padx=18)
            equivalent = (dinar_equivalent_text(value, currency, self.snapshot.settings)
                          if self.snapshot.valid else "")
            label(box, equivalent or ("Received" if kind == "income" else "Spent"), 11, P["muted"],
                  bg=P["card"]).pack(fill="x", padx=18, pady=(4, 16))

    def two_up(self, parent, weights=(3, 2)):
        row = tk.Frame(parent, bg=P["bg"])
        row.pack(fill="x", pady=(0, 24))
        for i, weight in enumerate(weights):
            row.grid_columnconfigure(i, weight=weight, uniform="two_up")
        return row

    def render_page(self):
        page = self.current_page
        configurations = {
            "overview": ("Overview", [("Add expense", lambda: self.open_form("expense"), "primary"),
                                      ("Add income", lambda: self.open_form("income"), "secondary")]),
            "income": ("Income", [("New income", lambda: self.open_form("income"), "primary")]),
            "expenses": ("Expenses", [("New expense", lambda: self.open_form("expense"), "primary")]),
            "transfers": ("Transfers", [("New transfer", lambda: self.open_form("transfer"), "primary")]),
            "savings": ("Savings", [("Add savings", lambda: self.open_form("savings_deposit"), "primary"),
                                    ("Release", lambda: self.open_form("savings_withdraw"), "secondary")]),
            "lending": ("Lending", [("New loan", lambda: self.open_form("loan_out"), "primary")]),
            "activity": ("All activity", [("Reset filters", self.review_reset, "secondary"),
                                          ("Export this view", self.export_review, "secondary")])}
        title, actions = configurations[page]
        self.page_header(title, "", actions)
        if page == "activity":
            self.render_review()
            return
        body = self.page_body()
        if not self.online:
            self.notice(body, "Offline snapshot. Balances may be out of date and changes are disabled.", P["red"])
        if self.snapshot.errors:
            self.notice(body, "Balances are unavailable until the ledger issue is reviewed in Settings.", P["red"])
        elif self.snapshot.legacy_pending != ZERO and page == "overview":
            self.notice(body, f"Your former pending USD balance ({fmt(self.snapshot.legacy_pending, 'USD')}) is included in USD bank. Review that total once after upgrading.", P["amber"])

        if page == "overview":
            self.dinar_total_card(body)
            self.month_bar(body, "Monthly activity")
            inc = self.snapshot.monthly(self.selected_month, "income")
            exp = self.snapshot.monthly(self.selected_month, "expense")
            net = {c: inc[c] - exp[c] for c in CURRENCIES}
            stats = tk.Frame(body, bg=P["bg"])
            stats.pack(fill="x", pady=(0, 18))
            for i, (name, values, color) in enumerate([
                    ("Income", inc, P["green"]), ("Expenses", exp, P["red"]),
                    ("Net cash flow", net, P["accent_dark"])]):
                stats.grid_columnconfigure(i, weight=1, uniform="stats")
                self.triple_summary(stats, name, values, color).grid(
                    row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            charts = self.two_up(body)
            trend, trend_inner = self.chart_card(charts, "Last 6 months", "DZD equivalent")
            trend.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.trend_chart(trend_inner)
            spend, spend_inner = self.chart_card(charts, "Where it went", month_label(self.selected_month, True))
            spend.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
            self.category_chart(spend_inner)
            heading = tk.Frame(body, bg=P["bg"])
            heading.pack(fill="x", pady=(0, 12))
            label(heading, "Accounts", 19, bold=True).pack(side="left")
            self.account_cards(body)
            self.activity_table(body, allowed=None, selected_month=True, compact=True)
        elif page in ("income", "expenses"):
            kind = "income" if page == "income" else "expense"
            self.month_bar(body, "Monthly income" if kind == "income" else "Monthly expenses")
            self.currency_cards(body, kind)
            if kind == "income":
                box, inner = self.chart_card(body, "Income, last 6 months", "DZD equivalent")
                box.pack(fill="x", pady=(0, 24))
                self.trend_chart(inner, show=("income",))
            else:
                charts = self.two_up(body, (1, 1))
                spend, spend_inner = self.chart_card(charts, "By category", "DZD equivalent")
                spend.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
                self.category_chart(spend_inner)
                trend, trend_inner = self.chart_card(charts, "Expenses, last 6 months", "DZD equivalent")
                trend.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
                self.trend_chart(trend_inner, show=("expense",))
            self.activity_table(body, allowed={kind}, selected_month=True)
        elif page == "transfers":
            self.activity_table(body, allowed={"transfer", "adjustment"})
        elif page == "savings":
            self.account_cards(body, mode="saved")
            self.activity_table(body, allowed={"savings_deposit", "savings_withdraw"})
        elif page == "lending":
            self.render_loans(body)
        else:
            self.activity_table(body, allowed=None, allow_scope=True)

    # ------------------------------------------------------------------
    # All activity: a dense checking table for finding mistakes.
    # ------------------------------------------------------------------
    REVIEW_COLUMNS = [
        ("check", "✓", 30, "center", False),
        ("flag", "", 34, "center", False),
        ("date", "Date", 124, "w", False),
        ("description", "Description", 220, "w", True),
        ("category", "Category", 150, "w", False),
        ("account", "Account", 175, "w", False),
        ("in", "In", 135, "e", False),
        ("out", "Out", 135, "e", False),
        ("balance", "Balance", 145, "e", False),
    ]
    REVIEW_UNITS = [("week", "Week"), ("month", "Month"), ("quarter", "3 months"),
                    ("year", "Year"), ("all", "All time")]

    def review_state(self):
        if getattr(self, "_review", None) is None:
            start, end = period_for("month", date.today())
            self._review = {"query": "", "account": "All accounts", "type": "All types",
                            "category": "All categories", "checked": "All rows", "voided": False,
                            "unit": "month", "start": start, "end": end, "sort": "date",
                            "descending": False, "selected": [], "yview": 0.0, "actual": "",
                            "only_matches": False}
        return self._review

    def review_reset(self):
        self._review = None
        self.render()

    def review_preset(self, unit):
        st = self.review_state()
        anchor = st["start"] or date.today()
        if unit == "all":
            st["start"] = st["end"] = None
        else:
            today = date.today()
            # Keep looking at the same stretch of time when switching unit,
            # unless nothing is chosen yet, then start from today.
            st["start"], st["end"] = period_for(unit, anchor if st["unit"] != "all" else today)
        st["unit"], st["yview"] = unit, 0.0
        self.render()

    def review_step(self, delta):
        st = self.review_state()
        if self.current_page != "activity" or self.modals or st["start"] is None:
            return
        st["start"], st["end"] = shift_period(st["start"], st["end"], st["unit"], delta)
        st["yview"] = 0.0
        self.render()

    def review_jump_today(self):
        st = self.review_state()
        unit = st["unit"] if st["unit"] in ("week", "month", "quarter", "year") else "month"
        st["unit"] = unit
        st["start"], st["end"] = period_for(unit, date.today())
        self.render()

    def review_custom_range(self, start_text, end_text):
        st = self.review_state()
        try:
            start = date.fromisoformat(start_text.strip())
            end = date.fromisoformat(end_text.strip())
        except ValueError:
            self.toast("Use YYYY-MM-DD for both dates, for example 2026-10-01.", error=True)
            return
        if end < start:
            start, end = end, start
        unit = "days"
        # Whole calendar months step month by month, so Apr-Jun is followed by Jul-Sep.
        if start.day == 1 and end == month_end(end):
            months = (end.year - start.year) * 12 + end.month - start.month + 1
            unit = {1: "month", 3: "quarter", 12: "year"}.get(months, "days")
            if unit == "year" and start.month != 1:
                unit = "days"
        st["unit"], st["start"], st["end"], st["yview"] = unit, start, end, 0.0
        self.render()

    def review_sort(self, column):
        st = self.review_state()
        if st["sort"] == column:
            st["descending"] = not st["descending"]
        else:
            st["sort"], st["descending"] = column, column in ("in", "out")
        self._fill_review(keep_scroll=False)

    def _review_account(self):
        return ACCOUNT_NAMES.get(self.review_state()["account"])

    def _review_rows(self):
        st, snap = self.review_state(), self.snapshot
        account = self._review_account()
        kind = next((k for k, n in KIND_NAMES.items() if n == st["type"]), None)
        category = None if st["category"] == "All categories" else st["category"]
        start = st["start"].isoformat() if st["start"] else None
        end = st["end"].isoformat() if st["end"] else None
        query = st["query"].casefold().strip()
        compact_query = query.replace(",", "").replace(" ", "")
        rows = []
        for r in snap.records:
            if r.voided and not st["voided"]:
                continue
            if account and not touches(r, account):
                continue
            if kind and r.kind != kind:
                continue
            if category and not (r.kind == "expense" and expense_category(r) == category):
                continue
            if (start and r.day < start) or (end and r.day > end):
                continue
            state = check_state(r, snap.checks)
            if st["checked"] == "Not checked" and state == "checked":
                continue
            if st["checked"] == "Checked" and state != "checked":
                continue
            if query:
                haystack = (f"{r.title} {r.description} {r.category} {r.notes} {r.borrower} "
                            f"{r.amount} {r.amount_text} {r.received if r.destination else ''} "
                            f"{r.day} {day_heading(r.day)} {r.route} {KIND_NAMES[r.kind]}").casefold()
                if query not in haystack and compact_query not in haystack.replace(",", ""):
                    continue
            rows.append(r)
        return rows

    def _review_match_amount(self):
        """If a real balance was typed for the chosen account, the gap to look for."""
        st = self.review_state()
        account = self._review_account()
        if not account or not st["actual"].strip():
            return None, None
        try:
            actual = money(st["actual"], "Real balance", signed=True)
        except ValidationError:
            return None, None
        closing = balance_through(self.snapshot, account, st["end"].isoformat() if st["end"] else None)
        return actual - closing, closing

    def _review_flows(self, record, account=None):
        """(ins, outs) as lists of (currency, Decimal) for one record."""
        ins, outs = [], []
        keys = [account] if account else list(ACCOUNTS)
        for key in keys:
            delta = held_delta(record, key)
            if delta > ZERO:
                ins.append((ACCOUNTS[key].currency, delta))
            elif delta < ZERO:
                outs.append((ACCOUNTS[key].currency, -delta))
        return ins, outs

    def _fill_review(self, keep_scroll=True):
        self._review_job = None
        tree = getattr(self, "_review_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        st, snap = self.review_state(), self.snapshot
        account = self._review_account()
        running = running_balances(snap, account) if account else {}
        partners = duplicate_partners(snap)
        gap, closing = self._review_match_amount()
        match_amount = abs(gap) if gap not in (None, ZERO) else None
        rows = self._review_rows()

        def matches(r):
            if match_amount is None:
                return False
            ins, outs = self._review_flows(r, account)
            return any(v == match_amount for _, v in ins + outs) or r.amount == match_amount
        matched = {r.id for r in rows if matches(r)}
        if st["only_matches"] and match_amount is not None:
            rows = [r for r in rows if r.id in matched]

        def amount_key(r, side):
            ins, outs = self._review_flows(r, account)
            items = ins if side == "in" else outs
            total = ZERO
            for currency, value in items:
                converted = amount_in_dinars(value, currency, snap.settings)
                total += converted if converted is not None else value
            return total
        sorters = {
            "date": chrono_key,
            "description": lambda r: r.title.casefold(),
            "category": lambda r: (expense_category(r) if r.kind == "expense" else KIND_NAMES[r.kind]).casefold(),
            "account": lambda r: r.route.casefold(),
            "in": lambda r: amount_key(r, "in"), "out": lambda r: amount_key(r, "out"),
            "balance": lambda r: running.get(r.id, ZERO),
            "check": lambda r: check_state(r, snap.checks),
            "flag": lambda r: (r.id in partners, r.id in matched),
        }
        rows.sort(key=lambda r: (sorters[st["sort"]](r), chrono_key(r)), reverse=st["descending"])

        previous = set(tree.selection()) or set(st["selected"])
        tree.delete(*tree.get_children())
        totals_in = {c: ZERO for c in CURRENCIES}
        totals_out = {c: ZERO for c in CURRENCIES}
        checked_count = 0
        for index, r in enumerate(rows):
            state = check_state(r, snap.checks)
            if state == "checked":
                checked_count += 1
            ins, outs = self._review_flows(r, account)
            if not r.voided:
                for currency, value in ins:
                    totals_in[currency] += value
                for currency, value in outs:
                    totals_out[currency] += value
            category = expense_category(r) if r.kind == "expense" else KIND_NAMES[r.kind]
            if r.kind in ("savings_deposit", "savings_withdraw"):
                category = ("Saved " if r.kind == "savings_deposit" else "Released ") + fmt(r.amount, r.currency)
            balance = ""
            if account:
                # One account means one currency: the header names it, cells stay numeric.
                balance = "voided" if r.voided else f"{running[r.id]:,.2f}" if r.id in running else ""
                in_text = "  ".join(f"{v:,.2f}" for _, v in ins)
                out_text = "  ".join(f"{v:,.2f}" for _, v in outs)
            else:
                in_text = " + ".join(fmt(v, c) for c, v in ins)
                out_text = " + ".join(fmt(v, c) for c, v in outs)
            flag = ("⚠" if r.id in partners else "") + ("≈" if r.id in matched else "")
            values = ("✓" if state == "checked" else "!" if state == "changed" else "",
                      flag, f"{r.day}  {date.fromisoformat(r.day):%a}",
                      ("[voided] " if r.voided else "") + r.title, category, r.route,
                      in_text, out_text, balance)
            tone = "dupe" if r.id in partners else "match" if r.id in matched else ("odd" if index % 2 else "even")
            tags = (tone, "voided") if r.voided else (tone,)
            tree.insert("", "end", iid=r.id, values=values, tags=tags)

        for cid, head, *_ in self.REVIEW_COLUMNS:
            arrow = ("  ▼" if st["descending"] else "  ▲") if st["sort"] == cid else ""
            if account and cid in ("in", "out", "balance"):
                head = f"{head} ({ACCOUNTS[account].currency})"
            tree.heading(cid, text=head + arrow)
        # Size columns to their real content so amounts are never cut off.
        widths = {}
        for position, (cid, head, *_rest) in enumerate(self.REVIEW_COLUMNS):
            longest = text_width(tree.heading(cid, "text") + "  ▲", 11, True)
            for iid in tree.get_children():
                longest = max(longest, text_width(tree.item(iid, "values")[position], 12))
            widths[cid] = longest + 22
        self._review_widths = widths
        self._layout_review_columns()
        keep = [i for i in previous if tree.exists(i)]
        if keep:
            tree.selection_set(keep)
        tree.update_idletasks()
        if keep_scroll:
            tree.yview_moveto(st["yview"])
        elif keep:
            tree.see(keep[0])
        else:
            tree.yview_moveto(0)

        labels = self._review_labels
        count = len(rows)
        labels["rows"].configure(text=f"{count:,}")
        labels["rows_sub"].configure(text=f"{checked_count:,} of {count:,} checked")
        bar = labels["progress"]
        bar.delete("all")
        width = max(10, bar.winfo_width())
        rounded_rect(bar, 0, 0, width, 6, 3, fill=P["hairline"])
        if count:
            rounded_rect(bar, 0, 0, max(6, width * checked_count / count), 6, 3, fill=P["green"])

        def money_lines(totals):
            parts = [fmt(totals[c], c) for c in CURRENCIES if totals[c] != ZERO]
            dinars = dzd_equivalent(totals, snap.settings)
            return ("  ·  ".join(parts) or "—",
                    ("≈ " + fmt(dinars, "DZD")) if dinars is not None and len(parts) > 1 else "")
        net = {c: totals_in[c] - totals_out[c] for c in CURRENCIES}
        for key, totals in (("in", totals_in), ("out", totals_out), ("net", net)):
            main, sub = money_lines(totals)
            labels[key].configure(text=main)
            labels[key + "_sub"].configure(text=sub)
        if account and "balance" in labels:
            cur = ACCOUNTS[account].currency
            opening = balance_through(snap, account, date.fromordinal(st["start"].toordinal() - 1).isoformat()) if st["start"] else ZERO
            closing_now = balance_through(snap, account, st["end"].isoformat() if st["end"] else None)
            labels["balance"].configure(text=f"{fmt(opening, cur)}  →  {fmt(closing_now, cur)}")
            end_text = f"{st['end']:%d %b %Y}" if st["end"] else "today"
            labels["balance_sub"].configure(text=f"Start of period  →  end ({end_text})")
            if gap is None:
                text = ("Type what the bank or wallet really shows to find the gap."
                        if not st["actual"].strip() else "Use a number like 1250.50")
                labels["compare"].configure(text=text, fg=P["dim"])
            elif gap == ZERO:
                labels["compare"].configure(text="Matches. No gap in this period.", fg=P["green"])
            else:
                direction = "more" if gap > ZERO else "less"
                hits = len(matched)
                hint = (f"{hits} row{'s' if hits != 1 else ''} with exactly {fmt(abs(gap), cur)} marked ≈"
                        if hits else f"No row of exactly {fmt(abs(gap), cur)}. Look for a missing entry")
                labels["compare"].configure(
                    text=f"Real is {fmt(abs(gap), cur)} {direction} than the app. {hint}.", fg=P["amber"])
        suffix = "" if count else "  No rows match. Widen the period or clear filters."
        self._review_count.configure(text=f"{count:,} rows  ·  {period_text(st['start'], st['end'], st['unit'])}{suffix}")
        self._review_selected()

    def _review_selected(self, event=None):
        tree = getattr(self, "_review_tree", None)
        panel = getattr(self, "_review_panel", None)
        if tree is None or panel is None or not tree.winfo_exists():
            return
        st = self.review_state()
        ids = list(tree.selection())
        st["selected"] = ids
        for child in panel.winfo_children():
            child.destroy()
        inner = tk.Frame(panel, bg=P["card"])
        inner.pack(fill="both", expand=True, padx=18, pady=18)
        snap = self.snapshot
        account = self._review_account()
        records = {r.id: r for r in snap.records}
        if not ids:
            label(inner, "Nothing selected", 15, P["text"], True, bg=P["card"]).pack(fill="x")
            label(inner, "Click a row, or use ↑ ↓ to move through the list. Shift-click to select a range "
                  "and see its total.", 12, P["muted"], bg=P["card"], wraplength=228,
                  justify="left").pack(fill="x", pady=(8, 0))
            return
        if len(ids) > 1:
            chosen = [records[i] for i in ids if i in records]
            totals_in = {c: ZERO for c in CURRENCIES}
            totals_out = {c: ZERO for c in CURRENCIES}
            for r in chosen:
                if r.voided:
                    continue
                ins, outs = self._review_flows(r, account)
                for c, v in ins:
                    totals_in[c] += v
                for c, v in outs:
                    totals_out[c] += v
            label(inner, f"{len(chosen)} rows selected", 16, P["text"], True, bg=P["card"]).pack(fill="x")
            for name, totals, color in (("In", totals_in, P["green"]), ("Out", totals_out, P["red"])):
                label(inner, name.upper(), 10, P["dim"], True, bg=P["card"]).pack(fill="x", pady=(16, 4))
                parts = [fmt(totals[c], c) for c in CURRENCIES if totals[c] != ZERO]
                label(inner, "\n".join(parts) or "—", 14, color, True, bg=P["card"],
                      justify="left").pack(fill="x")
            all_checked = all(check_state(r, snap.checks) == "checked" for r in chosen)
            Button(inner, ("Unmark all" if all_checked else f"Mark {len(chosen)} as checked") + "  (Space)",
                   lambda: self.review_toggle_checks(), variant="secondary" if all_checked else "primary",
                   height=36, small=True).pack(fill="x", pady=(22, 0))
            return
        r = records.get(ids[0])
        if r is None:
            return
        label(inner, KIND_NAMES[r.kind].upper() + ("  /  VOIDED" if r.voided else ""), 10,
              activity_color(r), True, bg=P["card"]).pack(fill="x")
        label(inner, r.title, 16, P["text"], True, bg=P["card"], wraplength=228,
              justify="left").pack(fill="x", pady=(6, 2))
        label(inner, r.amount_text, 17, activity_color(r), True, bg=P["card"], wraplength=228,
              justify="left").pack(fill="x")
        equivalent = record_dinar_equivalent(r, snap.settings)
        if equivalent:
            label(inner, equivalent, 11, P["dim"], bg=P["card"]).pack(fill="x", pady=(2, 0))
        rows = [("Date", f"{date.fromisoformat(r.day):%A} {date.fromisoformat(r.day).day} "
                         f"{date.fromisoformat(r.day):%B %Y}  ({r.day})"),
                ("Category" if r.kind == "expense" else "Type",
                 expense_category(r) if r.kind == "expense" else KIND_NAMES[r.kind]),
                ("Account", r.route)]
        if r.kind == "transfer":
            rows.append(("Rate", rate_text(r.account, r.destination, r.amount, r.received)))
        if account:
            running = running_balances(snap, account)
            if r.id in running:
                rows.append((f"{ACCOUNTS[account].name} after this",
                             fmt(running[r.id], ACCOUNTS[account].currency)))
        if r.notes:
            rows.append(("Notes", r.notes))
        state = check_state(r, snap.checks)
        mark = snap.checks.get(r.id, {})
        when = ""
        try:
            when = datetime.fromisoformat(mark.get("at", "")).astimezone().strftime("%d %b %Y, %H:%M")
        except ValueError:
            pass
        rows.append(("Checked", f"Yes, {when}" if state == "checked" else
                     "Edited after it was checked" if state == "changed" else "Not yet"))
        for heading, value in rows:
            label(inner, heading.upper(), 9, P["dim"], True, bg=P["card"]).pack(fill="x", pady=(12, 3))
            label(inner, value, 12, P["amber"] if heading == "Checked" and state == "changed" else P["text"],
                  bg=P["card"], wraplength=228, justify="left").pack(fill="x")
        partners = duplicate_partners(snap).get(r.id, [])
        for other_id in partners[:2]:
            other = records.get(other_id)
            if not other:
                continue
            warn = tk.Frame(inner, bg="#33270F")
            warn.pack(fill="x", pady=(14, 0))
            label(warn, "⚠  Possible duplicate", 11, P["amber"], True, bg="#33270F").pack(
                fill="x", padx=10, pady=(8, 2))
            label(warn, f"{other.day}  ·  {other.title}  ·  {other.amount_text}", 11, P["text"],
                  bg="#33270F", wraplength=210, justify="left").pack(fill="x", padx=10)
            Button(warn, "Jump to it", lambda o=other_id: self.review_jump(o), variant="secondary",
                   height=28, small=True).pack(anchor="w", padx=10, pady=(6, 10))
        actions = tk.Frame(inner, bg=P["card"])
        actions.pack(fill="x", pady=(20, 0))
        Button(actions, ("Unmark" if state == "checked" else "Mark as checked") + "  (Space)",
               lambda: self.review_toggle_checks(), variant="secondary" if state == "checked" else "primary",
               height=36, small=True).pack(fill="x")
        buttons = tk.Frame(actions, bg=P["card"])
        buttons.pack(fill="x", pady=(8, 0))
        if not r.voided and r.kind not in ("loan_repaid", "adjustment"):
            Button(buttons, "Edit", lambda: self.open_form(r.kind, record=r), variant="secondary",
                   height=34, small=True).pack(side="left", fill="x", expand=True, padx=(0, 4))
        Button(buttons, "Void / details", lambda: self.review_open(r.id), variant="secondary",
               height=34, small=True).pack(side="left", fill="x", expand=True)

    def review_open(self, rid=None):
        tree = getattr(self, "_review_tree", None)
        if rid is None and tree is not None:
            focus = tree.focus() or (tree.selection()[0] if tree.selection() else None)
            rid = focus
        record = self.snapshot.by_id(rid) if rid else None
        if record is None:
            return
        if self.modals:
            self.lift_modal()
            return
        DetailDialog(self, record)

    def review_jump(self, rid):
        tree = self._review_tree
        if not tree.exists(rid):
            other = self.snapshot.by_id(rid)
            st = self.review_state()
            if other is None:
                return
            # Widen the view just enough to show it, then select it.
            st.update({"query": "", "type": "All types", "category": "All categories",
                       "checked": "All rows", "only_matches": False, "selected": [rid]})
            if st["start"] and not (st["start"].isoformat() <= other.day <= st["end"].isoformat()):
                st["start"], st["end"] = period_for(st["unit"] if st["unit"] in PERIOD_MONTHS or st["unit"] == "week" else "month",
                                                    date.fromisoformat(other.day))
            self.render()
            tree = self._review_tree
            if not tree.exists(rid):
                self.toast("That entry is outside the account filter. Choose All accounts to see it.", error=True)
                return
        tree.selection_set([rid])
        tree.focus(rid)
        tree.see(rid)
        tree.focus_set()

    def review_toggle_checks(self, ids=None, value=None):
        tree = getattr(self, "_review_tree", None)
        if ids is None:
            ids = list(tree.selection()) if tree is not None and tree.winfo_exists() else []
        if not ids:
            return
        if not self.repo or not self.online or not hasattr(self.repo, "set_checks"):
            self.toast("Reconnect to save checks. They are stored in your database so every computer sees them.", error=True)
            return
        records = {r.id: r for r in self.snapshot.records}
        chosen = [records[i] for i in ids if i in records]
        if value is None:
            value = not all(check_state(r, self.snapshot.checks) == "checked" for r in chosen)
        changes = {r.id: (review_fingerprint(r) if value else None) for r in chosen}
        previous = copy.deepcopy(self.snapshot.checks)
        for rid, fp in changes.items():
            if fp is None:
                self.snapshot.checks.pop(rid, None)
            else:
                self.snapshot.checks[rid] = {"fp": fp, "at": now_utc()}
        st = self.review_state()
        if tree is not None and tree.winfo_exists():
            st["yview"] = tree.yview()[0]
        # Moving on after ticking one row keeps the keyboard flow quick.
        advance = value and len(chosen) == 1 and tree is not None and tree.winfo_exists()
        self._fill_review(keep_scroll=True)
        if advance:
            following = tree.next(chosen[0].id)
            if following:
                tree.selection_set([following])
                tree.focus(following)
                tree.see(following)
        repo, snapshot = self.repo, self.snapshot

        def done(result, error):
            if error is not None:
                if self.snapshot is snapshot:
                    self.snapshot.checks = previous
                    if self.current_page == "activity" and not self.modals:
                        self._fill_review()
                self.toast("The check could not be saved. " + friendly_error(error), error=True)
            elif repo is self.repo and self.snapshot is snapshot:
                self.snapshot.checks = result
        self.worker.submit(lambda: repo.set_checks(changes), done)

    def render_review(self):
        st = self.review_state()
        snap = self.snapshot
        outer = tk.Frame(self.content, bg=P["bg"])
        outer.pack(fill="both", expand=True, padx=34, pady=(0, 4))
        if not self.online:
            self.notice(outer, "Offline snapshot. You can look through it, but ticks and changes are disabled.", P["red"])
        if snap.errors:
            self.notice(outer, "Some records need review in Settings > Data checks. Totals may be incomplete.", P["red"])

        # Period: the stretch of time being checked.
        nav = tk.Frame(outer, bg=P["bg"])
        nav.pack(fill="x", pady=(0, 10))
        stepper = card(nav, tone=P["surface"], border=False)
        stepper.pack(side="left")
        prev_button = Button(stepper, "‹", lambda: self.review_step(-1), variant="ghost", width=36, height=34)
        prev_button.pack(side="left", padx=(3, 0), pady=3)
        label(stepper, period_text(st["start"], st["end"], st["unit"]), 14, P["text"], True,
              bg=P["surface"], width=20, anchor="center").pack(side="left", padx=4)
        next_button = Button(stepper, "›", lambda: self.review_step(1), variant="ghost", width=36, height=34)
        next_button.pack(side="left", padx=(0, 3), pady=3)
        prev_button.set_enabled(st["start"] is not None)
        next_button.set_enabled(st["start"] is not None)
        Button(nav, "Today", self.review_jump_today, variant="secondary", height=34, small=True).pack(
            side="left", padx=(8, 0))
        chips = tk.Frame(nav, bg=P["bg"])
        chips.pack(side="left", padx=(16, 0))
        for unit, name in self.REVIEW_UNITS:
            Button(chips, name, lambda u=unit: self.review_preset(u),
                   variant="primary" if st["unit"] == unit else "secondary", height=34, small=True).pack(
                side="left", padx=(0, 6))
        custom = tk.Frame(nav, bg=P["bg"])
        custom.pack(side="right")
        date_entries = []
        for i, (caption, value) in enumerate((("From", st["start"]), ("to", st["end"]))):
            label(custom, caption, 11, P["dim"]).pack(side="left", padx=(0 if i == 0 else 8, 6))
            shell = tk.Frame(custom, bg=P["field"], highlightthickness=1, highlightbackground=P["border"])
            shell.pack(side="left")
            var = tk.StringVar(value=value.isoformat() if value else "")
            entry = tk.Entry(shell, textvariable=var, width=11, font=font(12), bg=P["field"], fg=P["text"],
                             insertbackground=P["text"], relief="flat", bd=0, highlightthickness=0)
            entry.pack(padx=8, pady=7)
            date_entries.append(var)
        apply = lambda e=None: self.review_custom_range(date_entries[0].get(), date_entries[1].get())
        Button(custom, "Apply", apply, variant="secondary", height=34, small=True).pack(side="left", padx=(8, 0))
        for widget in custom.winfo_children():
            for child in [widget] + list(widget.winfo_children()):
                if isinstance(child, tk.Entry):
                    child.bind("<Return>", apply)

        # Filters.
        filters = tk.Frame(outer, bg=P["bg"])
        filters.pack(fill="x", pady=(0, 10))
        search_shell = tk.Frame(filters, bg=P["field"], highlightthickness=1, highlightbackground=P["border"])
        search_shell.pack(side="left", fill="x", expand=True, padx=(0, 8))
        icon = tk.Canvas(search_shell, width=30, height=34, bg=P["field"], highlightthickness=0)
        icon.pack(side="left")
        icon.create_oval(10, 10, 20, 20, outline=P["dim"], width=1.5)
        icon.create_line(19, 19, 24, 24, fill=P["dim"], width=1.5, capstyle="round")
        search_var = tk.StringVar(value=st["query"])
        search = tk.Entry(search_shell, textvariable=search_var, bg=P["field"], fg=P["text"],
                          insertbackground=P["text"], relief="flat", bd=0, highlightthickness=0,
                          font=font(12), selectbackground=P["selection"])
        search.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=7)
        self._review_search = search
        ghost = label(search_shell, "Search text, amount (1250 or 1,250) or date", 11, P["dim"], bg=P["field"])

        def ghost_state(*args):
            if search_var.get():
                ghost.place_forget()
            else:
                ghost.place(x=34, rely=.5, anchor="w")
        ghost.bind("<Button-1>", lambda e: search.focus_set())

        def searched(*args):
            st["query"] = search_var.get()
            ghost_state()
            if getattr(self, "_review_job", None):
                self.after_cancel(self._review_job)
            self._review_job = self.after(160, lambda: self._fill_review(keep_scroll=False))
        search_var.trace_add("write", searched)
        ghost_state()

        categories = list(CATEGORIES)
        for r in snap.records:
            if r.kind == "expense":
                name = expense_category(r)
                if name not in categories:
                    categories.append(name)

        def combo(key, values, width, rerender=False):
            var = tk.StringVar(value=st[key])
            box = ttk.Combobox(filters, textvariable=var, values=values, state="readonly", width=width,
                               font=font(12), style="Finance.TCombobox")
            box.pack(side="left", padx=(0, 8), ipady=2)

            def changed(event=None):
                st[key] = var.get()
                if rerender:
                    st["yview"] = 0.0
                    self.render()
                else:
                    self._fill_review(keep_scroll=False)
            box.bind("<<ComboboxSelected>>", changed)
        combo("account", ["All accounts"] + [a.name for a in ACCOUNTS.values()], 13, rerender=True)
        combo("type", ["All types"] + [KIND_NAMES[k] for k in KINDS], 13)
        combo("category", ["All categories"] + categories, 13)
        combo("checked", ["All rows", "Not checked", "Checked"], 11)
        voided_var = tk.BooleanVar(value=st["voided"])

        def voided_changed():
            st["voided"] = voided_var.get()
            self._fill_review(keep_scroll=False)
        tk.Checkbutton(filters, text="Voided", variable=voided_var, command=voided_changed, font=font(11),
                       bg=P["bg"], fg=P["muted"], activebackground=P["bg"], activeforeground=P["text"],
                       selectcolor=P["field"], bd=0, highlightthickness=0).pack(side="left")

        # Totals for whatever is on screen.
        summary = tk.Frame(outer, bg=P["bg"])
        summary.pack(fill="x", pady=(0, 10))
        account = self._review_account()
        boxes = [("rows", "Rows", P["text"]), ("in", "Money in", P["green"]), ("out", "Money out", P["red"]),
                 ("net", "Net change", P["accent_dark"])]
        if account:
            boxes.append(("balance", ACCOUNTS[account].name + " balance", P["text"]))
        self._review_labels = {}
        for i, (key, title, color) in enumerate(boxes):
            weight = 4 if key == "balance" else 3 if key in ("in", "out", "net") else 2
            summary.grid_columnconfigure(i, weight=weight, uniform="review_summary")
            box = card(summary, tone=P["card"])
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == len(boxes) - 1 else 5))
            label(box, title.upper(), 9, P["dim"], True, bg=P["card"]).pack(fill="x", padx=14, pady=(11, 3))
            value = label(box, "", 14, color, True, bg=P["card"], width=1, justify="left")
            value.pack(fill="x", padx=14)
            value.bind("<Configure>", lambda e, w=value: w.configure(wraplength=max(60, e.width - 4)))
            self._review_labels[key] = value
            sub = label(box, "", 10, P["dim"], bg=P["card"], width=1)
            sub.pack(fill="x", padx=14, pady=(2, 11 if key not in ("rows", "balance") else 4))
            self._review_labels[key + "_sub"] = sub
            if key == "rows":
                bar = tk.Canvas(box, height=6, bg=P["card"], highlightthickness=0)
                bar.pack(fill="x", padx=14, pady=(2, 12))
                bar.bind("<Configure>", lambda e: self._fill_review_progress())
                self._review_labels["progress"] = bar
            if key == "balance":
                row = tk.Frame(box, bg=P["card"])
                row.pack(fill="x", padx=14, pady=(2, 4))
                label(row, "Real balance", 10, P["muted"], bg=P["card"]).pack(side="left")
                shell = tk.Frame(row, bg=P["field"], highlightthickness=1, highlightbackground=P["border"])
                shell.pack(side="left", padx=(8, 0))
                actual_var = tk.StringVar(value=st["actual"])
                tk.Entry(shell, textvariable=actual_var, width=12, font=font(12), bg=P["field"], fg=P["text"],
                         insertbackground=P["text"], relief="flat", bd=0, highlightthickness=0).pack(padx=6, pady=3)
                only_var = tk.BooleanVar(value=st["only_matches"])

                def only_changed():
                    st["only_matches"] = only_var.get()
                    self._fill_review(keep_scroll=False)
                tk.Checkbutton(row, text="Only ≈ rows", variable=only_var, command=only_changed,
                               font=font(10), bg=P["card"], fg=P["muted"], activebackground=P["card"],
                               activeforeground=P["text"], selectcolor=P["field"], bd=0,
                               highlightthickness=0).pack(side="left", padx=(8, 0))

                def actual_changed(*args):
                    st["actual"] = actual_var.get()
                    if getattr(self, "_review_job", None):
                        self.after_cancel(self._review_job)
                    self._review_job = self.after(250, lambda: self._fill_review(keep_scroll=True))
                actual_var.trace_add("write", actual_changed)
                compare = label(box, "", 10, P["dim"], bg=P["card"], wraplength=360, justify="left")
                compare.pack(fill="x", padx=14, pady=(2, 10))
                self._review_labels["compare"] = compare

        # Table and the selected-row panel.
        main = tk.Frame(outer, bg=P["bg"])
        main.pack(fill="both", expand=True)
        panel = card(main, tone=P["card"], width=270)
        if st.get("panel", True):
            panel.pack(side="right", fill="y", padx=(10, 0))
        panel.pack_propagate(False)
        self._review_panel = panel
        table_box = card(main, tone=P["card"])
        table_box.pack(side="left", fill="both", expand=True)
        columns = [c[0] for c in self.REVIEW_COLUMNS]
        tree = ttk.Treeview(table_box, columns=columns, show="headings", style="Ledger.Treeview",
                            selectmode="extended")
        bar = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview,
                            style="Finance.Vertical.TScrollbar")

        def scrolled(first, last):
            bar.set(first, last)
            st["yview"] = float(first)
        tree.configure(yscrollcommand=scrolled)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.bind("<Configure>", self._layout_review_columns, add="+")
        for cid, head, width, anchor, stretch in self.REVIEW_COLUMNS:
            tree.heading(cid, text=head, anchor=anchor if anchor != "center" else "center",
                         command=lambda c=cid: self.review_sort(c))
            tree.column(cid, width=width, minwidth=min(width, 40), anchor=anchor, stretch=stretch)
        tree["displaycolumns"] = [c for c in columns if c != "balance" or account]
        tree.tag_configure("even", background=P["card"])
        tree.tag_configure("odd", background=P["card_alt"])
        tree.tag_configure("dupe", background="#33270F")
        tree.tag_configure("match", background="#12304D")
        tree.tag_configure("voided", foreground=P["dim"])
        self._review_tree = tree
        tree.bind("<<TreeviewSelect>>", self._review_selected)
        tree.bind("<space>", lambda e: (self.review_toggle_checks(), "break")[1])
        tree.bind("<Return>", lambda e: (self.review_open(), "break")[1])

        def clicked(event):
            if tree.identify_region(event.x, event.y) == "cell" and tree.identify_column(event.x) == "#1":
                row = tree.identify_row(event.y)
                if row:
                    self.review_toggle_checks([row])
                    return "break"
        tree.bind("<Button-1>", clicked)

        def double(event):
            if tree.identify_region(event.x, event.y) == "cell":
                self.review_open(tree.identify_row(event.y))
        tree.bind("<Double-1>", double)
        for sequence in (("<Alt-Left>", -1), ("<Alt-Right>", 1)):
            tree.bind(sequence[0], lambda e, d=sequence[1]: (self.review_step(d), "break")[1])

        footer = tk.Frame(outer, bg=P["bg"])
        footer.pack(fill="x", pady=(8, 0))
        self._review_count = label(footer, "", 11, P["dim"])
        self._review_count.pack(side="left")

        def toggle_panel():
            st["panel"] = not st.get("panel", True)
            self.render()
        Button(footer, "Hide details" if st.get("panel", True) else "Show details", toggle_panel,
               variant="ghost", height=26, small=True).pack(side="left", padx=(10, 0))
        mod, alt = ("⌘", "⌥") if IS_MAC else ("Ctrl+", "Alt+")
        label(footer, f"↑↓ move  ·  Shift-click range  ·  Space tick  ·  Enter open  ·  "
              f"{alt}← {alt}→ period  ·  {mod}F search  ·  ✓ checked  ! edited since  "
              f"⚠ possible duplicate  ≈ matches the gap", 10, P["dim"]).pack(side="right")
        self._fill_review(keep_scroll=True)
        self.after(60, lambda: tree.winfo_exists() and (tree.focus_set() if not self.modals else None))

    def _layout_review_columns(self, event=None):
        tree = getattr(self, "_review_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        total = tree.winfo_width()
        if total <= 50:
            return
        need = getattr(self, "_review_widths", {})
        shown = list(tree["displaycolumns"])
        fixed = {"check": 30, "flag": 34}
        for cid in ("date", "in", "out", "balance"):
            if cid in shown:
                fixed[cid] = max(70, need.get(cid, 100))
        flexible = {cid: min(need.get(cid, 120), cap) for cid, cap in (("category", 170), ("account", 190))}
        floors = {"category": 90, "account": 100, "description": 120}
        room = total - sum(fixed.values()) - 8
        description = max(floors["description"], room - sum(flexible.values()))
        overflow = sum(flexible.values()) + description - room
        if overflow > 0:
            for cid in ("category", "account"):
                give = min(overflow, flexible[cid] - floors[cid])
                flexible[cid] -= give
                overflow -= give
        widths = dict(fixed, **flexible, description=max(floors["description"], room - sum(flexible.values())))
        for cid, width in widths.items():
            tree.column(cid, width=int(width), stretch=False)

    def _fill_review_progress(self):
        labels = getattr(self, "_review_labels", {})
        bar = labels.get("progress")
        tree = getattr(self, "_review_tree", None)
        if bar is None or tree is None or not bar.winfo_exists():
            return
        count = len(tree.get_children())
        checked = sum(1 for i in tree.get_children() if tree.set(i, "check") == "✓")
        bar.delete("all")
        width = max(10, bar.winfo_width())
        rounded_rect(bar, 0, 0, width, 6, 3, fill=P["hairline"])
        if count and checked:
            rounded_rect(bar, 0, 0, max(6, width * checked / count), 6, 3, fill=P["green"])

    def export_review(self):
        tree = getattr(self, "_review_tree", None)
        if tree is None or not tree.winfo_exists():
            return self.export_csv()
        path = filedialog.asksaveasfilename(parent=self, title="Export this view", defaultextension=".csv",
                                            initialfile=f"finance-check-{date.today().isoformat()}.csv",
                                            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        def safe(value):
            text = str(value)
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text
        headings = ["Checked", "Flag", "Date", "Day", "Description", "Category", "Account", "In", "Out",
                    "Balance after", "Notes", "ID"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(headings)
                for iid in tree.get_children():
                    values = list(tree.item(iid, "values"))
                    record = self.snapshot.by_id(iid)
                    flag = " + ".join(name for mark, name in (("⚠", "Possible duplicate"), ("≈", "Matches gap"))
                                      if mark in values[1])
                    check = {"✓": "Yes", "!": "Edited since"}.get(values[0], "")
                    writer.writerow([check, flag] + [safe(v) for v in values[2:10]] +
                                    [safe(record.notes if record else ""), iid])
            self.toast("Exported exactly what is on screen, in the same order.")
        except OSError:
            self.toast("The CSV could not be saved. Choose a writable location.", error=True)

    def activity_table(self, parent, allowed=None, selected_month=False,
                       compact=False, allow_scope=False):
        state = self.filter_states.setdefault(
            self.current_page,
            {"query": "", "account": "All accounts", "type": "All types",
             "scope": "All dates", "page": 0, "voided": False})
        top = tk.Frame(parent, bg=P["bg"])
        top.pack(fill="x", pady=(0, 13))
        if compact or self.current_page != "activity":
            label(top, "Recent activity" if compact else "Transaction history",
                  19, bold=True).pack(side="left")
        if compact:
            Button(top, "View all", lambda: self.navigate("activity"),
                   variant="secondary", height=32, small=True).pack(side="right")
        else:
            filters = tk.Frame(parent, bg=P["bg"])
            filters.pack(fill="x", pady=(0, 12))

            search_shell = tk.Frame(filters, bg=P["field"], highlightthickness=1,
                                    highlightbackground=P["border"])
            search_shell.pack(side="left", fill="x", expand=True, padx=(0, 9))
            search_icon = tk.Canvas(search_shell, width=32, height=40,
                                    bg=P["field"], highlightthickness=0)
            search_icon.pack(side="left")
            search_icon.create_oval(10, 11, 21, 22, outline=P["dim"], width=1.5)
            search_icon.create_line(20, 21, 25, 26, fill=P["dim"], width=1.5,
                                    capstyle="round")
            search_var = tk.StringVar(value=state["query"])
            search = tk.Entry(search_shell, textvariable=search_var,
                              bg=P["field"], fg=P["text"],
                              insertbackground=P["text"], relief="flat", bd=0,
                              highlightthickness=0, font=font(13),
                              selectbackground=P["selection"])
            search.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=9)
            placeholder = label(search_shell, "Search transactions", 12, P["dim"],
                                bg=P["field"])
            def place_placeholder():
                if not search_var.get() and self.focus_get() is not search:
                    placeholder.place(x=39, rely=.5, anchor="w")
                else:
                    placeholder.place_forget()
            placeholder.bind("<Button-1>", lambda e: search.focus_set())
            search.bind("<FocusIn>", lambda e: (placeholder.place_forget(),
                                                  search_shell.configure(highlightbackground=P["accent"])))
            search.bind("<FocusOut>", lambda e: (search_shell.configure(highlightbackground=P["border"]),
                                                   place_placeholder()))
            def changed(*args):
                state["query"], state["page"] = search_var.get(), 0
                place_placeholder()
                if self._search_job:
                    self.after_cancel(self._search_job)
                self._search_job = self.after(150, self.fill_table)
            search_var.trace_add("write", changed)
            place_placeholder()

            account_var = tk.StringVar(value=state["account"])
            account_combo = ttk.Combobox(
                filters, textvariable=account_var,
                values=["All accounts"] + [a.name for a in ACCOUNTS.values()],
                state="readonly", width=14, font=font(12),
                style="Finance.TCombobox")
            account_combo.pack(side="left", ipady=4)
            def account_changed(event=None):
                state["account"], state["page"] = account_var.get(), 0
                self.fill_table()
            account_combo.bind("<<ComboboxSelected>>", account_changed)

            type_options = ["All types"] + [KIND_NAMES[k] for k in KINDS]
            type_var = tk.StringVar(value=state["type"])
            type_combo = ttk.Combobox(
                filters, textvariable=type_var, values=type_options,
                state="readonly", width=13, font=font(12),
                style="Finance.TCombobox")
            type_combo.pack(side="left", padx=(9, 0), ipady=4)
            def type_changed(event=None):
                state["type"], state["page"] = type_var.get(), 0
                self.fill_table()
            type_combo.bind("<<ComboboxSelected>>", type_changed)

            if allow_scope:
                scope_var = tk.StringVar(value=state["scope"])
                scope = ttk.Combobox(
                    filters, textvariable=scope_var,
                    values=["All dates", "Selected month"], state="readonly",
                    width=13, font=font(12), style="Finance.TCombobox")
                scope.pack(side="left", padx=(9, 0), ipady=4)
                def scope_changed(event=None):
                    state["scope"], state["page"] = scope_var.get(), 0
                    self.render()
                scope.bind("<<ComboboxSelected>>", scope_changed)
                if state["scope"] == "Selected month":
                    self.month_bar(parent, "Activity period")

            visibility = tk.Frame(parent, bg=P["bg"])
            visibility.pack(fill="x", pady=(0, 10))
            label(visibility, "Select a row to review, edit or void it.",
                  11, P["dim"]).pack(side="left")
            void_var = tk.BooleanVar(value=state["voided"])
            def show_void():
                state["voided"], state["page"] = void_var.get(), 0
                self.fill_table()
            tk.Checkbutton(
                visibility, text="Include voided", variable=void_var,
                command=show_void, font=font(11), bg=P["bg"], fg=P["dim"],
                activebackground=P["bg"], activeforeground=P["text"],
                selectcolor=P["field"], bd=0,
                highlightthickness=0).pack(side="right")

        panel = card(parent, tone=P["card"])
        panel.pack(fill="x")
        if not compact:
            header = tk.Frame(panel, bg=P["bg"], height=36)
            header.pack(fill="x")
            header.pack_propagate(False)
            heads = {name: label(header, text_, 9, P["dim"], True, bg=P["bg"], anchor=anchor)
                     for name, text_, anchor in [("desc", "DESCRIPTION", "w"),
                                                 ("route", "ACCOUNT / ROUTE", "w"),
                                                 ("amount", "AMOUNT", "e")]}
            def place_heads(event, heads=heads):
                for name, (x, w) in self.activity_columns(event.width).items():
                    shift = 22 if name == "desc" else 0
                    heads[name].place(x=x + shift, rely=.5, anchor="w", width=w - shift)
            header.bind("<Configure>", place_heads)
        host = tk.Frame(panel, bg=P["card"])
        host.pack(fill="x")
        footer = tk.Frame(parent, bg=P["bg"])
        footer.pack(fill="x", pady=(11, 8))
        count_label = label(footer, "", 11, P["dim"])
        count_label.pack(side="left")
        if not compact:
            Button(footer, "Next", lambda: self.page_table(1),
                   variant="secondary", height=31, small=True).pack(side="right")
            Button(footer, "Previous", lambda: self.page_table(-1),
                   variant="secondary", height=31, small=True).pack(
                       side="right", padx=(0, 8))
        self._table_context = {
            "host": host, "state": state, "allowed": allowed,
            "month": selected_month, "compact": compact,
            "allow_scope": allow_scope, "count": count_label,
            "page_size": 5 if compact else 25}
        self.fill_table()

    @staticmethod
    def activity_columns(width):
        """One column layout shared by the header and every row, so they line up."""
        pad, gap, amount_w = 18, 14, 250
        usable = max(200, width - 2 * pad - amount_w - 2 * gap)
        desc_w = int(usable * 0.58)
        return {"desc": (pad, desc_w), "route": (pad + desc_w + gap, usable - desc_w),
                "amount": (width - pad - amount_w, amount_w)}

    def _activity_row(self, parent, record, index, compact=False):
        base = P["card"] if index % 2 == 0 else P["card_alt"]
        row = tk.Frame(parent, bg=base, height=64)
        row.pack(fill="x")
        row.pack_propagate(False)
        amount_color = activity_color(record)
        title_text = ("Voided • " if record.voided else "") + record.title
        equivalent = record_dinar_equivalent(record, self.snapshot.settings)

        description = tk.Frame(row, bg=base)
        dot = tk.Canvas(description, width=12, height=20, bg=base, highlightthickness=0)
        dot.pack(side="left", padx=(0, 10))
        dot.create_oval(3, 7, 9, 13, fill=amount_color, outline="")
        text_host = tk.Frame(description, bg=base)
        text_host.pack(side="left", fill="x", expand=True)
        label(text_host, title_text, 13, P["text"] if not record.voided else P["dim"], True,
              bg=base, width=1).pack(fill="x")
        subtitle = KIND_NAMES[record.kind]
        if record.kind == "expense":
            subtitle += "  ·  " + expense_category(record)
        label(text_host, subtitle, 10, P["dim"], bg=base, width=1).pack(fill="x", pady=(3, 0))
        route = label(row, record.route, 12, P["muted"], bg=base, justify="left")
        amount_inner = tk.Frame(row, bg=base)
        label(amount_inner, record.amount_text, 13, amount_color, True,
              bg=base, anchor="e", width=1).pack(fill="x")
        if equivalent:
            label(amount_inner, equivalent, 10, P["dim"], bg=base, anchor="e", width=1).pack(
                fill="x", pady=(3, 0))

        def layout(event):
            columns = self.activity_columns(event.width)
            x, w = columns["desc"]
            description.place(x=x, rely=.5, anchor="w", width=w)
            x, w = columns["route"]
            route.configure(wraplength=w)
            route.place(x=x, rely=.5, anchor="w", width=w)
            x, w = columns["amount"]
            amount_inner.place(x=x, rely=.5, anchor="w", width=w)
        row.bind("<Configure>", layout)

        widgets: List[tk.Misc] = []
        def collect(widget):
            widgets.append(widget)
            for child in widget.winfo_children():
                collect(child)
        collect(row)

        def paint(color):
            for widget in widgets:
                try:
                    widget.configure(bg=color)
                except tk.TclError:
                    pass
        def enter(event=None):
            paint(P["card_hover"])
        def leave(event=None):
            try:
                inside = row.winfo_containing(row.winfo_pointerx(), row.winfo_pointery())
            except (tk.TclError, KeyError):
                inside = None
            if inside not in widgets:
                paint(base)
        def open_record(event=None):
            if not self.modals:
                DetailDialog(self, record)
            else:
                self.lift_modal()
            return "break"
        for widget in widgets:
            safe_cursor(widget, True)
            widget.bind("<Button-1>", open_record)
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", lambda e: row.after_idle(leave), add="+")
        tk.Frame(parent, bg=P["hairline"], height=1).pack(fill="x")

    def _day_header(self, parent, day, total_text=""):
        head = tk.Frame(parent, bg=P["surface"], height=34)
        head.pack(fill="x")
        head.pack_propagate(False)
        label(head, day_heading(day).upper(), 10, P["muted"], True, bg=P["surface"]).pack(
            side="left", padx=18)
        if total_text:
            label(head, total_text, 10, P["dim"], bg=P["surface"]).pack(side="right", padx=18)

    def filtered_records(self, context=None):
        context = context or self._table_context
        if context is None:
            return [r for r in self.snapshot.records if not r.voided]
        state = context["state"]
        query = state["query"].casefold().strip() if not context["compact"] else ""
        account = ACCOUNT_NAMES.get(state["account"])
        kind_filter = next((kind for kind, name in KIND_NAMES.items()
                            if name == state.get("type")), None)
        records = []
        for record in self.snapshot.records:
            if record.voided and (context["compact"] or not state["voided"]):
                continue
            if context["allowed"] and record.kind not in context["allowed"]:
                continue
            if kind_filter and record.kind != kind_filter:
                continue
            if (context["month"] or
                    (context["allow_scope"] and state["scope"] == "Selected month")) and not record.day.startswith(self.selected_month):
                continue
            if not context["compact"] and account and account not in (record.account, record.destination):
                continue
            if query:
                haystack = (f"{record.title} {record.description} {record.category} "
                            f"{record.notes} {record.borrower} {record.amount} {record.amount_text} "
                            f"{record.received if record.destination else ''} "
                            f"{record.day} {day_heading(record.day)} {record.route} {record.currency} "
                            f"{KIND_NAMES[record.kind]}").casefold()
                # Let "1250", "1,250" and "1 250" all find the same amount.
                if query not in haystack and query.replace(",", "").replace(" ", "") not in haystack.replace(",", ""):
                    continue
            records.append(record)
        records.sort(
            key=lambda record: (
                record.day,
                str(record.raw.get("created_at", record.raw.get("date", ""))),
                record.id), reverse=True)
        return records

    def fill_table(self):
        self._search_job = None
        context = self._table_context
        if not context or not context["host"].winfo_exists():
            return
        host, state = context["host"], context["state"]
        rows = self.filtered_records(context)
        size = context["page_size"]
        pages = max(1, (len(rows) + size - 1)//size)
        state["page"] = max(0, min(state["page"], pages-1))
        start = 0 if context["compact"] else state["page"] * size
        for child in host.winfo_children():
            child.destroy()
        visible = rows[start:start+size]
        if not visible:
            empty = tk.Frame(host, bg=P["card"])
            empty.pack(fill="x")
            label(empty, "No matching transactions", 14, P["muted"], True,
                  bg=P["card"], anchor="center").pack(fill="x", pady=(28, 6))
            label(empty, "Add a record or adjust the filters.", 11, P["dim"],
                  bg=P["card"], anchor="center").pack(fill="x", pady=(0, 28))
            context["count"].configure(text="No matching transactions")
            return
        last_day = None
        index = 0
        for record in visible:
            if record.day != last_day:
                self._day_header(host, record.day)
                last_day, index = record.day, 0
            self._activity_row(host, record, index, context["compact"])
            index += 1
        suffix = " • selected month" if context["month"] else ""
        pages_text = f"  •  page {state['page'] + 1} of {pages}" if pages > 1 and not context["compact"] else ""
        context["count"].configure(
            text=f"Showing {start+1}–{min(start+size, len(rows))} of {len(rows)}{suffix}{pages_text}")

    def page_table(self, delta):
        if self._table_context:
            self._table_context["state"]["page"] += delta
            self.fill_table()

    def open_selected(self, tree):
        # Retained for compatibility with older extensions; the Apple-style
        # activity list opens rows directly.
        selection = tree.selection() if hasattr(tree, "selection") else ()
        if selection:
            record = self.snapshot.by_id(selection[0])
            if record:
                DetailDialog(self, record)

    def render_loans(self, parent):
        totals = {c: ZERO for c in CURRENCIES}
        for rid, remaining in self.snapshot.outstanding.items():
            loan = self.snapshot.by_id(rid)
            if loan:
                totals[loan.currency] += remaining
        row = tk.Frame(parent, bg=P["bg"])
        row.pack(fill="x", pady=(0, 24))
        for i, currency in enumerate(CURRENCIES):
            row.grid_columnconfigure(i, weight=1, uniform="loans")
            box = card(row)
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
            label(box, currency + "  /  OUTSTANDING", 10, P["dim"], True).pack(fill="x", padx=20, pady=(20, 12))
            value = totals[currency]
            equivalent = (dinar_equivalent_text(value, currency,
                                                self.snapshot.settings)
                          if self.snapshot.valid else "")
            fit_label(box, fmt(value, currency) if self.snapshot.valid else "--", 26,
                      P["amber"], minimum=15).pack(fill="x", padx=20, pady=(0, 4))
            label(box, equivalent or "Owed to you", 11, P["muted"]).pack(
                fill="x", padx=20, pady=(0, 22))
        label(parent, "Your loans", 17, bold=True).pack(fill="x", pady=(0, 15))
        loans = [r for r in self.snapshot.records if r.kind == "loan_out" and not r.voided]
        loans.sort(key=lambda r: (self.snapshot.outstanding.get(r.id, ZERO) == ZERO, r.day), reverse=False)
        if not loans:
            self.notice(parent, "No loans recorded. Add one to track the amount owed and repayments.", P["muted"])
        for loan in loans:
            remaining = self.snapshot.outstanding.get(loan.id, ZERO)
            box = card(parent)
            box.pack(fill="x", pady=(0, 10))
            right = tk.Frame(box, bg=P["card"])
            right.pack(side="right", padx=20, pady=20)
            remaining_equivalent = dinar_equivalent_text(
                remaining, loan.currency, self.snapshot.settings)
            label(right, fmt(remaining, loan.currency), 23,
                  P["amber"] if remaining else P["green"], True).pack(anchor="e")
            if remaining_equivalent:
                label(right, remaining_equivalent, 11, P["muted"]).pack(
                    anchor="e", pady=(3, 0))
            label(right, "Remaining" if remaining else "Fully repaid", 11,
                  P["dim"]).pack(anchor="e", pady=(3, 12))
            actions = tk.Frame(right, bg=P["card"])
            actions.pack(anchor="e")
            Button(actions, "Details", lambda r=loan: DetailDialog(self, r), variant="secondary", height=32, small=True).pack(side="left")
            if remaining:
                Button(actions, "Record repayment", lambda r=loan: self.open_form("loan_repaid", loan=r), height=32, small=True).pack(side="left", padx=(8, 0))
            left = tk.Frame(box, bg=P["card"])
            left.pack(side="left", fill="x", expand=True, padx=22, pady=22)
            label(left, loan.borrower, 18, bold=True).pack(fill="x")
            label(left, f"{loan.day}  /  {loan.route}", 12, P["muted"]).pack(fill="x", pady=(8, 5))
            label(left, f"Lent {fmt(loan.amount, loan.currency)}  /  Repaid {fmt(loan.amount-remaining, loan.currency)}", 12, P["dim"]).pack(fill="x")
            if loan.notes:
                label(left, loan.notes, 12, P["muted"], wraplength=390, justify="left").pack(fill="x", pady=(10, 0))

    def render_settings(self):
        self.page_header("Settings", "")
        body = self.page_body()
        sections = []
        def section(title, subtitle):
            box = card(body)
            box.pack(fill="x", pady=(0, 18))
            inner = tk.Frame(box, bg=P["card"])
            inner.pack(fill="x", padx=24, pady=24)
            label(inner, title, 18, bold=True).pack(fill="x")
            label(inner, subtitle, 12, P["muted"], wraplength=880, justify="left").pack(fill="x", pady=(9, 17))
            sections.append(inner)
            return inner
        conversion = section("Dinar display estimate", "Your combined DZD total includes every bank and cash account plus savings, without counting savings twice. Unpaid loans are shown under Lending, not in this cash total.")
        rates = display_rates(self.snapshot.settings)
        for currency in DISPLAY_RATE_KEYS:
            text = f"1 {currency} = {compact_rate(rates[currency])} DZD" if rates[currency] is not None else f"{currency}: no valid display rate saved yet"
            label(conversion, text, 14, P["muted"]).pack(fill="x", pady=(0, 8))
        label(conversion, "Display-only preferences. Changing them never edits transactions or changes a transfer's calculated rate.",
              11, P["dim"], wraplength=850, justify="left").pack(fill="x", pady=(4, 14))
        Button(conversion, "Edit display rates", self.open_display_rates, variant="secondary").pack(anchor="w")
        accounts = section("Accounts and opening balances", "USD bank, EUR bank, EUR cash and DZD cash. Savings stay attached to the account that holds them.")
        totals = self.snapshot.currency_totals(include_loans=True)
        label(accounts, "TOTAL TRACKED ASSETS  /  AVAILABLE + SAVED + OUTSTANDING LOANS", 10, P["dim"], True).pack(fill="x", pady=(0, 9))
        label(accounts, "     ".join(fmt(totals[c], c) for c in CURRENCIES) if self.snapshot.valid else "Unavailable until data checks are resolved.", 20, bold=True).pack(fill="x", pady=(0, 7))
        tracked_dzd = dzd_equivalent(totals, self.snapshot.settings) if self.snapshot.valid else None
        if self.snapshot.valid:
            label(accounts, ("Estimated value  ≈ " + fmt(tracked_dzd, "DZD"))
                  if tracked_dzd is not None else "Set USD and EUR display rates to value tracked assets in DZD.",
                  12, P["muted"]).pack(fill="x", pady=(0, 18))
        Button(accounts, "+ Opening balance", lambda: self.open_form("opening"), variant="secondary").pack(anchor="w")
        backups = section("Backups and exports", "A full local JSON backup is made when the database opens, before this client can write. Exports contain private financial records; keep them somewhere secure.")
        buttons = tk.Frame(backups, bg=P["card"])
        buttons.pack(fill="x")
        Button(buttons, "Export full backup", self.export_backup, variant="secondary").pack(side="left")
        Button(buttons, "Export activity CSV", self.export_csv, variant="secondary").pack(side="left", padx=(10, 0))
        if self.repo and self.repo.backup_path:
            label(backups, str(self.repo.backup_path), 11, P["dim"], wraplength=860, justify="left").pack(fill="x", pady=(16, 0))
        label(backups, "Restore is deliberately not automatic: loading an old backup must not overwrite newer records from your other computer.", 11, P["dim"], wraplength=860, justify="left").pack(fill="x", pady=(10, 0))
        connection = section("Connection and local security", "Use the updated app on every computer. Older versions do not understand EUR cash, generic transfers or voided records.")
        if self.repo:
            params = self.repo.parameters
            label(connection, f"Host: {params.get('host', '(local)')}\nDatabase: {params.get('dbname', '(default)')}\nTLS mode: {params.get('sslmode', 'default')}\nCredential storage: {self.storage_method}", 13, P["muted"], justify="left", wraplength=870).pack(fill="x", pady=(0, 15))
        if self.storage_note:
            label(connection, self.storage_note, 12, P["amber"], wraplength=860, justify="left").pack(fill="x", pady=(0, 15))
        if isinstance(self.repo, Repository):
            label(connection, "The URL's certificate-verification settings are preserved. TLS 'require' encrypts the connection; use 'verify-full' with a trusted CA configuration for hostname verification.", 11, P["dim"], wraplength=860, justify="left").pack(fill="x", pady=(0, 15))
        Button(connection, "Reconnect / change database", self.change_connection, variant="secondary").pack(anchor="w")
        checks = section("Data checks", "Invalid records are flagged instead of quietly changing your totals. Linked repayments determine what is still owed.")
        messages = self.snapshot.errors + self.snapshot.warnings
        if self.snapshot.legacy_pending:
            messages.append(f"Former pending USD included in USD bank: {fmt(self.snapshot.legacy_pending, 'USD')}. This is a tracking merge, not a real transfer.")
        if self.repo and self.repo.storage_warning:
            messages.append(self.repo.storage_warning)
        if not messages:
            label(checks, "No ledger consistency issues detected.", 13, P["green"]).pack(fill="x")
        else:
            for text in messages:
                label(checks, text, 12, P["red"] if text in self.snapshot.errors else P["amber"], wraplength=860, justify="left").pack(fill="x", pady=(0, 12))
        label(body, "FINANCE " + APP_VERSION + "  /  BUILT FOR A SINGLE PERSONAL LEDGER", 10, P["dim"], True).pack(fill="x", pady=(10, 12))

    def open_form(self, kind, record=None, loan=None, account=None):
        if self.modals:
            next(iter(self.modals)).lift()
            return
        if not self.can_write():
            self.toast("Changes are disabled until your database is connected and data checks pass.", error=True)
            return
        TransactionDialog(self, kind, record=record, loan=loan, account=account)

    def change_connection(self):
        if self.demo:
            messagebox.showinfo("Demo workspace", "Close the app and launch without --demo to use your real database.", parent=self)
            return
        if self.busy_write or self.fetching:
            self.toast("Finish the current database request first.", error=True)
            return
        if messagebox.askyesno("Change database connection", "Return to the connection screen? Your records remain in the current database.", parent=self):
            self.online = False
            self.show_setup()

    def export_backup(self):
        if not self.has_snapshot:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Export full financial backup", defaultextension=".json",
                                           initialfile=f"finance-backup-{date.today().isoformat()}.json", filetypes=[("JSON backup", "*.json")])
        if not path:
            return
        try:
            value = backup_document(self.snapshot)
            value["snapshot_is_offline"] = not self.online
            atomic_json(Path(path), value)
            self.toast("Backup exported. It contains your financial records, not your database password.")
        except (OSError, ValueError):
            self.toast("The backup could not be saved. Choose a writable location.", error=True)

    def export_csv(self):
        if not self.has_snapshot:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Export activity", defaultextension=".csv",
                                           initialfile=f"finance-activity-{date.today().isoformat()}.csv", filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        def safe(value):
            text = str(value)
            # Spreadsheet formula injection prevention in user-entered text.
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["ID", "Date", "Type", "Description", "Account", "Currency", "Amount", "To account", "Received currency", "Received amount", "Category", "Borrower", "Notes", "Voided"])
                for r in sorted(self.snapshot.records, key=lambda rec: (rec.day, rec.id)):
                    writer.writerow([safe(r.id), r.day, KIND_NAMES[r.kind], safe(r.title), ACCOUNTS[r.account].name, r.currency,
                                     str(r.amount), ACCOUNTS[r.destination].name if r.destination else "", ACCOUNTS[r.destination].currency if r.destination else "",
                                     str(r.received) if r.destination else "", safe(r.category), safe(r.borrower), safe(r.notes), "Yes" if r.voided else "No"])
            self.toast("All activity exported, including voided records. Numeric amounts and currencies are separate columns.")
        except OSError:
            self.toast("The CSV could not be saved. Choose a writable location.", error=True)

    def report_callback_exception(self, exc_type, exc, tb):
        write_diagnostic("Tk callback failed.", exc)
        if not self.closed:
            messagebox.showerror("Interface error", "The interface encountered an error. Refresh before repeating any financial action.\n\nError type: " + exc_type.__name__ + "\nDiagnostic: " + str(LOG_FILE), parent=self)
        if os.environ.get("FINANCE_DEBUG") == "1":
            traceback.print_exception(exc_type, exc, tb)

    def close_app(self):
        if self.busy_write:
            messagebox.showinfo("Save in progress", "A save is still awaiting confirmation. Close the app once it finishes.", parent=self)
            return
        if any(getattr(dialog, "pending", None) for dialog in self.modals):
            if not messagebox.askyesno("Unconfirmed operation", "A save was not confirmed. Check your history after reopening before recording it again. Close anyway?", parent=self):
                return
        self.closed = True
        self.worker.close()
        self.destroy()


# Keep the original public class name for simple existing launch scripts.
FinancialTrackerApp = FinanceApp


def main():
    enable_crash_diagnostics()
    demo = "--demo" in sys.argv
    try:
        if not demo and psycopg2 is None:
            text = ("Finance requires psycopg2, as your original app did.\n\n"
                    "Install it with:\npython3 -m pip install psycopg2-binary\n\n"
                    "For a database-free preview:\npython3 financial_tracker.py --demo\n\n"
                    "Diagnostic: " + str(LOG_FILE))
            write_diagnostic("psycopg2 is unavailable in this Python environment.")
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Missing original dependency", text, parent=root)
                root.destroy()
            except Exception:
                native_fatal_alert("Finance could not start", text)
            return 1
        app = FinanceApp(demo=demo)
        app.mainloop()
        return 0
    except BaseException as exc:
        write_diagnostic("Fatal startup/runtime error.", exc)
        native_fatal_alert("Finance could not start",
                           "A startup error was recorded. Open this file for the exact cause:\n" + str(LOG_FILE))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
