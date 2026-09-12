#!/usr/bin/env python3
"""Finance Tracker 2.1 - a single-file, dark desktop ledger for macOS/Windows.

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

No money is moved by this application: it records transactions you already made.
"""
from __future__ import annotations

import base64
import copy
import csv
import ctypes
import hashlib
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

try:
    import psycopg2
    from psycopg2.extras import Json
    from psycopg2.extensions import parse_dsn
except ImportError:
    psycopg2 = None
    Json = None
    parse_dsn = None

APP_VERSION = "2.1.0"
APP_NAME = "Finance"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("999999999999.99")
CONFIG_FILE = Path.home() / "finance_tracker_db_config.json"
DATA_DIR = Path.home() / ".finance_tracker"
CURRENCIES = ("USD", "EUR", "DZD")
KINDS = ("income", "expense", "transfer", "savings_deposit", "savings_withdraw",
         "loan_out", "loan_repaid", "opening", "adjustment")
CATEGORIES = ("Essentials", "Business", "Food", "Transport", "Shopping", "Debt", "Other")
# Keep the original setting keys. Rates are display estimates, not ledger money.
DISPLAY_RATE_KEYS = {"USD": "display_rate", "EUR": "display_rate_eur"}
RATE_UNIT = Decimal("0.000001")
MAX_DISPLAY_RATE = Decimal("1000000")


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
    "usd_bank": Account("usd_bank", "USD bank", "USD", "Bank", "#B4A7D6"),
    "eur_bank": Account("eur_bank", "EUR bank", "EUR", "Bank", "#9EB4CA"),
    "eur_cash": Account("eur_cash", "EUR cash", "EUR", "Cash", "#C4B291"),
    "dzd_cash": Account("dzd_cash", "DZD cash", "DZD", "Cash", "#9DB8A8"),
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
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        find = sec.SecKeychainFindGenericPassword
        find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                         ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        find.restype = ctypes.c_int32
        sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecKeychainItemFreeContent.restype = ctypes.c_int32
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        size, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = find(None, len(cls.SERVICE), cls.SERVICE, len(cls.ACCOUNT), cls.ACCOUNT,
                      ctypes.byref(size), ctypes.byref(data), ctypes.byref(item))
        try:
            if password is None:
                if status != 0:
                    raise OSError("The database credential could not be read from Keychain.")
                return ctypes.string_at(data, size.value).decode("utf-8")
            encoded = password.encode("utf-8")
            if status == 0:
                modify = sec.SecKeychainItemModifyAttributesAndData
                modify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
                modify.restype = ctypes.c_int32
                result = modify(item, None, len(encoded), encoded)
            elif status == -25300:  # errSecItemNotFound
                add = sec.SecKeychainAddGenericPassword
                add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                                ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                                ctypes.c_char_p, ctypes.c_void_p]
                add.restype = ctypes.c_int32
                result = add(None, len(cls.SERVICE), cls.SERVICE, len(cls.ACCOUNT), cls.ACCOUNT,
                             len(encoded), encoded, None)
            else:
                raise OSError("Keychain access was not granted.")
            if result != 0:
                raise OSError("The database credential could not be stored in Keychain.")
            return password
        finally:
            if data.value:
                sec.SecKeychainItemFreeContent(None, data)
            if item.value:
                cf.CFRelease(item)

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
                    records, settings = self._read(cur)
            snapshot = project(records, settings)
            # A baseline backup is REQUIRED before this client is allowed to write.
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = self.folder / "backups" / f"before-v2-{stamp}-{uuid.uuid4().hex[:6]}.json"
            atomic_json(backup_path, backup_document(snapshot))
            self.backup_path = backup_path
            self._cache(snapshot)
            return snapshot
        finally:
            connection.close()

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
            snapshot = project(records, settings)
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
        self.lock = threading.Lock()
        self.parameters = {"host": "DEMO - no database", "dbname": "Sample records", "sslmode": "not applicable"}
        self.backup_path = None
        self.storage_warning = ""
        self.identity = "demo"

    def initialize(self) -> Snapshot:
        return self.fetch()

    def fetch(self) -> Snapshot:
        with self.lock:
            return project(self.records, self.settings)

    def write(self, command: Command) -> Snapshot:
        with self.lock:
            if command.operation_id in self.operations:
                if self.operations[command.operation_id] != command.digest:
                    raise ConflictError("Operation ID reused with different data.")
                return project(self.records, self.settings)
            if command.action == "display_rates":
                self.settings = apply_display_rates(self.settings, command)
            else:
                self.records, _ = apply_command(self.records, command)
            self.operations[command.operation_id] = command.digest
            return project(self.records, self.settings)


def demo_records() -> List[Dict[str, Any]]:
    today = date.today()
    month = today.replace(day=1).isoformat()
    rows: List[Dict[str, Any]] = []
    def add(kind, **kw):
        kw.setdefault("date", month)
        item = record_payload(kind, kw)
        rows.append(item)
        return item["id"]
    add("opening", account="usd_bank", amount="4200", description="Starting USD balance")
    add("opening", account="eur_bank", amount="1950", description="Starting EUR balance")
    add("opening", account="dzd_cash", amount="185000", description="Starting dinar balance")
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
# Dark, standard-library UI. No images, web views, icon packages or asset files.
# ---------------------------------------------------------------------------
P = {"bg": "#111318", "sidebar": "#0C0E12", "card": "#1A1D24", "field": "#13161C",
     "border": "#2A2E38", "hover": "#242832", "text": "#EDEEF2", "muted": "#979EAD",
     "dim": "#858E9E", "accent": "#C2B3E3", "accent_dark": "#A795CC",
     "green": "#A7C9B6", "red": "#D8A2A2", "amber": "#D2BC94", "selection": "#303041"}
# Text colors are independent of alternating dark row backgrounds.
ACTIVITY_COLORS = {
    "income": "#8FD6A3", "expense": "#F2A3A3", "transfer": "#AFC3FF",
    "savings_deposit": "#E5C07B", "savings_withdraw": "#9BD3AE",
    "loan_out": "#F0A0A0", "loan_repaid": "#8FD6A3",
    "opening": "#C6B3F2", "adjustment": "#C6B3F2",
}
FONT_FAMILY = "Arial"


def activity_color(record: Record) -> str:
    return P["dim"] if record.voided else ACTIVITY_COLORS.get(record.kind, P["text"])


def activity_tags(record: Record, index: int) -> Tuple[str, ...]:
    # A voided record must never retain its active transaction-type foreground.
    stripe = ("alternate",) if index % 2 else ()
    return stripe + (("voided",) if record.voided else (record.kind,))


