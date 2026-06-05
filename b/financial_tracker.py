import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import json
import os
import uuid
import sys
import psycopg2
import traceback
from psycopg2.extras import Json
from datetime import datetime

if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

FONT_MAIN = ("Segoe UI", 13)
FONT_BOLD = ("Segoe UI Semibold", 13)
FONT_HEADER = ("Segoe UI Bold", 28)
FONT_SUBHEADER = ("Segoe UI Semibold", 17)
FONT_NUMBERS = ("Segoe UI Bold", 30)
FONT_SMALL = ("Segoe UI", 13)
FONT_TINY = ("Segoe UI", 12)

COLOR_BG = "#0A0A0F"
COLOR_SIDEBAR = "#0E0E14"
COLOR_CARD = "#14141C"
COLOR_INPUT = "#1C1C28"
COLOR_BORDER = "#2A2A3A"
COLOR_PRIMARY = "#6C5CE7"
COLOR_PRIMARY_DIM = "#5A4BD6"
COLOR_HOVER = "#1E1E2A"
COLOR_SUCCESS = "#00D68F"
COLOR_SUCCESS_DIM = "#00B377"
COLOR_DANGER = "#FF6B6B"
COLOR_DANGER_DIM = "#E85555"
COLOR_WARNING = "#FDCB6E"
COLOR_WARNING_DIM = "#E5B85A"
COLOR_SAVINGS = "#A29BFE"
COLOR_SAVINGS_DIM = "#8B83E0"
COLOR_LOAN = "#FF9F43"
COLOR_LOAN_DIM = "#E08C3A"
COLOR_EUR = "#00B4D8"
COLOR_EUR_DIM = "#0096B7"
COLOR_TEXT_MAIN = "#F0F0F5"
COLOR_TEXT_SUB = "#9898AC"
COLOR_TEXT_DIM = "#8585A0"
COLOR_ACCENT_LINE = "#2A2A3A"

DEFAULT_USD_RATE = 200.0
DEFAULT_EUR_RATE = 230.0


class FinancialTrackerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Finance Tracker")
        self.geometry("1420x920")
        self.minsize(1200, 800)
        self.configure(fg_color=COLOR_BG)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.db_url = ""
        self.data = {"settings": {"display_rate": DEFAULT_USD_RATE, "display_rate_eur": DEFAULT_EUR_RATE}, "transactions": []}
        self.config_file = os.path.expanduser("~/finance_tracker_db_config.json")
        self.frames = {}
        self.nav_buttons = {}
        self._db_conn = None
        self._cached_stats = None
        self.after(100, self.check_db_connection)

    # ================================================================
    # DB
    # ================================================================
    def get_db_connection(self):
        try:
            if self._db_conn is None or self._db_conn.closed:
                self._db_conn = psycopg2.connect(self.db_url)
                self._db_conn.autocommit = False
            else:
                self._db_conn.isolation_level
        except Exception:
            try:
                self._db_conn = psycopg2.connect(self.db_url)
                self._db_conn.autocommit = False
            except Exception as e:
                self.show_error_native(f"DB connection lost:\n{e}")
                raise
        return self._db_conn

    def close_db_connection(self):
        if self._db_conn and not self._db_conn.closed:
            try:
                self._db_conn.close()
            except Exception:
                pass
        self._db_conn = None

    def destroy(self):
        self.close_db_connection()
        super().destroy()

    # ================================================================
    # STARTUP
    # ================================================================
    def check_db_connection(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as f:
                    self.db_url = json.load(f).get("db_url", "").strip()
                if self.db_url:
                    c = psycopg2.connect(self.db_url); c.close()
                    self.start_full_app(); return
            self.show_setup_screen()
        except Exception as e:
            messagebox.showerror("Connection Error", f"{e}")
            self.show_setup_screen()

    def show_setup_screen(self):
        for w in self.winfo_children(): w.destroy()
        self.grid_columnconfigure(0, weight=1)
        outer = ctk.CTkFrame(self, fg_color="transparent"); outer.grid(row=0, column=0)
        card = ctk.CTkFrame(outer, fg_color=COLOR_CARD, corner_radius=24, border_width=1, border_color=COLOR_BORDER); card.pack(padx=60, pady=60)
        ctk.CTkFrame(card, fg_color=COLOR_PRIMARY, height=4, corner_radius=2).pack(fill="x", padx=30, pady=(30, 0))
        ctk.CTkLabel(card, text="Connect Database", font=("Segoe UI Bold", 24), text_color=COLOR_TEXT_MAIN).pack(pady=(20, 5))
        ctk.CTkLabel(card, text="Link your Neon PostgreSQL to sync across devices", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(padx=40, pady=(0, 25))
        self.entry_db_url = ctk.CTkEntry(card, width=520, height=50, corner_radius=14, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", placeholder_text="postgresql://user:pass@ep-...neon.tech/neondb", placeholder_text_color=COLOR_TEXT_DIM)
        self.entry_db_url.pack(padx=40, pady=10)
        self.lbl_setup_error = ctk.CTkLabel(card, text="", font=FONT_BOLD, text_color=COLOR_DANGER); self.lbl_setup_error.pack(pady=5)
        ctk.CTkButton(card, text="Connect & Sync", width=220, height=48, corner_radius=14, font=FONT_BOLD, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, command=self.attempt_connection).pack(pady=(10, 35))

    def attempt_connection(self):
        url = self.entry_db_url.get().strip()
        if not url: self.lbl_setup_error.configure(text="Enter a valid URL."); return
        if "sslmode=require" not in url: url += "&sslmode=require" if "?" in url else "?sslmode=require"
        self.lbl_setup_error.configure(text="Connecting...", text_color=COLOR_WARNING); self.update()
        try:
            conn = psycopg2.connect(url)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value FLOAT)")
                    cur.execute("CREATE TABLE IF NOT EXISTS transactions (id VARCHAR(255) PRIMARY KEY, t_date VARCHAR(50), t_type VARCHAR(50), payload JSONB)")
                    cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate', %s) ON CONFLICT DO NOTHING", (DEFAULT_USD_RATE,))
                    cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate_eur', %s) ON CONFLICT DO NOTHING", (DEFAULT_EUR_RATE,))
            conn.close()
            self.db_url = url
            with open(self.config_file, "w") as f: json.dump({"db_url": self.db_url}, f)
            self.start_full_app()
        except Exception as e:
            self.lbl_setup_error.configure(text=f"Failed: {e}", text_color=COLOR_DANGER)

    def start_full_app(self):
        try:
            for w in self.winfo_children(): w.destroy()
            self.grid_columnconfigure(0, weight=0); self.grid_columnconfigure(1, weight=1)
            self.current_date = datetime.now()
            self.selected_month = self.current_date.month
            self.selected_year = self.current_date.year
            self.data = self.fetch_data_from_db()
            self.sidebar_frame = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=COLOR_SIDEBAR)
            self.sidebar_frame.grid(row=0, column=0, sticky="nsew"); self.sidebar_frame.grid_rowconfigure(8, weight=1)
            self.create_sidebar()
            self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            self.main_frame.grid(row=0, column=1, sticky="nsew")
            self.main_frame.grid_columnconfigure(0, weight=1); self.main_frame.grid_rowconfigure(0, weight=1)
            self.create_dashboard_frame(); self.create_income_frame(); self.create_transfer_frame()
            self.create_expenses_frame(); self.create_savings_frame(); self.create_lending_frame()
            self.show_frame("dashboard")
        except Exception as e:
            messagebox.showerror("Fatal", f"{e}\n\n{traceback.format_exc()}")

    # ================================================================
    # DB OPS
    # ================================================================
    def fetch_data_from_db(self):
        data = {"settings": {"display_rate": DEFAULT_USD_RATE, "display_rate_eur": DEFAULT_EUR_RATE}, "transactions": []}
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate_eur', %s) ON CONFLICT DO NOTHING", (DEFAULT_EUR_RATE,))
                conn.commit()
                cur.execute("SELECT key, value FROM settings WHERE key IN ('display_rate', 'display_rate_eur')")
                for row in cur.fetchall():
                    data["settings"][row[0]] = row[1]
                cur.execute("SELECT payload FROM transactions ORDER BY t_date ASC")
                data["transactions"] = [r[0] for r in cur.fetchall()]
            conn.commit()
        except Exception as e:
            self.show_error_native(f"Fetch failed:\n{e}")
        self._cached_stats = None
        return data

    def add_transaction_to_db(self, t):
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO transactions (id, t_date, t_type, payload) VALUES (%s, %s, %s, %s)", (t['id'], t['date'], t['type'], Json(t)))
            conn.commit(); self.data["transactions"].append(t); self._cached_stats = None; self.refresh_ui(); return True
        except psycopg2.IntegrityError:
            self.get_db_connection().rollback(); self.show_error_native("Duplicate."); return False
        except Exception as e:
            try: self.get_db_connection().rollback()
            except: pass
            self.show_error_native(f"Save failed:\n{e}"); return False

    def update_transaction_in_db(self, t):
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("UPDATE transactions SET payload = %s WHERE id = %s", (Json(t), t['id']))
            conn.commit()
            for i, ex in enumerate(self.data["transactions"]):
                if ex.get("id") == t["id"]: self.data["transactions"][i] = t; break
            self._cached_stats = None; self.refresh_ui(); return True
        except Exception as e:
            try: self.get_db_connection().rollback()
            except: pass
            self.show_error_native(f"Update failed:\n{e}"); return False

    def delete_transaction(self, tid):
        d = ctk.CTkToplevel(self); d.title("Confirm"); d.geometry("340x170"); d.configure(fg_color=COLOR_CARD); d.attributes('-topmost', True)
        ctk.CTkLabel(d, text="Delete this transaction?", font=FONT_BOLD, text_color=COLOR_TEXT_MAIN).pack(pady=(25, 5))
        ctk.CTkLabel(d, text="This cannot be undone.", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(pady=(0, 15))
        def confirm():
            d.destroy()
            try:
                conn = self.get_db_connection()
                with conn.cursor() as cur: cur.execute("DELETE FROM transactions WHERE id = %s", (tid,))
                conn.commit(); self.data["transactions"] = [t for t in self.data["transactions"] if t.get("id","") != tid]
                self._cached_stats = None; self.refresh_ui()
            except Exception as e:
                try: self.get_db_connection().rollback()
                except: pass
                self.show_error_native(f"Delete failed:\n{e}")
        bf = ctk.CTkFrame(d, fg_color="transparent"); bf.pack(pady=10)
        ctk.CTkButton(bf, text="Cancel", width=110, height=38, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=d.destroy).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="Delete", width=110, height=38, corner_radius=12, fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_DIM, command=confirm).pack(side="right", padx=8)

    def update_rate(self, key, entry_widget):
        try:
            val = float(entry_widget.get())
            if val <= 0: raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive rate."); return
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur: cur.execute("UPDATE settings SET value = %s WHERE key = %s", (val, key))
            conn.commit(); self.data["settings"][key] = val; self._cached_stats = None; self.refresh_ui()
            self.show_success_native("Rate updated.")
        except Exception as e:
            try: self.get_db_connection().rollback()
            except: pass
            self.show_error_native(f"Update failed:\n{e}")

    def show_error_native(self, msg):
        d = ctk.CTkToplevel(self); d.title("Error"); d.geometry("440x180"); d.configure(fg_color=COLOR_CARD); d.attributes('-topmost', True)
        ctk.CTkLabel(d, text="⚠", font=("Segoe UI", 32), text_color=COLOR_DANGER).pack(pady=(20, 5))
        ctk.CTkLabel(d, text=msg, font=FONT_MAIN, text_color=COLOR_TEXT_MAIN, wraplength=380).pack(pady=(0, 15))
        ctk.CTkButton(d, text="OK", width=100, height=36, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=d.destroy).pack()

    def show_success_native(self, msg):
        d = ctk.CTkToplevel(self); d.title("Success"); d.geometry("320x160"); d.configure(fg_color=COLOR_CARD); d.attributes('-topmost', True)
        ctk.CTkLabel(d, text="✓", font=("Segoe UI", 32), text_color=COLOR_SUCCESS).pack(pady=(20, 5))
        ctk.CTkLabel(d, text=msg, font=FONT_BOLD, text_color=COLOR_TEXT_MAIN).pack(pady=(0, 15))
        ctk.CTkButton(d, text="OK", width=100, height=36, corner_radius=12, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, command=d.destroy).pack()

    # ================================================================
    # SIDEBAR
    # ================================================================
    def create_sidebar(self):
        logo = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        logo.grid(row=0, column=0, padx=25, pady=(35, 8), sticky="w")
        ctk.CTkFrame(logo, width=10, height=10, corner_radius=5, fg_color=COLOR_PRIMARY).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(logo, text="Finance", font=("Segoe UI Bold", 20), text_color=COLOR_TEXT_MAIN).pack(side="left")
        ctk.CTkFrame(self.sidebar_frame, fg_color=COLOR_ACCENT_LINE, height=1).grid(row=0, column=0, sticky="sew", padx=20)

        for i, (txt, name) in enumerate([("⬡  Dashboard","dashboard"),("＋  Add Income","income"),("⇄  Transfers","transfer"),("▼  Expenses","expenses"),("◈  Savings","savings"),("⤴  Lending","lending")]):
            btn = ctk.CTkButton(self.sidebar_frame, text=txt, height=40, corner_radius=12, font=("Segoe UI Semibold", 13), fg_color="transparent", text_color=COLOR_TEXT_SUB, hover_color=COLOR_HOVER, anchor="w", border_spacing=18, command=lambda n=name: self.show_frame(n))
            btn.grid(row=i+1, column=0, sticky="ew", padx=12, pady=2); self.nav_buttons[name] = btn

        sf = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        sf.grid(row=9, column=0, padx=18, pady=(5, 25), sticky="sew")

        # Status
        sb = ctk.CTkFrame(sf, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color=COLOR_BORDER); sb.pack(fill="x", pady=(0, 12))
        si = ctk.CTkFrame(sb, fg_color="transparent"); si.pack(padx=12, pady=8)
        ctk.CTkFrame(si, width=8, height=8, corner_radius=4, fg_color=COLOR_SUCCESS).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(si, text="Database synced", font=FONT_TINY, text_color=COLOR_SUCCESS).pack(side="left")

        # USD Rate
        ctk.CTkLabel(sf, text="USD → DZD", font=("Segoe UI Bold", 10), text_color=COLOR_TEXT_DIM).pack(anchor="w", pady=(0, 3))
        self.entry_rate_usd = ctk.CTkEntry(sf, height=34, corner_radius=10, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, text_color="white")
        self.entry_rate_usd.insert(0, str(self.data["settings"].get("display_rate", DEFAULT_USD_RATE)))
        self.entry_rate_usd.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(sf, text="Update", height=30, corner_radius=10, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, border_width=1, border_color=COLOR_BORDER, font=("Segoe UI", 10), command=lambda: self.update_rate("display_rate", self.entry_rate_usd)).pack(fill="x", pady=(0, 10))

        # EUR Rate
        ctk.CTkLabel(sf, text="EUR → DZD", font=("Segoe UI Bold", 10), text_color=COLOR_TEXT_DIM).pack(anchor="w", pady=(0, 3))
        self.entry_rate_eur = ctk.CTkEntry(sf, height=34, corner_radius=10, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, text_color="white")
        self.entry_rate_eur.insert(0, str(self.data["settings"].get("display_rate_eur", DEFAULT_EUR_RATE)))
        self.entry_rate_eur.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(sf, text="Update", height=30, corner_radius=10, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, border_width=1, border_color=COLOR_BORDER, font=("Segoe UI", 10), command=lambda: self.update_rate("display_rate_eur", self.entry_rate_eur)).pack(fill="x")

    def show_frame(self, name):
        for f in self.frames.values(): f.grid_forget()
        self.frames[name].grid(row=0, column=0, sticky="nsew")
        for bn, btn in self.nav_buttons.items():
            btn.configure(fg_color=COLOR_PRIMARY if bn == name else "transparent", text_color="white" if bn == name else COLOR_TEXT_SUB)
        if name == "dashboard":
            now = datetime.now(); self.selected_month = now.month; self.selected_year = now.year
        self.data = self.fetch_data_from_db(); self.refresh_ui()

    def get_monthly_key(self): return f"{self.selected_year}-{self.selected_month:02d}"

    def get_rates(self):
        def sr(k, d):
            try: v = float(self.data["settings"].get(k, d)); return v if v > 0 else d
            except: return d
        return sr("display_rate", DEFAULT_USD_RATE), sr("display_rate_eur", DEFAULT_EUR_RATE)

    def to_dzd(self, amt, curr):
        usd_r, eur_r = self.get_rates()
        if curr == "USD": return amt * usd_r
        if curr == "EUR": return amt * eur_r
        return amt

    def dzd_to(self, dzd, target):
        usd_r, eur_r = self.get_rates()
        if target == "USD": return dzd / usd_r if usd_r else 0
        if target == "EUR": return dzd / eur_r if eur_r else 0
        return dzd

    def fmt_equiv(self, amt, curr):
        """Return a string showing the DZD equiv (or USD equiv if already DZD)."""
        usd_r, eur_r = self.get_rates()
        if curr == "USD":
            return f"≈ {amt * usd_r:,.0f} DZD"
        elif curr == "EUR":
            return f"≈ {amt * eur_r:,.0f} DZD"
        else:
            return f"≈ ${amt / usd_r:,.2f}" if usd_r else ""

    # ================================================================
    # STATS
    # ================================================================
    def calculate_stats(self):
        if self._cached_stats and self._cached_stats.get("_mk") == self.get_monthly_key():
            return self._cached_stats
        tm = self.get_monthly_key()
        s = {
            "usd": 0.0, "eur": 0.0, "dzd": 0.0, "paypal": 0.0,
            "usd_locked": 0.0, "eur_locked": 0.0, "dzd_locked": 0.0,
            "m_earn_usd": 0.0, "m_earn_eur": 0.0, "m_earn_dzd": 0.0,
            "m_spend_usd": 0.0, "m_spend_eur": 0.0, "m_spend_dzd": 0.0,
            "lent_usd": 0.0, "lent_eur": 0.0, "lent_dzd": 0.0,
        }
        for t in self.data.get("transactions", []):
            c = t.get('currency', 'USD'); tt = t.get('type', ''); td = str(t.get('date', ''))
            def sf(k):
                try: return float(t.get(k, 0))
                except: return 0.0
            base = sf('amount'); net = sf('net_amount')
            if net == 0: net = base

            bal_key = {"USD": "usd", "EUR": "eur", "DZD": "dzd"}.get(c, "usd")
            lock_key = bal_key + "_locked"

            if tt == 'income':
                if t.get('to_paypal') and c == 'USD': s['paypal'] += net
                else: s[bal_key] += net
            elif tt == 'expense':
                s[bal_key] -= base
            elif tt == 'transfer_usd_dzd':
                s['usd'] -= sf('amount_usd'); s['dzd'] += sf('amount_dzd')
            elif tt == 'transfer_eur_dzd':
                s['eur'] -= sf('amount_eur'); s['dzd'] += sf('amount_dzd')
            elif tt == 'transfer_dzd_eur':
                s['dzd'] -= sf('amount_dzd'); s['eur'] += sf('amount_eur')
            elif tt == 'transfer_paypal_bank':
                s['paypal'] -= sf('amount_sent'); s['usd'] += sf('amount_received')
            elif tt == 'savings_deposit':
                s[bal_key] -= base; s[lock_key] += base
            elif tt == 'savings_withdraw':
                s[bal_key] += base; s[lock_key] -= base
            elif tt == 'loan_out':
                s[bal_key] -= base
                if t.get('status', 'active') == 'active':
                    s["lent_" + bal_key] += base
            elif tt == 'loan_repaid':
                s[bal_key] += base

            if td.startswith(tm):
                if tt == 'income': s["m_earn_" + bal_key] += net
                elif tt == 'expense': s["m_spend_" + bal_key] += base

        s["_mk"] = tm; self._cached_stats = s; return s

    # ================================================================
    # CARD BUILDERS
    # ================================================================
    def make_stat_card(self, parent, label, accent, main_v, sub_v):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16, border_width=1, border_color=COLOR_BORDER)
        ctk.CTkFrame(card, fg_color=accent, height=3, corner_radius=2).pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(card, text=label, font=("Segoe UI Semibold", 12), text_color=COLOR_TEXT_SUB).pack(padx=18, pady=(10, 2), anchor="w")
        lm = ctk.CTkLabel(card, text=main_v, font=FONT_NUMBERS, text_color=COLOR_TEXT_MAIN); lm.pack(padx=18, anchor="w")
        ls = ctk.CTkLabel(card, text=sub_v, font=("Segoe UI", 14), text_color=COLOR_TEXT_SUB); ls.pack(padx=18, pady=(0, 16), anchor="w")
        return card, lm, ls

    def make_triple_card(self, parent, label, accent):
        """Card with USD | EUR | DZD columns."""
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16, border_width=1, border_color=COLOR_BORDER)
        ctk.CTkFrame(card, fg_color=accent, height=3, corner_radius=2).pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(card, text=label, font=("Segoe UI Semibold", 12), text_color=COLOR_TEXT_SUB).pack(padx=18, pady=(10, 4), anchor="w")
        row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(padx=18, pady=(0, 16), fill="x")

        cols = []
        for i in range(3):
            if i > 0:
                ctk.CTkFrame(row, fg_color=COLOR_ACCENT_LINE, width=1).pack(side="left", fill="y", padx=14, pady=2)
            col = ctk.CTkFrame(row, fg_color="transparent"); col.pack(side="left", expand=True)
            lm = ctk.CTkLabel(col, text="--", font=("Segoe UI Bold", 22), text_color=accent); lm.pack(anchor="w")
            ls = ctk.CTkLabel(col, text="", font=("Segoe UI", 12), text_color=COLOR_TEXT_SUB); ls.pack(anchor="w")
            cols.append((lm, ls))
        return card, cols  # cols = [(usd_main, usd_sub), (eur_main, eur_sub), (dzd_main, dzd_sub)]

    # ================================================================
    # DASHBOARD
    # ================================================================
    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["dashboard"] = frame
        frame.grid_columnconfigure(0, weight=1); frame.grid_rowconfigure(4, weight=1)
        content = ctk.CTkFrame(frame, fg_color="transparent"); content.grid(row=0, column=0, sticky="nsew", padx=30, pady=(25, 0))
        content.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(content, fg_color="transparent"); hdr.pack(fill="x", pady=(0, 18))
        nv = ctk.CTkFrame(hdr, fg_color="transparent"); nv.pack(side="left")
        nav = ctk.CTkFrame(nv, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER); nav.pack(side="left")
        ctk.CTkButton(nav, text="‹", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI",18), command=lambda: self.change_time('m',-1)).pack(side="left", padx=2)
        self.lbl_month = ctk.CTkLabel(nav, text="", font=("Segoe UI Semibold",13), width=90, text_color=COLOR_TEXT_MAIN); self.lbl_month.pack(side="left")
        ctk.CTkButton(nav, text="›", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI",18), command=lambda: self.change_time('m',1)).pack(side="left", padx=2)
        ny = ctk.CTkFrame(nv, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER); ny.pack(side="left", padx=8)
        ctk.CTkButton(ny, text="‹", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI",18), command=lambda: self.change_time('y',-1)).pack(side="left", padx=2)
        self.lbl_year = ctk.CTkLabel(ny, text="", font=("Segoe UI Semibold",13), width=55, text_color=COLOR_TEXT_MAIN); self.lbl_year.pack(side="left")
        ctk.CTkButton(ny, text="›", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI",18), command=lambda: self.change_time('y',1)).pack(side="left", padx=2)

        nwf = ctk.CTkFrame(hdr, fg_color="transparent"); nwf.pack(side="right")
        ctk.CTkLabel(nwf, text="NET WORTH", font=("Segoe UI Bold",10), text_color=COLOR_TEXT_DIM).pack(anchor="e")
        self.lbl_nw = ctk.CTkLabel(nwf, text="...", font=("Segoe UI Bold",26), text_color=COLOR_TEXT_MAIN); self.lbl_nw.pack(anchor="e")
        self.lbl_nw_sub = ctk.CTkLabel(nwf, text="", font=("Segoe UI",15), text_color=COLOR_TEXT_SUB); self.lbl_nw_sub.pack(anchor="e")

        # Income / Expense (triple currency)
        r1 = ctk.CTkFrame(content, fg_color="transparent"); r1.pack(fill="x", pady=(0, 10))
        r1.grid_columnconfigure((0,1), weight=1, uniform="r1")
        c_inc, self.inc_cols = self.make_triple_card(r1, "INCOME THIS MONTH", COLOR_SUCCESS); c_inc.grid(row=0, column=0, sticky="ew", padx=(0,6))
        c_exp, self.exp_cols = self.make_triple_card(r1, "EXPENSES THIS MONTH", COLOR_DANGER); c_exp.grid(row=0, column=1, sticky="ew", padx=(6,0))

        # Balance cards: PayPal | USD | EUR | DZD
        r2 = ctk.CTkFrame(content, fg_color="transparent"); r2.pack(fill="x", pady=(0, 18))
        r2.grid_columnconfigure((0,1,2,3), weight=1, uniform="r2")
        c_pp, self.lbl_pp, self.lbl_pp_s = self.make_stat_card(r2, "PAYPAL (PENDING)", COLOR_WARNING, "$0", "≈ 0 DZD"); c_pp.grid(row=0, column=0, sticky="ew", padx=(0,5))
        c_usd, self.lbl_usd, self.lbl_usd_s = self.make_stat_card(r2, "BANK USD", COLOR_PRIMARY, "$0", "≈ 0 DZD"); c_usd.grid(row=0, column=1, sticky="ew", padx=5)
        c_eur, self.lbl_eur, self.lbl_eur_s = self.make_stat_card(r2, "EUR BALANCE", COLOR_EUR, "€0", "≈ 0 DZD"); c_eur.grid(row=0, column=2, sticky="ew", padx=5)
        c_dzd, self.lbl_dzd, self.lbl_dzd_s = self.make_stat_card(r2, "LOCAL CASH DZD", COLOR_TEXT_MAIN, "0 DZD", "≈ $0"); c_dzd.grid(row=0, column=3, sticky="ew", padx=(5,0))

        # Filters
        fr = ctk.CTkFrame(content, fg_color="transparent"); fr.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(fr, text="Transactions", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(side="left")
        self.d_sort = ctk.CTkOptionMenu(fr, values=["Newest First","Oldest First","Highest","Lowest"], fg_color=COLOR_INPUT, button_color=COLOR_INPUT, button_hover_color=COLOR_HOVER, corner_radius=10, height=32, font=FONT_TINY, command=lambda _: self.update_dashboard_history()); self.d_sort.pack(side="right", padx=(8,0))
        self.d_type = ctk.CTkOptionMenu(fr, values=["All","Income","Expense","Transfer","Savings","Loan"], fg_color=COLOR_INPUT, button_color=COLOR_INPUT, button_hover_color=COLOR_HOVER, corner_radius=10, height=32, font=FONT_TINY, command=lambda _: self.update_dashboard_history()); self.d_type.pack(side="right")

        self.dash_list = ctk.CTkScrollableFrame(frame, fg_color="transparent", scrollbar_button_color=COLOR_BORDER)
        self.dash_list.grid(row=4, column=0, sticky="nsew", padx=30, pady=(0, 20))

    def update_dashboard_history(self):
        for w in self.dash_list.winfo_children(): w.destroy()
        tm = self.get_monthly_key(); ft = self.d_type.get(); fs = self.d_sort.get()
        fl = []
        for t in self.data["transactions"]:
            td = str(t.get('date','')); tv = str(t.get('type',''))
            if not td.startswith(tm): continue
            ok = (ft=="All" or (ft=="Income" and tv=='income') or (ft=="Expense" and tv=='expense') or (ft=="Transfer" and 'transfer' in tv) or (ft=="Savings" and 'savings' in tv) or (ft=="Loan" and tv in ('loan_out','loan_repaid')))
            if ok: fl.append(t)
        def sa(x):
            try: return float(x.get('amount', x.get('amount_usd', x.get('amount_sent',0))))
            except: return 0
        if "Newest" in fs: fl.sort(key=lambda x: str(x.get('date','')), reverse=True)
        elif "Oldest" in fs: fl.sort(key=lambda x: str(x.get('date','')))
        elif "Highest" in fs: fl.sort(key=sa, reverse=True)
        elif "Lowest" in fs: fl.sort(key=sa)
        if not fl: ctk.CTkLabel(self.dash_list, text="No transactions this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=40)
        else:
            for t in fl: self.create_list_row(self.dash_list, t, simple=True)

    # ================================================================
    # REFRESH
    # ================================================================
    def refresh_ui(self):
        try:
            st = self.calculate_stats(); ur, er = self.get_rates()

            self.lbl_pp.configure(text=f"${st['paypal']:,.2f}"); self.lbl_pp_s.configure(text=f"≈ {st['paypal']*ur:,.0f} DZD")
            self.lbl_usd.configure(text=f"${st['usd']:,.2f}"); self.lbl_usd_s.configure(text=f"≈ {st['usd']*ur:,.0f} DZD")
            self.lbl_eur.configure(text=f"€{st['eur']:,.2f}"); self.lbl_eur_s.configure(text=f"≈ {st['eur']*er:,.0f} DZD")
            self.lbl_dzd.configure(text=f"{st['dzd']:,.2f} DZD"); self.lbl_dzd_s.configure(text=f"≈ ${st['dzd']/ur:,.2f}")

            # Income triple
            for i, (k, sym, r) in enumerate([("usd","$",ur),("eur","€",er),("dzd","",1)]):
                val = st[f"m_earn_{k}"]
                if k == "dzd": self.inc_cols[i][0].configure(text=f"+ {val:,.0f} DZD"); self.inc_cols[i][1].configure(text=f"≈ ${val/ur:,.2f}")
                else: self.inc_cols[i][0].configure(text=f"+ {sym}{val:,.2f}"); self.inc_cols[i][1].configure(text=f"≈ {val*r:,.0f} DZD")

            # Expense triple
            for i, (k, sym, r) in enumerate([("usd","$",ur),("eur","€",er),("dzd","",1)]):
                val = st[f"m_spend_{k}"]
                if k == "dzd": self.exp_cols[i][0].configure(text=f"{val:,.0f} DZD"); self.exp_cols[i][1].configure(text=f"≈ ${val/ur:,.2f}")
                else: self.exp_cols[i][0].configure(text=f"{sym}{val:,.2f}"); self.exp_cols[i][1].configure(text=f"≈ {val*r:,.0f} DZD")

            # Net worth
            t_usd = st['usd'] + st['paypal'] + st['usd_locked']
            t_eur = st['eur'] + st['eur_locked']
            t_dzd = st['dzd'] + st['dzd_locked']
            nw_dzd = t_dzd + (t_usd * ur) + (t_eur * er)
            nw_usd = t_usd + t_eur * (er / ur) + (t_dzd / ur)
            self.lbl_nw.configure(text=f"${nw_usd:,.2f}"); self.lbl_nw_sub.configure(text=f"≈ {nw_dzd:,.0f} DZD")
            self.lbl_month.configure(text=datetime(self.selected_year, self.selected_month, 1).strftime('%B'))
            self.lbl_year.configure(text=str(self.selected_year))

            # Savings vault
            if "savings" in self.frames:
                self.lbl_v_usd.configure(text=f"${st['usd_locked']:,.2f}"); self.lbl_v_usd_s.configure(text=f"≈ {st['usd_locked']*ur:,.0f} DZD")
                self.lbl_v_eur.configure(text=f"€{st['eur_locked']:,.2f}"); self.lbl_v_eur_s.configure(text=f"≈ {st['eur_locked']*er:,.0f} DZD")
                self.lbl_v_dzd.configure(text=f"{st['dzd_locked']:,.2f} DZD"); self.lbl_v_dzd_s.configure(text=f"≈ ${st['dzd_locked']/ur:,.2f}")

            # Lending
            if "lending" in self.frames:
                self.lbl_lo_usd.configure(text=f"${st['lent_usd']:,.2f}"); self.lbl_lo_usd_s.configure(text=f"≈ {st['lent_usd']*ur:,.0f} DZD")
                self.lbl_lo_eur.configure(text=f"€{st['lent_eur']:,.2f}"); self.lbl_lo_eur_s.configure(text=f"≈ {st['lent_eur']*er:,.0f} DZD")
                self.lbl_lo_dzd.configure(text=f"{st['lent_dzd']:,.2f} DZD"); self.lbl_lo_dzd_s.configure(text=f"≈ ${st['lent_dzd']/ur:,.2f}")

            self.update_income_list(); self.update_expense_list(); self.update_transfer_list()
            self.update_savings_list(); self.update_dashboard_history(); self.update_lending_list()
        except Exception as e:
            messagebox.showerror("Refresh Error", f"{e}\n\n{traceback.format_exc()}")

    def change_time(self, u, d):
        if u == 'm':
            if d == 1:
                if self.selected_month == 12: self.selected_month = 1; self.selected_year += 1
                else: self.selected_month += 1
            else:
                if self.selected_month == 1: self.selected_month = 12; self.selected_year -= 1
                else: self.selected_month -= 1
        else: self.selected_year += d
        self._cached_stats = None; self.refresh_ui()

    # ================================================================
    # FORM HELPERS
    # ================================================================
    def make_form(self, parent, title):
        parent.grid_columnconfigure(0, weight=1); parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text=title, font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).grid(row=0, column=0, sticky="w", padx=30, pady=(25, 15))
        c = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        c.grid(row=1, column=0, sticky="new", padx=30, pady=(0, 10))
        inner = ctk.CTkFrame(c, fg_color="transparent"); inner.pack(fill="x", padx=24, pady=24)
        inner.grid_columnconfigure((0, 1), weight=1); return inner

    def inp(self, p, ph):
        return ctk.CTkEntry(p, height=44, corner_radius=12, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", placeholder_text=ph, placeholder_text_color=COLOR_TEXT_DIM)

    def combo(self, p, vals):
        return ctk.CTkComboBox(p, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=vals)

    def btn(self, p, txt, col, col_h, cmd, tc="white"):
        return ctk.CTkButton(p, text=txt, height=46, corner_radius=12, fg_color=col, hover_color=col_h, font=FONT_BOLD, text_color=tc, command=cmd)

    def on_inc_curr(self, ch):
        is_dzd = "DZD" in ch
        self.combo_fee.set("No Fee"); self.combo_fee.configure(state="disabled" if is_dzd else "normal")
        self.entry_fee.configure(state="disabled"); self.chk_pp_var.set("off")
        self.chk_pp.configure(state="disabled" if is_dzd else "normal")

    # ================================================================
    # INCOME
    # ================================================================
    def create_income_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["income"] = f
        form = self.make_form(f, "Add Income")
        self.e_inc_name = self.inp(form, "Source Name"); self.e_inc_name.grid(row=0, column=0, padx=(0,6), pady=6, sticky="ew")
        self.e_inc_amt = self.inp(form, "Gross Amount"); self.e_inc_amt.grid(row=0, column=1, padx=(6,0), pady=6, sticky="ew")
        self.combo_inc_curr = self.combo(form, ["USD (Online)", "EUR (Online)", "DZD (Cash)"])
        self.combo_inc_curr.configure(command=self.on_inc_curr); self.combo_inc_curr.grid(row=1, column=0, columnspan=2, pady=6, sticky="ew")
        self.combo_fee = self.combo(form, ["No Fee", "Upwork (10%)", "Manual Fee"])
        self.combo_fee.configure(command=lambda c: self.entry_fee.configure(state="normal" if "Manual" in c else "disabled"))
        self.combo_fee.grid(row=2, column=0, padx=(0,6), pady=6, sticky="ew")
        self.entry_fee = self.inp(form, "Fee Amount"); self.entry_fee.grid(row=2, column=1, padx=(6,0), pady=6, sticky="ew"); self.entry_fee.configure(state="disabled")
        self.chk_pp_var = ctk.StringVar(value="on")
        self.chk_pp = ctk.CTkCheckBox(form, text="Add to PayPal (USD only)", variable=self.chk_pp_var, onvalue="on", offvalue="off", font=FONT_MAIN, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, border_color=COLOR_BORDER)
        self.chk_pp.grid(row=3, column=0, columnspan=2, pady=12, sticky="w")
        self.btn(form, "Add Income", COLOR_SUCCESS, COLOR_SUCCESS_DIM, self.add_income).grid(row=4, column=0, columnspan=2, pady=(8,0), sticky="ew")
        ctk.CTkLabel(f, text="History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=2, column=0, sticky="w", padx=30, pady=(20,8))
        self.income_list = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER); self.income_list.grid(row=3, column=0, sticky="nsew", padx=30, pady=(0,20))

    # ================================================================
    # TRANSFERS
    # ================================================================
    def fill_max_pp(self):
        b = self.calculate_stats()['paypal']; self.e_pp.delete(0,'end'); self.e_pp.insert(0, str(round(b,2)))

    def create_transfer_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["transfer"] = f
        ctk.CTkLabel(f, text="Transfers", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w", padx=30, pady=(25,15))
        tv = ctk.CTkTabview(f, fg_color=COLOR_CARD, segmented_button_fg_color=COLOR_INPUT, segmented_button_selected_color=COLOR_PRIMARY, segmented_button_unselected_color=COLOR_INPUT, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        tv.pack(fill="x", padx=30)

        # PayPal → Bank
        t1 = tv.add("PayPal → Bank"); c1 = ctk.CTkFrame(t1, fg_color="transparent"); c1.pack(fill="x", padx=20, pady=10)
        pr = ctk.CTkFrame(c1, fg_color="transparent"); pr.pack(fill="x", pady=6)
        self.e_pp = self.inp(pr, "Amount ($)"); self.e_pp.pack(side="left", fill="x", expand=True, padx=(0,8))
        ctk.CTkButton(pr, text="MAX", width=60, height=44, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, border_width=1, border_color=COLOR_BORDER, font=FONT_TINY, command=self.fill_max_pp).pack(side="right")
        self.combo_pp = self.combo(c1, ["Automatic (Free)", "Manual ($5 Fee)"]); self.combo_pp.pack(fill="x", pady=6)
        self.btn(c1, "Process Transfer", COLOR_WARNING, COLOR_WARNING_DIM, self.transfer_pp, tc="#000").pack(fill="x", pady=(12,6))

        # Sell USD → DZD
        t2 = tv.add("USD → DZD"); c2 = ctk.CTkFrame(t2, fg_color="transparent"); c2.pack(fill="x", padx=20, pady=10)
        self.e_ex_usd = self.inp(c2, "Amount ($)"); self.e_ex_usd.pack(fill="x", pady=6)
        self.e_ex_usd_rate = self.inp(c2, "Rate (1 USD = ? DZD)"); self.e_ex_usd_rate.pack(fill="x", pady=6)
        self.btn(c2, "Confirm Sale", COLOR_PRIMARY, COLOR_PRIMARY_DIM, self.transfer_usd_dzd).pack(fill="x", pady=(12,6))

        # Sell EUR → DZD
        t3 = tv.add("EUR → DZD"); c3 = ctk.CTkFrame(t3, fg_color="transparent"); c3.pack(fill="x", padx=20, pady=10)
        self.e_ex_eur = self.inp(c3, "Amount (€)"); self.e_ex_eur.pack(fill="x", pady=6)
        self.e_ex_eur_rate = self.inp(c3, "Rate (1 EUR = ? DZD)"); self.e_ex_eur_rate.pack(fill="x", pady=6)
        self.btn(c3, "Confirm Sale", COLOR_EUR, COLOR_EUR_DIM, self.transfer_eur_dzd).pack(fill="x", pady=(12,6))

        # Buy EUR with DZD
        t4 = tv.add("DZD → EUR"); c4 = ctk.CTkFrame(t4, fg_color="transparent"); c4.pack(fill="x", padx=20, pady=10)
        self.e_buy_eur_dzd = self.inp(c4, "DZD Amount to spend"); self.e_buy_eur_dzd.pack(fill="x", pady=6)
        self.e_buy_eur_rate = self.inp(c4, "Rate (1 EUR = ? DZD)"); self.e_buy_eur_rate.pack(fill="x", pady=6)
        self.btn(c4, "Buy EUR", COLOR_EUR, COLOR_EUR_DIM, self.transfer_dzd_eur).pack(fill="x", pady=(12,6))

        ctk.CTkLabel(f, text="Transfer History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w", padx=30, pady=(20,8))
        self.transfer_list = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER); self.transfer_list.pack(fill="both", expand=True, padx=30, pady=(0,20))

    # ================================================================
    # EXPENSES
    # ================================================================
    def create_expenses_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["expenses"] = f
        form = self.make_form(f, "Record Expense")
        self.e_exp_desc = self.inp(form, "Description"); self.e_exp_desc.grid(row=0, column=0, padx=(0,6), pady=6, sticky="ew")
        self.e_exp_amt = self.inp(form, "Amount"); self.e_exp_amt.grid(row=0, column=1, padx=(6,0), pady=6, sticky="ew")
        self.combo_exp_cat = self.combo(form, ["Essentials","Debt","Luxury","Business","Other"]); self.combo_exp_cat.grid(row=1, column=0, padx=(0,6), pady=6, sticky="ew")
        self.combo_exp_curr = self.combo(form, ["DZD (Cash)","USD (Online)","EUR (Online)"]); self.combo_exp_curr.grid(row=1, column=1, padx=(6,0), pady=6, sticky="ew")
        self.btn(form, "Record Expense", COLOR_DANGER, COLOR_DANGER_DIM, self.add_expense).grid(row=2, column=0, columnspan=2, pady=(12,0), sticky="ew")
        ctk.CTkLabel(f, text="Recent", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=2, column=0, sticky="w", padx=30, pady=(20,8))
        self.expense_list = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER); self.expense_list.grid(row=3, column=0, sticky="nsew", padx=30, pady=(0,20))

    # ================================================================
    # SAVINGS
    # ================================================================
    def create_savings_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["savings"] = f
        f.grid_columnconfigure((0,1,2), weight=1); f.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(f, text="Savings Vault", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).grid(row=0, column=0, columnspan=3, sticky="w", padx=30, pady=(25,15))

        r0 = ctk.CTkFrame(f, fg_color="transparent"); r0.grid(row=1, column=0, columnspan=3, sticky="ew", padx=30, pady=(0,15))
        r0.grid_columnconfigure((0,1,2), weight=1, uniform="sv")
        c1, self.lbl_v_usd, self.lbl_v_usd_s = self.make_stat_card(r0, "USD VAULT", COLOR_SAVINGS, "$0", "≈ 0 DZD"); c1.grid(row=0, column=0, sticky="ew", padx=(0,5))
        c2, self.lbl_v_eur, self.lbl_v_eur_s = self.make_stat_card(r0, "EUR VAULT", COLOR_EUR, "€0", "≈ 0 DZD"); c2.grid(row=0, column=1, sticky="ew", padx=5)
        c3, self.lbl_v_dzd, self.lbl_v_dzd_s = self.make_stat_card(r0, "DZD VAULT", COLOR_SAVINGS, "0 DZD", "≈ $0"); c3.grid(row=0, column=2, sticky="ew", padx=(5,0))

        fc = ctk.CTkFrame(f, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        fc.grid(row=2, column=0, columnspan=3, sticky="ew", padx=30, pady=(0,10))
        form = ctk.CTkFrame(fc, fg_color="transparent"); form.pack(fill="x", padx=24, pady=20); form.grid_columnconfigure((0,1), weight=1)
        self.combo_sav_act = self.combo(form, ["Lock into Savings","Withdraw to Available"]); self.combo_sav_act.grid(row=0, column=0, padx=(0,6), pady=6, sticky="ew")
        self.combo_sav_curr = self.combo(form, ["USD","EUR","DZD"]); self.combo_sav_curr.grid(row=0, column=1, padx=(6,0), pady=6, sticky="ew")
        self.e_sav = self.inp(form, "Amount"); self.e_sav.grid(row=1, column=0, columnspan=2, pady=6, sticky="ew")
        self.btn(form, "Confirm", COLOR_SAVINGS, COLOR_SAVINGS_DIM, self.manage_savings).grid(row=2, column=0, columnspan=2, pady=(8,0), sticky="ew")

        ctk.CTkLabel(f, text="History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=3, column=0, columnspan=3, sticky="w", padx=30, pady=(20,8))
        self.savings_list = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER); self.savings_list.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=30, pady=(0,20))

    # ================================================================
    # LENDING
    # ================================================================
    def create_lending_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["lending"] = f
        f.grid_columnconfigure(0, weight=1); f.grid_rowconfigure(4, weight=1)
        hdr = ctk.CTkFrame(f, fg_color="transparent"); hdr.grid(row=0, column=0, sticky="w", padx=30, pady=(25,15))
        ctk.CTkLabel(hdr, text="Lending Tracker", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        ctk.CTkLabel(hdr, text="Track money lent out and repayments", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(anchor="w", pady=(2,0))

        sr = ctk.CTkFrame(f, fg_color="transparent"); sr.grid(row=1, column=0, sticky="ew", padx=30, pady=(0,15))
        sr.grid_columnconfigure((0,1,2), weight=1, uniform="ln")
        c1, self.lbl_lo_usd, self.lbl_lo_usd_s = self.make_stat_card(sr, "OUTSTANDING USD", COLOR_LOAN, "$0", "≈ 0 DZD"); c1.grid(row=0, column=0, sticky="ew", padx=(0,5))
        c2, self.lbl_lo_eur, self.lbl_lo_eur_s = self.make_stat_card(sr, "OUTSTANDING EUR", COLOR_LOAN, "€0", "≈ 0 DZD"); c2.grid(row=0, column=1, sticky="ew", padx=5)
        c3, self.lbl_lo_dzd, self.lbl_lo_dzd_s = self.make_stat_card(sr, "OUTSTANDING DZD", COLOR_LOAN, "0 DZD", "≈ $0"); c3.grid(row=0, column=2, sticky="ew", padx=(5,0))

        fc = ctk.CTkFrame(f, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        fc.grid(row=2, column=0, sticky="ew", padx=30, pady=(0,10))
        form = ctk.CTkFrame(fc, fg_color="transparent"); form.pack(fill="x", padx=24, pady=24); form.grid_columnconfigure((0,1), weight=1)
        ctk.CTkLabel(form, text="Record a Loan", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,10))
        self.e_loan_who = self.inp(form, "Who?"); self.e_loan_who.grid(row=1, column=0, padx=(0,6), pady=6, sticky="ew")
        self.e_loan_amt = self.inp(form, "Amount"); self.e_loan_amt.grid(row=1, column=1, padx=(6,0), pady=6, sticky="ew")
        self.combo_loan_curr = self.combo(form, ["USD","EUR","DZD"]); self.combo_loan_curr.grid(row=2, column=0, columnspan=2, pady=6, sticky="ew")
        ctk.CTkLabel(form, text="Notes (optional)", font=FONT_TINY, text_color=COLOR_TEXT_SUB).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6,2))
        self.txt_loan = ctk.CTkTextbox(form, height=80, corner_radius=12, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", font=FONT_MAIN)
        self.txt_loan.grid(row=4, column=0, columnspan=2, pady=(0,6), sticky="ew")
        self.btn(form, "Record Loan", COLOR_LOAN, COLOR_LOAN_DIM, self.add_loan, tc="#000").grid(row=5, column=0, columnspan=2, pady=(8,0), sticky="ew")

        lh = ctk.CTkFrame(f, fg_color="transparent"); lh.grid(row=3, column=0, sticky="ew", padx=30, pady=(20,8))
        ctk.CTkLabel(lh, text="Loans", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(side="left")
        self.combo_loan_filter = ctk.CTkOptionMenu(lh, values=["Active","Repaid","All"], fg_color=COLOR_INPUT, button_color=COLOR_INPUT, button_hover_color=COLOR_HOVER, corner_radius=10, height=32, font=FONT_TINY, command=lambda _: self.update_lending_list())
        self.combo_loan_filter.pack(side="right")
        self.lending_list = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER); self.lending_list.grid(row=4, column=0, sticky="nsew", padx=30, pady=(0,20))

    # ================================================================
    # ACTIONS
    # ================================================================
    def _parse_curr(self, combo_val):
        if "EUR" in combo_val: return "EUR"
        if "USD" in combo_val: return "USD"
        return "DZD"

    def _bal_key(self, curr):
        return {"USD":"usd","EUR":"eur","DZD":"dzd"}.get(curr,"usd")

    def _sym(self, curr):
        return {"USD":"$","EUR":"€","DZD":""}.get(curr,"")

    def _check_bal(self, curr, amt, stat_key=None):
        self._cached_stats = None; s = self.calculate_stats()
        k = stat_key or self._bal_key(curr); bal = s[k]
        if bal < amt:
            sym = self._sym(curr)
            if curr == "DZD": self.show_error_native(f"Insufficient {curr}.\nAvailable: {bal:,.2f} DZD")
            else: self.show_error_native(f"Insufficient {curr}.\nAvailable: {sym}{bal:,.2f}")
            return False
        return True

    def add_income(self):
        name = self.e_inc_name.get().strip()
        if not name: self.show_error_native("Enter a source name."); return
        try:
            val = float(self.e_inc_amt.get())
            if val <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter a valid positive amount."); return
        curr = self._parse_curr(self.combo_inc_curr.get())
        if curr == "DZD": fee, fee_type, to_pp = 0, "No Fee", False
        else:
            fee_type = self.combo_fee.get()
            if "Manual" in fee_type:
                try:
                    fee = float(self.entry_fee.get())
                    if fee < 0: raise ValueError
                except ValueError: self.show_error_native("Enter a valid fee."); return
            elif "Upwork" in fee_type: fee = val * 0.1
            else: fee = 0
            to_pp = self.chk_pp_var.get() == "on" and curr == "USD"
        if fee > val: self.show_error_native("Fee exceeds income."); return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"income","category":name,"amount":val,"currency":curr,"fee_type":fee_type,"fee_amount":fee,"net_amount":val-fee,"to_paypal":to_pp}
        if self.add_transaction_to_db(t):
            self.e_inc_name.delete(0,'end'); self.e_inc_amt.delete(0,'end')
            self.entry_fee.configure(state="normal"); self.entry_fee.delete(0,'end'); self.entry_fee.configure(state="disabled")
            self.show_success_native("Income added.")

    def transfer_pp(self):
        try:
            amt = float(self.e_pp.get())
            if amt <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter a valid amount."); return
        fee = 5.0 if "Manual" in self.combo_pp.get() else 0
        if not self._check_bal("USD", amt, "paypal"): return
        if amt - fee <= 0: self.show_error_native("Amount after fee is zero."); return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"transfer_paypal_bank","amount_sent":amt,"fee_paid":fee,"amount_received":amt-fee}
        if self.add_transaction_to_db(t): self.e_pp.delete(0,'end'); self.show_success_native("Transfer complete.")

    def transfer_usd_dzd(self):
        try:
            usd = float(self.e_ex_usd.get()); rate = float(self.e_ex_usd_rate.get())
            if usd <= 0 or rate <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter valid numbers."); return
        if not self._check_bal("USD", usd): return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"transfer_usd_dzd","amount_usd":usd,"rate":rate,"amount_dzd":usd*rate}
        if self.add_transaction_to_db(t): self.e_ex_usd.delete(0,'end'); self.e_ex_usd_rate.delete(0,'end'); self.show_success_native("Exchange complete.")

    def transfer_eur_dzd(self):
        try:
            eur = float(self.e_ex_eur.get()); rate = float(self.e_ex_eur_rate.get())
            if eur <= 0 or rate <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter valid numbers."); return
        if not self._check_bal("EUR", eur): return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"transfer_eur_dzd","amount_eur":eur,"rate":rate,"amount_dzd":eur*rate}
        if self.add_transaction_to_db(t): self.e_ex_eur.delete(0,'end'); self.e_ex_eur_rate.delete(0,'end'); self.show_success_native("Exchange complete.")

    def transfer_dzd_eur(self):
        try:
            dzd = float(self.e_buy_eur_dzd.get()); rate = float(self.e_buy_eur_rate.get())
            if dzd <= 0 or rate <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter valid numbers."); return
        if not self._check_bal("DZD", dzd): return
        eur_received = dzd / rate
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"transfer_dzd_eur","amount_dzd":dzd,"rate":rate,"amount_eur":eur_received}
        if self.add_transaction_to_db(t): self.e_buy_eur_dzd.delete(0,'end'); self.e_buy_eur_rate.delete(0,'end'); self.show_success_native("EUR purchased.")

    def add_expense(self):
        desc = self.e_exp_desc.get().strip()
        if not desc: self.show_error_native("Enter a description."); return
        try:
            amt = float(self.e_exp_amt.get())
            if amt <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter a valid amount."); return
        curr = self._parse_curr(self.combo_exp_curr.get())
        if not self._check_bal(curr, amt): return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"expense","category":f"{self.combo_exp_cat.get()} - {desc}","amount":amt,"currency":curr}
        if self.add_transaction_to_db(t): self.e_exp_desc.delete(0,'end'); self.e_exp_amt.delete(0,'end'); self.show_success_native("Expense recorded.")

    def manage_savings(self):
        try:
            amt = float(self.e_sav.get())
            if amt <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter a valid amount."); return
        curr = self.combo_sav_curr.get(); act = self.combo_sav_act.get()
        tt = 'savings_deposit' if 'Lock' in act else 'savings_withdraw'
        bk = self._bal_key(curr); lk = bk + "_locked"
        self._cached_stats = None; s = self.calculate_stats()
        if tt == 'savings_deposit':
            if s[bk] < amt: self.show_error_native(f"Insufficient available {curr}."); return
        else:
            if s[lk] < amt: self.show_error_native(f"Insufficient locked {curr}."); return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":tt,"amount":amt,"currency":curr}
        if self.add_transaction_to_db(t): self.e_sav.delete(0,'end'); self.show_success_native("Vault updated.")

    def add_loan(self):
        who = self.e_loan_who.get().strip()
        if not who: self.show_error_native("Enter who you're lending to."); return
        try:
            amt = float(self.e_loan_amt.get())
            if amt <= 0: raise ValueError
        except ValueError: self.show_error_native("Enter a valid amount."); return
        curr = self.combo_loan_curr.get(); notes = self.txt_loan.get("1.0","end-1c").strip()
        if not self._check_bal(curr, amt): return
        t = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"loan_out","borrower":who,"amount":amt,"currency":curr,"notes":notes,"status":"active"}
        if self.add_transaction_to_db(t):
            self.e_loan_who.delete(0,'end'); self.e_loan_amt.delete(0,'end'); self.txt_loan.delete("1.0","end")
            self.show_success_native(f"Loan to {who} recorded.")

    def mark_repaid(self, loan):
        d = ctk.CTkToplevel(self); d.title("Confirm"); d.geometry("420x220"); d.configure(fg_color=COLOR_CARD); d.attributes('-topmost', True)
        amt = loan.get('amount',0); curr = loan.get('currency','USD'); who = loan.get('borrower','?')
        sym = self._sym(curr); eq = self.fmt_equiv(amt, curr)
        if curr == "DZD": amt_str = f"{amt:,.2f} DZD ({eq})"
        else: amt_str = f"{sym}{amt:,.2f} ({eq})"
        ctk.CTkLabel(d, text="Mark as Repaid?", font=("Segoe UI Bold",18), text_color=COLOR_TEXT_MAIN).pack(pady=(20,5))
        ctk.CTkLabel(d, text=f"{who} owes you {amt_str}", font=FONT_MAIN, text_color=COLOR_TEXT_SUB, wraplength=350).pack(pady=(0,5))
        ctk.CTkLabel(d, text="Funds return to your available balance.", font=FONT_TINY, text_color=COLOR_TEXT_DIM).pack(pady=(0,15))
        def confirm():
            d.destroy()
            rt = {"id":str(uuid.uuid4()),"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"type":"loan_repaid","borrower":who,"amount":amt,"currency":curr,"original_loan_id":loan['id'],"notes":f"Repayment from {who}"}
            if self.add_transaction_to_db(rt):
                ul = dict(loan); ul['status'] = 'repaid'; ul['repaid_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.update_transaction_in_db(ul); self.show_success_native(f"{who} marked repaid.")
        bf = ctk.CTkFrame(d, fg_color="transparent"); bf.pack(pady=10)
        ctk.CTkButton(bf, text="Cancel", width=120, height=40, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=d.destroy).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="Confirm Repaid", width=140, height=40, corner_radius=12, fg_color=COLOR_SUCCESS, hover_color=COLOR_SUCCESS_DIM, font=FONT_BOLD, command=confirm).pack(side="right", padx=8)

    # ================================================================
    # LIST ROW
    # ================================================================
    def create_list_row(self, parent, t, simple=False):
        tt = t.get('type',''); td = str(t.get('date',''))[:10]; c = t.get('currency','USD')
        ur, er = self.get_rates()
        def sf(k):
            try: return float(t.get(k, 0))
            except: return 0.0
        net = sf('net_amount'); base = sf('amount')
        if net == 0: net = base
        sym = self._sym(c)
        main_t, sub_t, amt_t, amt_s, col = "","","","",COLOR_TEXT_MAIN

        def famt(v, curr):
            s = self._sym(curr)
            if curr == "DZD": return f"{v:,.2f} DZD"
            return f"{s}{v:,.2f}"

        def feq(v, curr):
            if curr == "USD": return f"≈ {v*ur:,.0f} DZD"
            if curr == "EUR": return f"≈ {v*er:,.0f} DZD"
            return f"≈ ${v/ur:,.2f}"

        if tt == 'income':
            main_t = str(t.get('category','Income'))
            dest = 'Cash' if c=='DZD' else ('PayPal' if t.get('to_paypal') else 'Bank')
            sub_t = f"{td}  •  {dest}"; amt_t = f"+ {famt(net,c)}"; amt_s = feq(net,c); col = COLOR_SUCCESS
        elif tt == 'expense':
            main_t = str(t.get('category','Expense')); sub_t = td
            amt_t = f"- {famt(base,c)}"; amt_s = feq(base,c); col = COLOR_DANGER
        elif tt == 'transfer_usd_dzd':
            main_t = "USD → DZD"; sub_t = f"{td}  •  Rate: {t.get('rate','?')}"
            amt_t = f"+ {sf('amount_dzd'):,.2f} DZD"; amt_s = f"- ${sf('amount_usd'):,.2f}"; col = COLOR_PRIMARY
        elif tt == 'transfer_eur_dzd':
            main_t = "EUR → DZD"; sub_t = f"{td}  •  Rate: {t.get('rate','?')}"
            amt_t = f"+ {sf('amount_dzd'):,.2f} DZD"; amt_s = f"- €{sf('amount_eur'):,.2f}"; col = COLOR_EUR
        elif tt == 'transfer_dzd_eur':
            main_t = "DZD → EUR"; sub_t = f"{td}  •  Rate: {t.get('rate','?')}"
            amt_t = f"+ €{sf('amount_eur'):,.2f}"; amt_s = f"- {sf('amount_dzd'):,.0f} DZD"; col = COLOR_EUR
        elif tt == 'transfer_paypal_bank':
            main_t = "PayPal → Bank"; sub_t = f"{td}  •  Fee: ${sf('fee_paid'):,.2f}"
            amt_t = f"+ ${sf('amount_received'):,.2f}"; amt_s = f"≈ {sf('amount_received')*ur:,.0f} DZD"; col = COLOR_WARNING
        elif tt == 'savings_deposit':
            main_t = "Locked to Vault"; sub_t = td; amt_t = famt(base,c); amt_s = feq(base,c); col = COLOR_SAVINGS
        elif tt == 'savings_withdraw':
            main_t = "Unlocked from Vault"; sub_t = td; amt_t = famt(base,c); amt_s = feq(base,c); col = COLOR_TEXT_SUB
        elif tt == 'loan_out':
            who = t.get('borrower','?'); st = t.get('status','active')
            main_t = f"Lent to {who}"; sub_t = f"{td}  •  {'Active' if st=='active' else 'Repaid'}"
            amt_t = famt(base,c); amt_s = feq(base,c); col = COLOR_LOAN
        elif tt == 'loan_repaid':
            main_t = f"Repaid by {t.get('borrower','?')}"; sub_t = td
            amt_t = f"+ {famt(base,c)}"; amt_s = feq(base,c); col = COLOR_SUCCESS
        else:
            main_t = "Transfer"; sub_t = td; amt_t = "Processed"; col = COLOR_PRIMARY

        outer = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER); outer.pack(fill="x", pady=3)
        inner = ctk.CTkFrame(outer, fg_color="transparent"); inner.pack(fill="x")
        df = ctk.CTkFrame(inner, fg_color="transparent", width=20); df.pack(side="left", padx=(12,0), pady=14)
        ctk.CTkFrame(df, width=8, height=8, corner_radius=4, fg_color=col).pack()
        tf = ctk.CTkFrame(inner, fg_color="transparent"); tf.pack(side="left", padx=(8,10), pady=10)
        ctk.CTkLabel(tf, text=main_t, font=("Segoe UI Semibold",14), text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        ctk.CTkLabel(tf, text=sub_t, font=("Segoe UI",12), text_color=COLOR_TEXT_SUB).pack(anchor="w")
        if not simple:
            ctk.CTkButton(inner, text="×", width=28, height=28, corner_radius=8, fg_color="transparent", hover_color=COLOR_DANGER, font=("Segoe UI",16), text_color=COLOR_TEXT_DIM, command=lambda _id=t.get('id',''): self.delete_transaction(_id)).pack(side="right", padx=(4,12))
        af = ctk.CTkFrame(inner, fg_color="transparent"); af.pack(side="right", padx=12, pady=10)
        ctk.CTkLabel(af, text=amt_t, font=("Segoe UI Bold",15), text_color=col).pack(anchor="e")
        if amt_s: ctk.CTkLabel(af, text=amt_s, font=("Segoe UI",13), text_color=COLOR_TEXT_SUB).pack(anchor="e")

    # ================================================================
    # LIST UPDATES
    # ================================================================
    def update_income_list(self):
        for w in self.income_list.winfo_children(): w.destroy()
        mk = self.get_monthly_key(); found = False
        for t in reversed(self.data.get("transactions",[])):
            if t.get('type')=='income' and str(t.get('date','')).startswith(mk): self.create_list_row(self.income_list, t); found = True
        if not found: ctk.CTkLabel(self.income_list, text="No income this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_expense_list(self):
        for w in self.expense_list.winfo_children(): w.destroy()
        mk = self.get_monthly_key(); found = False
        for t in reversed(self.data.get("transactions",[])):
            if t.get('type')=='expense' and str(t.get('date','')).startswith(mk): self.create_list_row(self.expense_list, t); found = True
        if not found: ctk.CTkLabel(self.expense_list, text="No expenses this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_transfer_list(self):
        for w in self.transfer_list.winfo_children(): w.destroy()
        found = False
        for t in reversed(self.data.get("transactions",[])):
            if 'transfer' in str(t.get('type','')): self.create_list_row(self.transfer_list, t); found = True
        if not found: ctk.CTkLabel(self.transfer_list, text="No transfers yet.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_savings_list(self):
        if not hasattr(self, 'savings_list'): return
        for w in self.savings_list.winfo_children(): w.destroy()
        found = False
        for t in reversed(self.data.get("transactions",[])):
            if 'savings' in str(t.get('type','')): self.create_list_row(self.savings_list, t); found = True
        if not found: ctk.CTkLabel(self.savings_list, text="No savings yet.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_lending_list(self):
        if not hasattr(self, 'lending_list'): return
        for w in self.lending_list.winfo_children(): w.destroy()
        fv = self.combo_loan_filter.get(); ur, er = self.get_rates()
        loans = [t for t in self.data.get("transactions",[]) if t.get('type')=='loan_out']
        if fv == "Active": loans = [l for l in loans if l.get('status','active')=='active']
        elif fv == "Repaid": loans = [l for l in loans if l.get('status')=='repaid']
        loans.sort(key=lambda x: str(x.get('date','')), reverse=True)
        if not loans:
            msg = {"Active":"No active loans.","Repaid":"No repaid loans.","All":"No loans yet."}.get(fv,"No loans.")
            ctk.CTkLabel(self.lending_list, text=msg, font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30); return

        if fv == "Active":
            totals = {}
            for l in loans:
                b = l.get('borrower','?'); c = l.get('currency','USD'); a = l.get('amount',0)
                if b not in totals: totals[b] = {"USD":0,"EUR":0,"DZD":0}
                totals[b][c] += a
            sf = ctk.CTkFrame(self.lending_list, fg_color="transparent"); sf.pack(fill="x", pady=(0,12))
            for bor, tots in totals.items():
                badge = ctk.CTkFrame(sf, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color=COLOR_LOAN); badge.pack(side="left", padx=(0,8), pady=2)
                bi = ctk.CTkFrame(badge, fg_color="transparent"); bi.pack(padx=12, pady=8)
                parts = []
                if tots["USD"] > 0: parts.append(f"${tots['USD']:,.2f}")
                if tots["EUR"] > 0: parts.append(f"€{tots['EUR']:,.2f}")
                if tots["DZD"] > 0: parts.append(f"{tots['DZD']:,.0f} DZD")
                ctk.CTkLabel(bi, text=bor, font=("Segoe UI Bold",12), text_color=COLOR_LOAN).pack(side="left", padx=(0,8))
                ctk.CTkLabel(bi, text=" + ".join(parts), font=("Segoe UI",12), text_color=COLOR_TEXT_MAIN).pack(side="left")

        for loan in loans:
            self._loan_row(self.lending_list, loan)

    def _loan_row(self, parent, t):
        active = t.get('status','active') == 'active'
        amt = t.get('amount',0); curr = t.get('currency','USD'); who = t.get('borrower','?')
        notes = t.get('notes',''); td = str(t.get('date',''))[:10]
        rd = str(t.get('repaid_date',''))[:10] if t.get('repaid_date') else None
        sym = self._sym(curr); eq = self.fmt_equiv(amt, curr)

        outer = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_LOAN if active else COLOR_BORDER); outer.pack(fill="x", pady=4)
        inner = ctk.CTkFrame(outer, fg_color="transparent"); inner.pack(fill="x")
        df = ctk.CTkFrame(inner, fg_color="transparent", width=20); df.pack(side="left", padx=(14,0), pady=14)
        ctk.CTkFrame(df, width=10, height=10, corner_radius=5, fg_color=COLOR_LOAN if active else COLOR_SUCCESS).pack()

        tf = ctk.CTkFrame(inner, fg_color="transparent"); tf.pack(side="left", padx=(10,10), pady=12, fill="x", expand=True)
        nr = ctk.CTkFrame(tf, fg_color="transparent"); nr.pack(anchor="w")
        ctk.CTkLabel(nr, text=who, font=("Segoe UI Bold",15), text_color=COLOR_TEXT_MAIN).pack(side="left")
        bc = COLOR_LOAN if active else COLOR_SUCCESS; bt = "ACTIVE" if active else "REPAID"
        badge = ctk.CTkFrame(nr, fg_color=bc, corner_radius=6); badge.pack(side="left", padx=(10,0))
        ctk.CTkLabel(badge, text=bt, font=("Segoe UI Bold",9), text_color="#000").pack(padx=8, pady=2)
        dtxt = f"Lent on {td}" + (f"  •  Repaid {rd}" if rd else "")
        ctk.CTkLabel(tf, text=dtxt, font=("Segoe UI",12), text_color=COLOR_TEXT_SUB).pack(anchor="w")
        if notes: ctk.CTkLabel(tf, text=notes, font=("Segoe UI",12), text_color=COLOR_TEXT_DIM, wraplength=500, justify="left").pack(anchor="w", pady=(4,0))

        right = ctk.CTkFrame(inner, fg_color="transparent"); right.pack(side="right", padx=14, pady=12)
        if curr == "DZD":
            ctk.CTkLabel(right, text=f"{amt:,.2f} DZD", font=("Segoe UI Bold",16), text_color=COLOR_LOAN if active else COLOR_TEXT_SUB).pack(anchor="e")
        else:
            ctk.CTkLabel(right, text=f"{sym}{amt:,.2f}", font=("Segoe UI Bold",16), text_color=COLOR_LOAN if active else COLOR_TEXT_SUB).pack(anchor="e")
        ctk.CTkLabel(right, text=eq, font=("Segoe UI",13), text_color=COLOR_TEXT_SUB).pack(anchor="e")
        if active:
            ctk.CTkButton(right, text="✓ Mark Repaid", width=120, height=32, corner_radius=10, fg_color=COLOR_SUCCESS, hover_color=COLOR_SUCCESS_DIM, font=("Segoe UI Semibold",11), text_color="#000", command=lambda l=t: self.mark_repaid(l)).pack(anchor="e", pady=(8,0))


if __name__ == "__main__":
    app = FinancialTrackerApp()
    app.mainloop()