def font(size: int = 14, bold: bool = False) -> Tuple[str, int, str]:
    return FONT_FAMILY, -size, "bold" if bold else "normal"


def label(parent, text="", size=14, color=None, bold=False, bg=None, **kwargs):
    kwargs.setdefault("anchor", "w")
    return tk.Label(parent, text=text, font=font(size, bold), fg=color or P["text"],
                    bg=bg or parent.cget("bg"), bd=0, **kwargs)


def card(parent, **kwargs):
    return tk.Frame(parent, bg=P["card"], highlightbackground=P["border"], highlightthickness=1, bd=0, **kwargs)


def line(parent, pady=16):
    tk.Frame(parent, bg=P["border"], height=1).pack(fill="x", pady=pady)


class Button(tk.Canvas):
    def __init__(self, parent, text, command=None, *, variant="primary", width=None, height=40, small=False):
        self.text, self.command, self.variant = text, command, variant
        self.enabled = True
        self.hover = False
        self.focused = False
        self.font_spec = font(12 if small else 13, True)
        calculated = tkfont.Font(font=self.font_spec).measure(text) + 30
        super().__init__(parent, width=width or calculated, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
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
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def _draw(self, event=None):
        if not self.winfo_exists():
            return
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        if self.variant == "primary":
            fill = P["accent_dark"] if self.hover else P["accent"]
            ink, edge = P["sidebar"], fill
        elif self.variant == "danger":
            fill, ink, edge = ("#39282D" if self.hover else "#2B2228"), P["red"], "#523A43"
        else:
            fill, ink, edge = (P["hover"] if self.hover else P["card"]), P["text"], P["border"]
        if not self.enabled:
            fill, ink, edge = P["card"], P["dim"], P["border"]
        if self.focused and self.enabled:
            edge = P["text"]
        self.delete("all")
        r = min(9, h / 3)
        points = [r, 1, w-r, 1, w-1, 1, w-1, r, w-1, h-r,
                  w-1, h-1, w-r, h-1, r, h-1, 1, h-1, 1, h-r, 1, r, 1, 1]
        self.create_polygon(points, smooth=True, splinesteps=20, fill=fill, outline=edge, width=1)
        self.create_text(w/2, h/2, text=self.text, fill=ink, font=self.font_spec)


class Field(tk.Frame):
    def __init__(self, parent, title, value="", *, choices=None, hint="", secret=False, large=False):
        super().__init__(parent, bg=parent.cget("bg"))
        self.title_label = label(self, title, 12, P["muted"], True)
        self.title_label.pack(fill="x", pady=(0, 8))
        self.var = tk.StringVar(value=value)
        self.is_combo = choices is not None
        if choices is not None:
            self.input = ttk.Combobox(self, textvariable=self.var, values=list(choices), state="readonly",
                                      style="Finance.TCombobox", font=font(14))
            self.input.pack(fill="x", ipady=7)
        else:
            box = tk.Frame(self, bg=P["field"], highlightthickness=1, highlightbackground=P["border"])
            box.pack(fill="x")
            self.input = tk.Entry(box, textvariable=self.var, font=font(22 if large else 14),
                                  bg=P["field"], fg=P["text"], insertbackground=P["text"],
                                  readonlybackground=P["field"], disabledbackground=P["field"],
                                  disabledforeground=P["dim"], relief="flat", bd=0, highlightthickness=0,
                                  show="\u2022" if secret else "", selectbackground=P["selection"])
            self.input.pack(fill="x", padx=12, pady=11 if large else 10)
            self.input.bind("<FocusIn>", lambda e: box.configure(highlightbackground=P["accent"]))
            self.input.bind("<FocusOut>", lambda e: box.configure(highlightbackground=P["border"]))
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
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Finance.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.body = tk.Frame(self.canvas, bg=self.cget("bg"))
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._content)
        self.canvas.bind("<Configure>", self._resize)

    def _content(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def wheel(self, event):
        if self.body.winfo_height() <= self.canvas.winfo_height():
            return
        if getattr(event, "num", 0) in (4, 5):
            units = -3 if event.num == 4 else 3
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) * 3 if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")


def draw_icon(canvas, name, color, ox=0, oy=0):
    def l(points):
        coords = []
        for i, v in enumerate(points):
            coords.append(v + (ox if i % 2 == 0 else oy))
        canvas.create_line(*coords, fill=color, width=1.5, capstyle="round", joinstyle="round")
    def r(x, y, a, b):
        canvas.create_rectangle(x+ox, y+oy, a+ox, b+oy, outline=color, width=1.3)
    if name == "overview":
        for x, y in [(3, 3), (13, 3), (3, 13), (13, 13)]:
            r(x, y, x+6, y+6)
    elif name in ("income", "expenses"):
        if name == "income":
            l([11, 3, 11, 20]); l([5, 14, 11, 20, 17, 14])
        else:
            l([11, 20, 11, 3]); l([5, 9, 11, 3, 17, 9])
    elif name == "transfers":
        l([3, 7, 20, 7, 16, 3]); l([20, 16, 3, 16, 7, 20])
    elif name == "savings":
        l([4, 6, 11, 3, 19, 6, 19, 12, 16, 18, 11, 21, 6, 18, 4, 12, 4, 6])
        l([8, 11, 10, 14, 15, 9])
    elif name == "lending":
        canvas.create_oval(6+ox, 2+oy, 15+ox, 11+oy, outline=color, width=1.4)
        l([3, 21, 3, 18, 6, 15, 15, 15, 19, 18, 19, 21])
    elif name == "settings":
        for y, x in [(5, 8), (11, 16), (18, 7)]:
            l([3, y, 20, y])
            r(x-2, y-2, x+2, y+2)
    else:
        for y in (5, 11, 18):
            l([7, y, 21, y]); l([2, y, 3, y])


class Modal(tk.Toplevel):
    def __init__(self, app, title, subtitle="", width=660, height=660):
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
        self.minsize(460, 450)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=P["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(26, 18))
        label(header, title, 25, bold=True).pack(fill="x")
        if subtitle:
            label(header, subtitle, 12, P["muted"], wraplength=width-70, justify="left").pack(fill="x", pady=(8, 0))
        self.scroll = ScrollArea(self)
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=(26, 14))
        self.body = self.scroll.body
        self.footer = tk.Frame(self, bg=P["bg"])
        self.footer.grid(row=2, column=0, sticky="ew", padx=28, pady=(12, 24))
        self.error = label(self.footer, "", 12, P["red"], wraplength=width-70, justify="left")
        self.error.pack(fill="x", pady=(0, 12))
        self.buttons = tk.Frame(self.footer, bg=P["bg"])
        self.buttons.pack(fill="x")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.after(20, self._show)
        app.modals.add(self)

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
            if not messagebox.askyesno("Unconfirmed save", "The last save was not confirmed. Refresh the history before entering it again. Close this form?", parent=self):
                return
        self.app.modals.discard(self)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


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
            names = [a.name for a in ACCOUNTS.values() if not loan or a.currency == loan.currency]
            self.add_field("account", "Account", ACCOUNTS[chosen].name, choices=names)
            self.fields["account"].input.bind("<<ComboboxSelected>>", self.update_preview)
            amount = str(record.amount) if record else str(app.snapshot.outstanding.get(loan.id, ZERO)) if loan else ""
            self.add_field("amount", "Amount", amount, large=True)
            self.fields["amount"].var.trace_add("write", self.update_preview)
            if kind == "expense":
                category = record.category if record and record.category else "Other"
                # The old application concatenated category and description.
                if record and record.legacy and " - " in category:
                    category = category.split(" - ", 1)[0]
                options = list(CATEGORIES)
                if category not in options:
                    options.append(category)
                self.add_field("category", "Category", category, choices=options)
            self.balance_hint = label(self.body, "", 12, P["muted"], wraplength=570, justify="left")
            self.balance_hint.pack(fill="x", pady=(0, 12))
            if kind in ("savings_deposit", "savings_withdraw", "loan_repaid"):
                Button(self.body, "Use remaining amount" if loan else "Use available amount", self.fill_max, variant="secondary", small=True, height=32).pack(anchor="w", pady=(0, 14))
        row = tk.Frame(self.body, bg=P["bg"])
        row.pack(fill="x", pady=(0, 14))
        self.fields["date"] = Field(row, "Date (YYYY-MM-DD)", record.day if record else date.today().isoformat())
        self.fields["date"].pack(fill="x")
        if kind == "transfer":
            self.add_field("description", "Description (optional)", record.description if record else "")
        self.add_field("notes", "Notes (optional)", record.notes if record else "")
        self.cancel_button = Button(self.buttons, "Cancel", self.close, variant="secondary")
        self.cancel_button.pack(side="left")
        self.save_button = Button(self.buttons, "Save changes" if record else "Save transaction", self.save, width=172)
        self.save_button.pack(side="right")
        self.update_preview()
        self.bind("<Control-Return>", lambda e: self.save())
        if sys.platform == "darwin":
            self.bind("<Command-Return>", lambda e: self.save())
        first = next(iter(self.fields.values()))
        self.after(80, first.input.focus_set)

    def add_field(self, key, title, value="", **kwargs):
        field_ = Field(self.body, title, value, **kwargs)
        field_.pack(fill="x", pady=(0, 15))
        self.fields[key] = field_
        return field_

    def _build_transfer(self, record, chosen):
        panel = tk.Frame(self.body, bg=P["bg"])
        panel.pack(fill="x", pady=(0, 14))
        panel.grid_columnconfigure((0, 1), weight=1, uniform="transfer")
        source = chosen
        dest = record.destination if record else ("eur_cash" if chosen == "dzd_cash" else "dzd_cash")
        for col, prefix, name, account_id, amount in [
            (0, "from", "YOU SEND", source, str(record.amount) if record else ""),
            (1, "to", "YOU RECEIVE", dest, str(record.received) if record else "")]:
            box = card(panel)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 7) if col == 0 else (7, 0))
            inside = tk.Frame(box, bg=P["card"])
            inside.pack(fill="both", expand=True, padx=16, pady=18)
            label(inside, name, 10, P["accent"] if col == 0 else P["green"], True).pack(fill="x", pady=(0, 16))
            field_ = Field(inside, "From account" if col == 0 else "To account", ACCOUNTS[account_id].name,
                           choices=[a.name for a in ACCOUNTS.values()])
            field_.pack(fill="x", pady=(0, 18))
            self.fields[prefix + "_account"] = field_
            field_.input.bind("<<ComboboxSelected>>", self.on_accounts)
            key = "amount_sent" if col == 0 else "amount_received"
            self.fields[key] = Field(inside, "Amount (" + ACCOUNTS[account_id].currency + ")", amount, large=True)
            self.fields[key].pack(fill="x")
            self.fields[key].var.trace_add("write", self.update_preview)
        controls = tk.Frame(self.body, bg=P["bg"])
        controls.pack(fill="x", pady=(0, 14))
        self.max_button = Button(controls, "Send available balance", self.fill_max, variant="secondary", small=True, height=32)
        self.max_button.pack(side="left")
        self.swap_button = Button(controls, "Swap direction", self.swap, variant="secondary", small=True, height=32)
        self.swap_button.pack(side="right")
        rate_card = card(self.body)
        rate_card.pack(fill="x", pady=(0, 16))
        label(rate_card, "EFFECTIVE RATE  /  CALCULATED", 10, P["dim"], True).pack(fill="x", padx=16, pady=(14, 5))
        self.rate_label = label(rate_card, "Enter both amounts", 18, P["text"], True, wraplength=530, justify="left")
        self.rate_label.pack(fill="x", padx=16)
        self.balance_hint = label(rate_card, "", 12, P["muted"], wraplength=530, justify="left")
        self.balance_hint.pack(fill="x", padx=16, pady=(8, 14))
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
            self.balance_hint.configure(text=f"{ACCOUNTS[src].name}: {fmt(current, ACCOUNTS[src].currency)} {'saved' if self.kind == 'savings_withdraw' else 'available'}{' before this record' if self.record else ''}.", fg=P["muted"])
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
                    self.balance_hint.configure(text=f"After {'changes' if self.record else 'transfer'}: {ACCOUNTS[src].name} {fmt(current-sent, ACCOUNTS[src].currency)}\n{ACCOUNTS[dst].name} {fmt(available[dst]+received, ACCOUNTS[dst].currency)}",
                                                fg=P["red"] if sent > current else P["muted"])
                except ValidationError:
                    self.rate_label.configure(text="Enter both amounts")
            elif self.loan:
                self.balance_hint.configure(text=f"Still owed by {self.loan.borrower}: {fmt(self.app.snapshot.outstanding.get(self.loan.id, ZERO), self.loan.currency)}. Repayment goes to {ACCOUNTS[src].name}.")
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
        label(summary, record.amount_text, 22, activity_color(record), True,
              wraplength=550, justify="left").pack(fill="x", padx=20, pady=(0, 18))
        fields = [("Description", record.title), ("Date", record.day), ("Account", record.route)]
        if record.kind == "transfer":
            fields.append(("Calculated rate", rate_text(record.account, record.destination, record.amount, record.received)))
        if record.kind == "loan_out":
            fields.append(("Remaining", fmt(app.snapshot.outstanding.get(record.id, ZERO), record.currency)))
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


class FinanceApp(tk.Tk):
    PAGES = [("overview", "Overview"), ("income", "Income"), ("expenses", "Expenses"),
             ("transfers", "Transfers"), ("savings", "Savings"), ("lending", "Lending"),
             ("activity", "All activity"), ("settings", "Settings")]

    def __init__(self, demo=False):
        super().__init__()
        global FONT_FAMILY
        families = set(tkfont.families(self))
        preferences = ("SF Pro Text", "Helvetica Neue", "Arial") if sys.platform == "darwin" else ("Segoe UI", "DejaVu Sans", "Arial")
        FONT_FAMILY = next((name for name in preferences if name in families), "TkDefaultFont")
        self.title("Finance" + (" - DEMO" if demo else ""))
        self.configure(bg=P["bg"])
        width = min(1440, self.winfo_screenwidth() - 60)
        height = min(930, self.winfo_screenheight() - 85)
        self.geometry(f"{width}x{height}+30+30")
        self.minsize(min(980, width), min(640, height))
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
        self.bind("<Control-r>", lambda e: self.refresh())
        self.bind("<Control-n>", lambda e: self.open_form("income"))
        if sys.platform == "darwin":
            self.bind("<Command-r>", lambda e: self.refresh())
            self.bind("<Command-n>", lambda e: self.open_form("income"))
        self.bind("<FocusIn>", self._on_focus, add="+")
        self.after(60, self._drain)
        self.show_connecting()
        self.after(80, self.startup)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Finance.Treeview", background=P["card"], foreground=P["text"],
                        fieldbackground=P["card"], borderwidth=0, relief="flat", rowheight=54, font=font(13))
        style.configure("Finance.Treeview.Heading", background=P["card"], foreground=P["dim"],
                        font=font(10, True), relief="flat", borderwidth=0, padding=(8, 14))
        style.map("Finance.Treeview", background=[("selected", P["selection"])], foreground=[("selected", P["text"])])
        style.map("Finance.Treeview.Heading", background=[("active", P["hover"])])
        style.layout("Finance.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Finance.TCombobox", fieldbackground=P["field"], background=P["field"],
                        foreground=P["text"], arrowcolor=P["muted"], bordercolor=P["border"],
                        lightcolor=P["field"], darkcolor=P["field"], padding=(10, 4), insertcolor=P["text"])
        style.map("Finance.TCombobox", fieldbackground=[("readonly", P["field"]), ("disabled", P["field"])],
                  foreground=[("readonly", P["text"]), ("disabled", P["dim"])],
                  selectbackground=[("readonly", P["field"])], selectforeground=[("readonly", P["text"])],
                  background=[("active", P["hover"]), ("readonly", P["field"])])
        style.configure("Finance.Vertical.TScrollbar", troughcolor=P["bg"], background=P["border"],
                        darkcolor=P["border"], lightcolor=P["border"], bordercolor=P["bg"],
                        arrowcolor=P["muted"], gripcount=0, width=10)
        style.map("Finance.Vertical.TScrollbar",
                  background=[("disabled", P["border"]), ("active", P["dim"]), ("!active", P["border"])],
                  troughcolor=[("disabled", P["bg"]), ("!disabled", P["bg"])],
                  arrowcolor=[("disabled", P["dim"]), ("!disabled", P["muted"])])
        self.option_add("*TCombobox*Listbox.background", P["field"])
        self.option_add("*TCombobox*Listbox.foreground", P["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", P["selection"])
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
            except Exception:
                messagebox.showerror("Interface error", "The interface could not display the result. Refresh before repeating any financial operation.", parent=self)
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
        area.place(relx=.5, rely=.45, anchor="center")
        label(area, "FINANCE", 12, P["accent"], True).pack()
        label(area, "Your money. Clearly.", 34, bold=True).pack(pady=(18, 10))
        label(area, "Opening your workspace...", 14, P["muted"]).pack()

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
        outer.place(relx=.5, rely=.5, anchor="center", width=min(620, self.winfo_width()-60))
        label(outer, "FINANCE  /  PERSONAL WORKSPACE", 11, P["accent"], True).pack(fill="x", pady=(0, 22))
        label(outer, "Welcome back.", 36, bold=True).pack(fill="x")
        label(outer, "One ledger. Every account.", 18, P["muted"]).pack(fill="x", pady=(10, 30))
        box = card(outer)
        box.pack(fill="x")
        inside = tk.Frame(box, bg=P["card"])
        inside.pack(fill="x", padx=26, pady=26)
        label(inside, "Connect your database", 20, bold=True).pack(fill="x", pady=(0, 8))
        label(inside, "Use the same Neon / PostgreSQL URL as your original app.\nYour existing records will be read, not replaced.", 13, P["muted"], justify="left", wraplength=520).pack(fill="x", pady=(0, 24))
        self.url_field = Field(inside, "Database connection URL", secret=True)
        self.url_field.pack(fill="x")
        self.remember_var = tk.BooleanVar(value=True)
        tk.Checkbutton(inside, text="Remember securely on this computer", variable=self.remember_var,
                       font=font(12), bg=P["card"], fg=P["muted"], activebackground=P["card"],
                       activeforeground=P["text"], selectcolor=P["field"], relief="flat", bd=0,
                       highlightthickness=0).pack(anchor="w", pady=(15, 4))
        self.setup_error = label(inside, error_text, 12, P["red"], wraplength=520, justify="left")
        self.setup_error.pack(fill="x", pady=(8, 16))
        self.connect_button = Button(inside, "Connect workspace", lambda: self.connect(self.url_field.get(), self.remember_var.get()), width=200)
        self.connect_button.pack(anchor="w")
        label(outer, "No new account. No web hosting. The database keeps your Mac and Windows records in sync.", 12, P["dim"], wraplength=570, justify="left").pack(fill="x", pady=(18, 0))
        self.url_field.input.bind("<Return>", lambda e: self.connect(self.url_field.get(), self.remember_var.get()))
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
        self.sidebar = tk.Frame(self, bg=P["sidebar"], width=214)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        logo = tk.Frame(self.sidebar, bg=P["sidebar"])
        logo.pack(fill="x", padx=24, pady=(24, 6))
        mark = tk.Canvas(logo, width=28, height=27, bg=P["sidebar"], highlightthickness=0)
        mark.pack(side="left", padx=(0, 10))
        mark.create_rectangle(2, 3, 11, 12, fill=P["accent"], outline="")
        mark.create_rectangle(14, 3, 23, 12, fill="#766B8E", outline="")
        mark.create_rectangle(2, 15, 11, 24, fill="#766B8E", outline="")
        mark.create_rectangle(14, 15, 23, 24, fill=P["accent"], outline="")
        label(logo, "Finance", 23, bold=True).pack(side="left")
        label(self.sidebar, "PERSONAL WORKSPACE", 9, P["dim"], True).pack(anchor="w", padx=24, pady=(4, 26))
        self.nav = {}
        for page, name in self.PAGES:
            if page == "activity":
                tk.Frame(self.sidebar, bg=P["border"], height=1).pack(fill="x", padx=24, pady=(12, 8))
            row = tk.Frame(self.sidebar, bg=P["sidebar"], cursor="hand2", height=40)
            row.pack(fill="x", padx=12, pady=2)
            row.pack_propagate(False)
            icon = tk.Canvas(row, bg=P["sidebar"], width=25, height=25, highlightthickness=0)
            icon.pack(side="left", padx=(14, 13))
            draw_icon(icon, page, P["muted"])
            title = label(row, name, 13, P["muted"])
            title.pack(side="left")
            for widget in (row, icon, title):
                widget.bind("<Button-1>", lambda e, p=page: self.navigate(p))
                widget.bind("<Enter>", lambda e, p=page: self.nav_hover(p, True))
                widget.bind("<Leave>", lambda e, p=page: self.nav_hover(p, False))
            self.nav[page] = (row, icon, title)
        bottom = tk.Frame(self.sidebar, bg=P["sidebar"])
        bottom.pack(side="bottom", fill="x", padx=22, pady=20)
        self.sync_title = label(bottom, "", 12, P["green"], True)
        self.sync_title.pack(fill="x")
        self.sync_detail = label(bottom, "", 11, P["dim"], wraplength=170, justify="left")
        self.sync_detail.pack(fill="x", pady=(6, 15))
        Button(bottom, "Refresh", self.refresh, variant="secondary", width=166, height=35, small=True).pack(fill="x")
        label(bottom, "v" + APP_VERSION + ("  /  DEMO" if self.demo else "  /  Desktop"), 10, P["dim"]).pack(fill="x", pady=(18, 0))
        self.main = tk.Frame(self, bg=P["bg"])
        self.main.pack(side="left", fill="both", expand=True)
        self.toast_label = label(self.main, "", 12, P["green"], wraplength=900, justify="left")
        self.toast_label.pack(side="bottom", fill="x", padx=30, pady=(0, 9))
        self.content = tk.Frame(self.main, bg=P["bg"])
        self.content.pack(fill="both", expand=True)
        self.render()
        self.update_status()

    def nav_hover(self, page, hovered):
        if page == self.current_page:
            return
        row, icon, title = self.nav[page]
        background = P["bg"] if hovered else P["sidebar"]
        for widget in (row, icon, title):
            widget.configure(bg=background)

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
            title, color, detail = "Demo workspace", P["amber"], "Sample data only.\nNothing is saved online."
        elif self.busy_write:
            title, color, detail = "Saving...", P["amber"], "Waiting for database confirmation."
        elif self.fetching:
            title, color, detail = "Refreshing...", P["muted"], "Keeping the last loaded balances."
        elif not self.online:
            title, color, detail = "Offline / read only", P["red"], "Showing cached data.\nRefresh to reconnect."
        elif not self.snapshot.valid:
            title, color, detail = "Review required", P["amber"], "Open Settings for data checks."
        else:
            title, color = "Connected", P["green"]
            try:
                stamp = datetime.fromisoformat(self.snapshot.fetched_at).astimezone().strftime("%H:%M:%S")
            except ValueError:
                stamp = "earlier"
            detail = "Last synced " + stamp
        self.sync_title.configure(text=title, fg=color)
        self.sync_detail.configure(text=detail)

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
        self._table_context = None
        for widget in self.content.winfo_children():
            widget.destroy()
        for page, (row, icon, title) in self.nav.items():
            active = page == self.current_page
            bg = "#24232E" if active else P["sidebar"]
            color = P["accent"] if active else P["muted"]
            for widget in (row, icon, title):
                widget.configure(bg=bg)
            title.configure(fg=color, font=font(13, active))
            icon.delete("all")
            draw_icon(icon, page, color)
        if self.current_page == "settings":
            self.render_settings()
        else:
            self.render_page()

    def page_header(self, title, subtitle, actions=()):
        header = tk.Frame(self.content, bg=P["bg"])
        header.pack(fill="x", padx=30, pady=(30, 23))
        right = tk.Frame(header, bg=P["bg"])
        right.pack(side="right", anchor="n", pady=(5, 0))
        for text, command, variant in actions:
            button = Button(right, text, command, variant=variant)
            button.pack(side="left", padx=(10, 0))
        left = tk.Frame(header, bg=P["bg"])
        left.pack(side="left", fill="x", expand=True)
        label(left, title, 30, bold=True).pack(fill="x")
        label(left, subtitle, 12, P["muted"], wraplength=550, justify="left").pack(fill="x", pady=(8, 0))

    def page_body(self):
        scroll = ScrollArea(self.content)
        scroll.pack(fill="both", expand=True, padx=(30, 15))
        body = tk.Frame(scroll.body, bg=P["bg"])
        body.pack(fill="x", padx=(0, 14), pady=(0, 20))
        self.page_scroll = scroll
        return body

    def notice(self, parent, text, color=None):
        box = card(parent)
        box.pack(fill="x", pady=(0, 18))
        label(box, text, 12, color or P["amber"], wraplength=930, justify="left").pack(fill="x", padx=17, pady=14)
        box.bind("<Configure>", lambda e: [child.configure(wraplength=max(240, e.width-40)) for child in box.winfo_children() if isinstance(child, tk.Label)])

    def month_bar(self, parent, title="Monthly overview"):
        row = tk.Frame(parent, bg=P["bg"])
        row.pack(fill="x", pady=(5, 16))
        label(row, title, 17, bold=True).pack(side="left")
        chooser = tk.Frame(row, bg=P["bg"])
        chooser.pack(side="right")
        Button(chooser, "<", lambda: self.change_month(-1), variant="secondary", width=32, height=30, small=True).pack(side="left")
        current = datetime.strptime(self.selected_month, "%Y-%m")
        label(chooser, current.strftime("%B %Y"), 12, P["muted"], width=17, anchor="center").pack(side="left", padx=5)
        Button(chooser, ">", lambda: self.change_month(1), variant="secondary", width=32, height=30, small=True).pack(side="left")
        Button(chooser, "Today", self.today, variant="secondary", height=30, small=True).pack(side="left", padx=(8, 0))

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
        box = card(parent)
        box.pack(fill="x", pady=(0, 22))
        inner = tk.Frame(box, bg=P["card"])
        inner.pack(fill="x", padx=22, pady=18)
        top = tk.Frame(inner, bg=P["card"])
        top.pack(fill="x")
        label(top, "TOTAL IN DINARS", 11, P["accent"], True).pack(side="left")
        Button(top, "Display rates", self.open_display_rates, variant="secondary",
               height=30, small=True).pack(side="right")
        total = total_in_dinars(self.snapshot)
        rates = display_rates(self.snapshot.settings)
        if not self.snapshot.valid:
            text = "Unavailable"
        elif total is None:
            text = "Set display rates"
        else:
            text = "\u2248 " + fmt(total, "DZD")
        self.dinar_total_label = label(inner, text, 34 if len(text) <= 25 else 26,
                                       P["red"] if total is not None and total < ZERO else P["text"], True)
        self.dinar_total_label.pack(fill="x", pady=(4, 8))
        note = label(inner, "Bank + cash + savings. Unpaid loans are excluded. Current balances, not just this month.",
                     12, P["muted"], wraplength=880, justify="left")
        note.pack(fill="x")
        quotes = "    /    ".join(f"1 {currency} = {compact_rate(rates[currency])} DZD" if rates[currency] is not None
                                  else f"{currency} rate not set" for currency in DISPLAY_RATE_KEYS)
        self.dinar_rates_label = label(inner, quotes + "    /    Display estimate only", 11, P["dim"],
                                       wraplength=880, justify="left")
        self.dinar_rates_label.pack(fill="x", pady=(8, 0))
        def wrap(event):
            width = max(220, event.width)
            note.configure(wraplength=width)
            self.dinar_rates_label.configure(wraplength=width)
        inner.bind("<Configure>", wrap)

    def account_cards(self, parent, mode="available"):
        grid = tk.Frame(parent, bg=P["bg"])
        grid.pack(fill="x", pady=(0, 22))
        widgets = []
        for key, account in ACCOUNTS.items():
            box = card(grid)
            inside = tk.Frame(box, bg=P["card"])
            inside.pack(fill="both", expand=True, padx=20, pady=19)
            top = tk.Frame(inside, bg=P["card"])
            top.pack(fill="x", pady=(0, 19))
            label(top, account.currency, 11, account.accent, True).pack(side="left")
            label(top, account.location.upper(), 9, P["dim"], True).pack(side="right")
            value = (self.snapshot.saved if mode == "saved" else self.snapshot.available)[key]
            text = fmt(value, account.currency) if self.snapshot.valid else "Unavailable"
            size = 27 if len(text) < 15 else 22
            label(inside, text, size, P["red"] if value < ZERO else P["text"], True).pack(fill="x")
            if mode == "saved":
                note = f"{fmt(self.snapshot.available[key], account.currency)} available"
            else:
                note = "Available" + ("  /  " + fmt(self.snapshot.saved[key], account.currency) + " saved" if self.snapshot.saved[key] else " balance")
            label(inside, note if self.snapshot.valid else "Review data checks", 10, P["dim"], wraplength=220, justify="left").pack(fill="x", pady=(12, 0))
            widgets.append(box)
        last_cols = [0]
        def arrange(event):
            columns = 4 if event.width >= 940 else 2
            if columns == last_cols[0]:
                return
            last_cols[0] = columns
            for c in range(4):
                grid.grid_columnconfigure(c, weight=1 if c < columns else 0, uniform="accounts" if c < columns else "")
            for i, box in enumerate(widgets):
                box.grid(row=i//columns, column=i % columns, sticky="nsew", padx=(0 if i % columns == 0 else 6, 0 if i % columns == columns-1 else 6), pady=(0, 12 if columns == 2 and i < 2 else 0))
        grid.bind("<Configure>", arrange)
        # Initial geometry must be established before its first Configure event.
        for i, box in enumerate(widgets):
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
            grid.grid_columnconfigure(i, weight=1, uniform="accounts")

    def triple_summary(self, parent, title, totals, color=None):
        box = card(parent)
        label(box, title.upper(), 10, P["dim"], True).pack(fill="x", padx=20, pady=(17, 13))
        for currency in CURRENCIES:
            row = tk.Frame(box, bg=P["card"])
            row.pack(fill="x", padx=20, pady=(0, 9))
            label(row, currency, 11, P["muted"]).pack(side="left")
            value = totals[currency]
            label(row, fmt(value, currency) if self.snapshot.valid else "--", 15,
                  P["red"] if value < ZERO else color or P["text"], True).pack(side="right")
        tk.Frame(box, height=7, bg=P["card"]).pack()
        return box

    def render_page(self):
        page = self.current_page
        configurations = {
            "overview": ("Overview", "A clear view of your money, wherever you keep it.",
                         [("+ Income", lambda: self.open_form("income"), "primary"), ("Add expense", lambda: self.open_form("expense"), "secondary")]),
            "income": ("Income", "Record what you actually received. Nothing else to calculate.", [("+ Record income", lambda: self.open_form("income"), "primary")]),
            "expenses": ("Expenses", "Know what left your accounts and where it went.", [("+ Record expense", lambda: self.open_form("expense"), "primary")]),
            "transfers": ("Transfers", "Any currency. Any direction. Bank and cash, kept separate.", [("+ New transfer", lambda: self.open_form("transfer"), "primary")]),
            "savings": ("Savings", "Money set aside, still tracked in its original account.", [("Add to savings", lambda: self.open_form("savings_deposit"), "primary"), ("Release", lambda: self.open_form("savings_withdraw"), "secondary")]),
            "lending": ("Lending", "Keep track of who owes you and every repayment.", [("+ Record loan", lambda: self.open_form("loan_out"), "primary")]),
            "activity": ("All activity", "Search your history. Open any record to review or correct it.", [("Export CSV", self.export_csv, "secondary")])}
        self.page_header(*configurations[page])
        body = self.page_body()
        if self.demo:
            self.notice(body, "DEMO WORKSPACE  /  These are sample transactions. Changes here never touch your database.", P["accent"])
        if not self.online:
            self.notice(body, "OFFLINE SNAPSHOT  /  These balances may be out of date. Refresh to reconnect; new changes are disabled.", P["red"])
        if self.snapshot.errors:
            self.notice(body, "BALANCES UNAVAILABLE  /  " + self.snapshot.errors[0] + "  Open Settings for the full data checks.", P["red"])
        elif self.snapshot.legacy_pending != ZERO and page == "overview":
            self.notice(body, f"UPGRADE NOTE  /  Your old pending USD balance ({fmt(self.snapshot.legacy_pending, 'USD')}) is now included in USD bank. No money was moved. Review that total before using it as a bank balance.")
        if page == "overview":
            self.dinar_total_card(body)
            heading = tk.Frame(body, bg=P["bg"])
            heading.pack(fill="x", pady=(0, 12))
            label(heading, "Your accounts", 17, bold=True).pack(side="left")
            label(heading, "CURRENT AVAILABLE BALANCES", 9, P["dim"], True).pack(side="right")
            self.account_cards(body)
            self.month_bar(body)
            inc = self.snapshot.monthly(self.selected_month, "income")
            exp = self.snapshot.monthly(self.selected_month, "expense")
            net = {c: inc[c]-exp[c] for c in CURRENCIES}
            stats = tk.Frame(body, bg=P["bg"])
            stats.pack(fill="x", pady=(0, 26))
            for i, (name, values, color) in enumerate([("Income", inc, P["green"]), ("Expenses", exp, P["red"]), ("Income minus expenses", net, P["text"])]):
                stats.grid_columnconfigure(i, weight=1, uniform="stats")
                self.triple_summary(stats, name, values, color).grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            self.activity_table(body, allowed=None, selected_month=True, compact=True)
        elif page in ("income", "expenses"):
            self.month_bar(body, "Monthly income" if page == "income" else "Monthly expenses")
            kind = "income" if page == "income" else "expense"
            totals = self.snapshot.monthly(self.selected_month, kind)
            row = tk.Frame(body, bg=P["bg"])
            row.pack(fill="x", pady=(0, 25))
            for i, c in enumerate(CURRENCIES):
                row.grid_columnconfigure(i, weight=1, uniform="currency")
                box = card(row)
                box.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
                label(box, c + "  /  " + ("RECEIVED" if kind == "income" else "SPENT"), 10, P["dim"], True).pack(fill="x", padx=20, pady=(20, 12))
                label(box, fmt(totals[c], c) if self.snapshot.valid else "--", 27, P["green"] if kind == "income" else P["red"], True).pack(fill="x", padx=20, pady=(0, 22))
            self.activity_table(body, allowed={kind}, selected_month=True)
        elif page == "transfers":
            info = card(body)
            info.pack(fill="x", pady=(0, 24))
            inside = tk.Frame(info, bg=P["card"])
            inside.pack(fill="x", padx=22, pady=22)
            label(inside, "You send  \u2192  You receive", 21, bold=True).pack(fill="x")
            label(inside, "USD, EUR and DZD in every direction. Move euros between bank and cash without mixing their balances. The exchange rate is calculated from the actual amounts, not entered manually.", 13, P["muted"], wraplength=870, justify="left").pack(fill="x", pady=(10, 0))
            self.activity_table(body, allowed={"transfer", "adjustment"})
        elif page == "savings":
            self.account_cards(body, mode="saved")
            self.notice(body, "Savings are reserved inside each account, not an extra bank balance. Total held = available + saved. EUR bank savings and EUR cash savings stay separate.", P["muted"])
            self.activity_table(body, allowed={"savings_deposit", "savings_withdraw"})
        elif page == "lending":
            self.render_loans(body)
        else:
            self.activity_table(body, allowed=None, allow_scope=True)

    def activity_table(self, parent, allowed=None, selected_month=False, compact=False, allow_scope=False):
        state = self.filter_states.setdefault(self.current_page, {"query": "", "account": "All accounts", "type": "All types", "scope": "All dates", "page": 0, "voided": False})
        top = tk.Frame(parent, bg=P["bg"])
        top.pack(fill="x", pady=(0, 14))
        label(top, "Recent activity" if compact else "Transaction history", 17, bold=True).pack(side="left")
        if compact:
            Button(top, "View all", lambda: self.navigate("activity"), variant="secondary", height=30, small=True).pack(side="right")
        else:
            filters = tk.Frame(parent, bg=P["bg"])
            filters.pack(fill="x", pady=(0, 14))
            search_var = tk.StringVar(value=state["query"])
            search = tk.Entry(filters, textvariable=search_var, bg=P["field"], fg=P["text"], insertbackground=P["text"],
                              relief="flat", bd=0, highlightthickness=1, highlightbackground=P["border"], font=font(13), width=20)
            search.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 10))
            if not state["query"]:
                label(filters, "Search", 11, P["dim"]).pack(side="left", padx=(0, 10))
            def changed(*args):
                state["query"], state["page"] = search_var.get(), 0
                if self._search_job:
                    self.after_cancel(self._search_job)
                self._search_job = self.after(150, self.fill_table)
            search_var.trace_add("write", changed)
            account_var = tk.StringVar(value=state["account"])
            account_combo = ttk.Combobox(filters, textvariable=account_var, values=["All accounts"] + [a.name for a in ACCOUNTS.values()],
                                         state="readonly", width=14, font=font(12), style="Finance.TCombobox")
            account_combo.pack(side="left", ipady=4)
            def account_changed(e):
                state["account"], state["page"] = account_var.get(), 0
                self.fill_table()
            account_combo.bind("<<ComboboxSelected>>", account_changed)
            if allow_scope:
                scope_var = tk.StringVar(value=state["scope"])
                scope = ttk.Combobox(filters, textvariable=scope_var, values=["All dates", "Selected month"], state="readonly",
                                     width=14, font=font(12), style="Finance.TCombobox")
                scope.pack(side="left", padx=(10, 0), ipady=4)
                def scope_changed(e):
                    state["scope"], state["page"] = scope_var.get(), 0
                    self.render()
                scope.bind("<<ComboboxSelected>>", scope_changed)
                if state["scope"] == "Selected month":
                    self.month_bar(parent, "Activity period")
            visibility = tk.Frame(parent, bg=P["bg"])
            visibility.pack(fill="x", pady=(0, 10))
            label(visibility, "Double-click a row to review, edit or void it.", 11, P["dim"]).pack(side="left")
            void_var = tk.BooleanVar(value=state["voided"])
            def show_void():
                state["voided"], state["page"] = void_var.get(), 0
                self.fill_table()
            tk.Checkbutton(visibility, text="Include voided", variable=void_var, command=show_void,
                           font=font(11), bg=P["bg"], fg=P["dim"], activebackground=P["bg"], activeforeground=P["text"],
                           selectcolor=P["field"], bd=0, highlightthickness=0).pack(side="right")
        panel = card(parent)
        panel.pack(fill="x")
        columns = ("date", "title", "account", "amount")
        tree = ttk.Treeview(panel, columns=columns, show="headings", style="Finance.Treeview", height=5 if compact else 9, selectmode="browse")
        for key, title_, width_, minimum, stretch in [("date", "DATE", 100, 88, False), ("title", "DESCRIPTION", 265, 140, True),
                                                      ("account", "ACCOUNT / ROUTE", 205, 145, True), ("amount", "AMOUNT", 265, 155, True)]:
            tree.heading(key, text=title_, anchor="w" if key != "amount" else "e")
            tree.column(key, width=width_, minwidth=minimum, stretch=stretch, anchor="e" if key == "amount" else "w")
        tree.pack(side="left", fill="both", expand=True, padx=12)
        bar = ttk.Scrollbar(panel, orient="vertical", command=tree.yview, style="Finance.Vertical.TScrollbar")
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        tree.tag_configure("voided", foreground=P["dim"])
        tree.tag_configure("alternate", background="#1C1F27")
        for kind, color in ACTIVITY_COLORS.items():
            tree.tag_configure(kind, foreground=color)
        tree.bind("<Double-1>", lambda e: self.open_selected(tree))
        tree.bind("<Return>", lambda e: self.open_selected(tree))
        footer = tk.Frame(parent, bg=P["bg"])
        footer.pack(fill="x", pady=(12, 8))
        count_label = label(footer, "", 11, P["dim"])
        count_label.pack(side="left")
        if not compact:
            Button(footer, "Next", lambda: self.page_table(1), variant="secondary", height=30, small=True).pack(side="right")
            Button(footer, "Previous", lambda: self.page_table(-1), variant="secondary", height=30, small=True).pack(side="right", padx=(0, 8))
        self._table_context = {"tree": tree, "state": state, "allowed": allowed, "month": selected_month,
                               "compact": compact, "allow_scope": allow_scope, "count": count_label, "page_size": 5 if compact else 50}
        self.fill_table()

    def filtered_records(self, context=None):
        context = context or self._table_context
        if context is None:
            return [r for r in self.snapshot.records if not r.voided]
        state = context["state"]
        query = state["query"].casefold().strip() if not context["compact"] else ""
        account = ACCOUNT_NAMES.get(state["account"])
        records = []
        for r in self.snapshot.records:
            if r.voided and (context["compact"] or not state["voided"]):
                continue
            if context["allowed"] and r.kind not in context["allowed"]:
                continue
            if (context["month"] or (context["allow_scope"] and state["scope"] == "Selected month")) and not r.day.startswith(self.selected_month):
                continue
            if not context["compact"] and account and account not in (r.account, r.destination):
                continue
            haystack = f"{r.title} {r.description} {r.category} {r.notes} {r.borrower} {r.amount} {r.day} {r.route} {r.currency} {KIND_NAMES[r.kind]}".casefold()
            if query and query not in haystack:
                continue
            records.append(r)
        records.sort(key=lambda r: (r.day, str(r.raw.get("created_at", r.raw.get("date", ""))), r.id), reverse=True)
        return records

    def fill_table(self):
        self._search_job = None
        context = self._table_context
        if not context or not context["tree"].winfo_exists():
            return
        tree, state = context["tree"], context["state"]
        rows = self.filtered_records(context)
        size = context["page_size"]
        pages = max(1, (len(rows) + size - 1)//size)
        state["page"] = max(0, min(state["page"], pages-1))
        start = 0 if context["compact"] else state["page"] * size
        tree.delete(*tree.get_children())
        for i, rec in enumerate(rows[start:start+size]):
            title_ = ("[Voided] " if rec.voided else "") + rec.title
            tree.insert("", "end", iid=rec.id, values=(rec.day, title_, rec.route, rec.amount_text),
                        tags=activity_tags(rec, i))
        if not rows:
            context["count"].configure(text="No matching transactions. Add a record or adjust your filters.")
        else:
            context["count"].configure(text=f"Showing {start+1}-{min(start+size, len(rows))} of {len(rows)} records" + ("  /  selected month" if context["month"] else ""))

    def page_table(self, delta):
        if self._table_context:
            self._table_context["state"]["page"] += delta
            self.fill_table()

    def open_selected(self, tree):
        selection = tree.selection()
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
            box.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
            label(box, currency + "  /  OUTSTANDING", 10, P["dim"], True).pack(fill="x", padx=20, pady=(20, 12))
            label(box, fmt(totals[currency], currency) if self.snapshot.valid else "--", 27, P["amber"], True).pack(fill="x", padx=20, pady=(0, 22))
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
            label(right, fmt(remaining, loan.currency), 23, P["amber"] if remaining else P["green"], True).pack(anchor="e")
            label(right, "Remaining" if remaining else "Fully repaid", 11, P["dim"]).pack(anchor="e", pady=(3, 12))
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
        self.page_header("Settings", "A simple workspace, with your data under your control.")
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
        label(accounts, "     ".join(fmt(totals[c], c) for c in CURRENCIES) if self.snapshot.valid else "Unavailable until data checks are resolved.", 20, bold=True).pack(fill="x", pady=(0, 18))
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
        label(body, "FINANCE 2.1  /  BUILT FOR A SINGLE PERSONAL LEDGER", 10, P["dim"], True).pack(fill="x", pady=(10, 12))

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
        if not self.closed:
            messagebox.showerror("Interface error", "The interface encountered an error. Refresh before repeating any financial action.\n\nError type: " + exc_type.__name__, parent=self)
        if os.environ.get("FINANCE_DEBUG") == "1":
            import traceback
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
    demo = "--demo" in sys.argv
    if not demo and psycopg2 is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing original dependency", "Finance requires psycopg2, as your original app did.\n\nInstall it with:\npython -m pip install psycopg2-binary\n\nFor a database-free preview:\npython financial_tracker.py --demo")
        root.destroy()
        return 1
    app = FinanceApp(demo=demo)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
