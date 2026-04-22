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
COLOR_CARD_HOVER = "#1A1A24"
COLOR_INPUT = "#1C1C28"
COLOR_INPUT_FOCUS = "#22222E"
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
COLOR_TEXT_MAIN = "#F0F0F5"
COLOR_TEXT_SUB = "#9898AC"
COLOR_TEXT_DIM = "#8585A0"
COLOR_ACCENT_LINE = "#2A2A3A"


class FinancialTrackerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Finance Tracker")
        self.geometry("1380x900")
        self.minsize(1150, 780)
        self.configure(fg_color=COLOR_BG)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.db_url = ""
        self.data = {"settings": {"display_rate": 200.0}, "transactions": []}
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
                self.show_error_native(f"Database connection lost:\n{e}")
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
                    conn = psycopg2.connect(self.db_url)
                    conn.close()
                    self.start_full_app()
                    return
            self.show_setup_screen()
        except Exception as e:
            messagebox.showerror("Connection Error", f"Could not connect:\n{e}")
            self.show_setup_screen()

    def show_setup_screen(self):
        for w in self.winfo_children():
            w.destroy()
        self.grid_columnconfigure(0, weight=1)
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.grid(row=0, column=0)
        card = ctk.CTkFrame(outer, fg_color=COLOR_CARD, corner_radius=24, border_width=1, border_color=COLOR_BORDER)
        card.pack(padx=60, pady=60)
        ctk.CTkFrame(card, fg_color=COLOR_PRIMARY, height=4, corner_radius=2).pack(fill="x", padx=30, pady=(30, 0))
        ctk.CTkLabel(card, text="Connect Database", font=("Segoe UI Bold", 24), text_color=COLOR_TEXT_MAIN).pack(pady=(20, 5))
        ctk.CTkLabel(card, text="Link your Neon PostgreSQL to sync across devices", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(padx=40, pady=(0, 25))
        self.entry_db_url = ctk.CTkEntry(card, width=520, height=50, corner_radius=14, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", placeholder_text="postgresql://user:pass@ep-...neon.tech/neondb", placeholder_text_color=COLOR_TEXT_DIM)
        self.entry_db_url.pack(padx=40, pady=10)
        self.lbl_setup_error = ctk.CTkLabel(card, text="", font=FONT_BOLD, text_color=COLOR_DANGER)
        self.lbl_setup_error.pack(pady=5)
        ctk.CTkButton(card, text="Connect & Sync", width=220, height=48, corner_radius=14, font=FONT_BOLD, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, command=self.attempt_connection).pack(pady=(10, 35))

    def attempt_connection(self):
        url = self.entry_db_url.get().strip()
        if not url:
            self.lbl_setup_error.configure(text="Please enter a valid URL.")
            return
        if "sslmode=require" not in url:
            url += "&sslmode=require" if "?" in url else "?sslmode=require"
        self.lbl_setup_error.configure(text="Connecting...", text_color=COLOR_WARNING)
        self.update()
        try:
            conn = psycopg2.connect(url)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value FLOAT)")
                    cur.execute("CREATE TABLE IF NOT EXISTS transactions (id VARCHAR(255) PRIMARY KEY, t_date VARCHAR(50), t_type VARCHAR(50), payload JSONB)")
                    cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate', 200.0) ON CONFLICT DO NOTHING")
            conn.close()
            self.db_url = url
            with open(self.config_file, "w") as f:
                json.dump({"db_url": self.db_url}, f)
            self.start_full_app()
        except psycopg2.OperationalError as e:
            self.lbl_setup_error.configure(text=f"Connection failed: {e}", text_color=COLOR_DANGER)
        except Exception as e:
            self.lbl_setup_error.configure(text=f"Error: {e}", text_color=COLOR_DANGER)

    def start_full_app(self):
        try:
            for w in self.winfo_children():
                w.destroy()
            self.grid_columnconfigure(0, weight=0)
            self.grid_columnconfigure(1, weight=1)
            self.current_date = datetime.now()
            self.selected_month = self.current_date.month
            self.selected_year = self.current_date.year
            self.data = self.fetch_data_from_db()

            self.sidebar_frame = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color=COLOR_SIDEBAR)
            self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
            self.sidebar_frame.grid_rowconfigure(8, weight=1)
            self.create_sidebar()

            self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            self.main_frame.grid(row=0, column=1, sticky="nsew")
            self.main_frame.grid_columnconfigure(0, weight=1)
            self.main_frame.grid_rowconfigure(0, weight=1)

            self.create_dashboard_frame()
            self.create_income_frame()
            self.create_transfer_frame()
            self.create_expenses_frame()
            self.create_savings_frame()
            self.create_lending_frame()
            self.show_frame("dashboard")
        except Exception as e:
            messagebox.showerror("Fatal Error", f"{e}\n\n{traceback.format_exc()}")

    # ================================================================
    # DB OPS
    # ================================================================
    def fetch_data_from_db(self):
        data = {"settings": {"display_rate": 200.0}, "transactions": []}
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key = 'display_rate'")
                row = cur.fetchone()
                if row:
                    data["settings"]["display_rate"] = row[0]
                cur.execute("SELECT payload FROM transactions ORDER BY t_date ASC")
                rows = cur.fetchall()
                data["transactions"] = [r[0] for r in rows]
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
            conn.commit()
            self.data["transactions"].append(t)
            self._cached_stats = None
            self.refresh_ui()
            return True
        except psycopg2.IntegrityError:
            self.get_db_connection().rollback()
            self.show_error_native("Duplicate transaction.")
            return False
        except Exception as e:
            try:
                self.get_db_connection().rollback()
            except Exception:
                pass
            self.show_error_native(f"Save failed:\n{e}")
            return False

    def update_transaction_in_db(self, t):
        """Update an existing transaction's payload in the DB."""
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("UPDATE transactions SET payload = %s WHERE id = %s", (Json(t), t['id']))
            conn.commit()
            for i, existing in enumerate(self.data["transactions"]):
                if existing.get("id") == t["id"]:
                    self.data["transactions"][i] = t
                    break
            self._cached_stats = None
            self.refresh_ui()
            return True
        except Exception as e:
            try:
                self.get_db_connection().rollback()
            except Exception:
                pass
            self.show_error_native(f"Update failed:\n{e}")
            return False

    def delete_transaction(self, tid):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Confirm")
        dialog.geometry("340x170")
        dialog.configure(fg_color=COLOR_CARD)
        dialog.attributes('-topmost', True)
        ctk.CTkLabel(dialog, text="Delete this transaction?", font=FONT_BOLD, text_color=COLOR_TEXT_MAIN).pack(pady=(25, 5))
        ctk.CTkLabel(dialog, text="This action cannot be undone.", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(pady=(0, 15))

        def confirm():
            dialog.destroy()
            try:
                conn = self.get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM transactions WHERE id = %s", (tid,))
                conn.commit()
                self.data["transactions"] = [t for t in self.data["transactions"] if t.get("id", "") != tid]
                self._cached_stats = None
                self.refresh_ui()
            except Exception as e:
                try:
                    self.get_db_connection().rollback()
                except Exception:
                    pass
                self.show_error_native(f"Delete failed:\n{e}")

        bf = ctk.CTkFrame(dialog, fg_color="transparent")
        bf.pack(pady=10)
        ctk.CTkButton(bf, text="Cancel", width=110, height=38, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=dialog.destroy).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="Delete", width=110, height=38, corner_radius=12, fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_DIM, command=confirm).pack(side="right", padx=8)

    def update_display_rate(self):
        try:
            new_rate = float(self.entry_display_rate.get())
            if new_rate <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive rate.")
            return
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("UPDATE settings SET value = %s WHERE key = 'display_rate'", (new_rate,))
            conn.commit()
            self.data["settings"]["display_rate"] = new_rate
            self._cached_stats = None
            self.refresh_ui()
            self.show_success_native("Rate updated.")
        except Exception as e:
            try:
                self.get_db_connection().rollback()
            except Exception:
                pass
            self.show_error_native(f"Update failed:\n{e}")

    def show_error_native(self, msg):
        d = ctk.CTkToplevel(self)
        d.title("Error")
        d.geometry("440x180")
        d.configure(fg_color=COLOR_CARD)
        d.attributes('-topmost', True)
        ctk.CTkLabel(d, text="⚠", font=("Segoe UI", 32), text_color=COLOR_DANGER).pack(pady=(20, 5))
        ctk.CTkLabel(d, text=msg, font=FONT_MAIN, text_color=COLOR_TEXT_MAIN, wraplength=380).pack(pady=(0, 15))
        ctk.CTkButton(d, text="OK", width=100, height=36, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=d.destroy).pack()

    def show_success_native(self, msg):
        d = ctk.CTkToplevel(self)
        d.title("Success")
        d.geometry("320x160")
        d.configure(fg_color=COLOR_CARD)
        d.attributes('-topmost', True)
        ctk.CTkLabel(d, text="✓", font=("Segoe UI", 32), text_color=COLOR_SUCCESS).pack(pady=(20, 5))
        ctk.CTkLabel(d, text=msg, font=FONT_BOLD, text_color=COLOR_TEXT_MAIN).pack(pady=(0, 15))
        ctk.CTkButton(d, text="OK", width=100, height=36, corner_radius=12, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, command=d.destroy).pack()

    # ================================================================
    # SIDEBAR
    # ================================================================
    def create_sidebar(self):
        logo_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        logo_frame.grid(row=0, column=0, padx=25, pady=(40, 10), sticky="w")
        ctk.CTkFrame(logo_frame, width=10, height=10, corner_radius=5, fg_color=COLOR_PRIMARY).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(logo_frame, text="Finance", font=("Segoe UI Bold", 20), text_color=COLOR_TEXT_MAIN).pack(side="left")
        ctk.CTkFrame(self.sidebar_frame, fg_color=COLOR_ACCENT_LINE, height=1).grid(row=0, column=0, sticky="sew", padx=20)

        buttons_info = [
            ("⬡  Dashboard", "dashboard"),
            ("＋  Add Income", "income"),
            ("⇄  Transfers", "transfer"),
            ("▼  Expenses", "expenses"),
            ("◈  Savings", "savings"),
            ("⤴  Lending", "lending"),
        ]
        for i, (text, name) in enumerate(buttons_info):
            btn = ctk.CTkButton(
                self.sidebar_frame, text=text, height=42, corner_radius=12,
                font=("Segoe UI Semibold", 13), fg_color="transparent",
                text_color=COLOR_TEXT_SUB, hover_color=COLOR_HOVER,
                anchor="w", border_spacing=18,
                command=lambda n=name: self.show_frame(n)
            )
            btn.grid(row=i + 1, column=0, sticky="ew", padx=12, pady=2)
            self.nav_buttons[name] = btn

        settings_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        settings_frame.grid(row=9, column=0, padx=20, pady=(10, 30), sticky="sew")

        status_frame = ctk.CTkFrame(settings_frame, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color=COLOR_BORDER)
        status_frame.pack(fill="x", pady=(0, 15))
        sf_inner = ctk.CTkFrame(status_frame, fg_color="transparent")
        sf_inner.pack(padx=12, pady=10)
        ctk.CTkFrame(sf_inner, width=8, height=8, corner_radius=4, fg_color=COLOR_SUCCESS).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(sf_inner, text="Database synced", font=FONT_TINY, text_color=COLOR_SUCCESS).pack(side="left")

        ctk.CTkLabel(settings_frame, text="USD → DZD Rate", font=FONT_TINY, text_color=COLOR_TEXT_SUB).pack(anchor="w", pady=(0, 5))
        current_rate = self.data.get("settings", {}).get("display_rate", 200.0)
        self.entry_display_rate = ctk.CTkEntry(settings_frame, height=36, corner_radius=10, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, text_color="white")
        self.entry_display_rate.insert(0, str(current_rate))
        self.entry_display_rate.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(settings_frame, text="Update Rate", height=36, corner_radius=10, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, border_width=1, border_color=COLOR_BORDER, font=FONT_TINY, command=self.update_display_rate).pack(fill="x")

    def show_frame(self, name):
        for frame in self.frames.values():
            frame.grid_forget()
        self.frames[name].grid(row=0, column=0, sticky="nsew")
        for bn, btn in self.nav_buttons.items():
            btn.configure(fg_color=COLOR_PRIMARY if bn == name else "transparent", text_color="white" if bn == name else COLOR_TEXT_SUB)
        if name == "dashboard":
            self.current_date = datetime.now()
            self.selected_month = self.current_date.month
            self.selected_year = self.current_date.year
        self.data = self.fetch_data_from_db()
        self.refresh_ui()

    def get_monthly_key(self):
        return f"{self.selected_year}-{self.selected_month:02d}"

    def get_disp_rate(self):
        try:
            r = float(self.data.get("settings", {}).get("display_rate", 200.0))
            return r if r > 0 else 200.0
        except (ValueError, TypeError):
            return 200.0

    # ================================================================
    # STATS
    # ================================================================
    def calculate_stats(self):
        if self._cached_stats is not None and self._cached_stats.get("_month_key") == self.get_monthly_key():
            return self._cached_stats
        target_month = self.get_monthly_key()
        s = {
            "usd_savings": 0.0, "paypal_balance": 0.0, "dzd_cash": 0.0,
            "usd_locked": 0.0, "dzd_locked": 0.0,
            "month_earned_usd": 0.0, "month_earned_dzd": 0.0,
            "month_spent_usd": 0.0, "month_spent_dzd": 0.0,
            "total_lent_usd": 0.0, "total_lent_dzd": 0.0,
        }
        for t in self.data.get("transactions", []):
            curr = t.get('currency', 'USD')
            tt = t.get('type', 'unknown')
            td = str(t.get('date', ''))

            def sf(key):
                try:
                    return float(t.get(key, 0.0))
                except (ValueError, TypeError):
                    return 0.0

            base = sf('amount')
            net = sf('net_amount')
            if net == 0.0:
                net = base

            if tt == 'income':
                if t.get('to_paypal', False) and curr == 'USD':
                    s['paypal_balance'] += net
                elif curr == 'USD':
                    s['usd_savings'] += net
                else:
                    s['dzd_cash'] += net
            elif tt == 'expense':
                if curr == 'USD':
                    s['usd_savings'] -= base
                else:
                    s['dzd_cash'] -= base
            elif tt == 'transfer_usd_dzd':
                s['usd_savings'] -= sf('amount_usd')
                s['dzd_cash'] += sf('amount_dzd')
            elif tt == 'transfer_paypal_bank':
                s['paypal_balance'] -= sf('amount_sent')
                s['usd_savings'] += sf('amount_received')
            elif tt == 'savings_deposit':
                if curr == 'USD':
                    s['usd_savings'] -= base
                    s['usd_locked'] += base
                else:
                    s['dzd_cash'] -= base
                    s['dzd_locked'] += base
            elif tt == 'savings_withdraw':
                if curr == 'USD':
                    s['usd_savings'] += base
                    s['usd_locked'] -= base
                else:
                    s['dzd_cash'] += base
                    s['dzd_locked'] -= base
            elif tt == 'loan_out':
                if t.get('status', 'active') == 'active':
                    if curr == 'USD':
                        s['total_lent_usd'] += base
                    else:
                        s['total_lent_dzd'] += base
                # Money leaves your pocket when you lend
                if curr == 'USD':
                    s['usd_savings'] -= base
                else:
                    s['dzd_cash'] -= base
            elif tt == 'loan_repaid':
                # Money comes back when repaid
                if curr == 'USD':
                    s['usd_savings'] += base
                else:
                    s['dzd_cash'] += base

            if td.startswith(target_month):
                if tt == 'income':
                    if curr == 'USD':
                        s['month_earned_usd'] += net
                    else:
                        s['month_earned_dzd'] += net
                elif tt == 'expense':
                    if curr == 'USD':
                        s['month_spent_usd'] += base
                    else:
                        s['month_spent_dzd'] += base

        s["_month_key"] = target_month
        self._cached_stats = s
        return s

    # ================================================================
    # CARD BUILDERS
    # ================================================================
    def make_stat_card(self, parent, label, accent_color, main_var, sub_var):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16, border_width=1, border_color=COLOR_BORDER)
        ctk.CTkFrame(card, fg_color=accent_color, height=3, corner_radius=2).pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(card, text=label, font=("Segoe UI Semibold", 12), text_color=COLOR_TEXT_SUB).pack(padx=18, pady=(10, 2), anchor="w")
        lbl_main = ctk.CTkLabel(card, text=main_var, font=FONT_NUMBERS, text_color=COLOR_TEXT_MAIN)
        lbl_main.pack(padx=18, anchor="w")
        lbl_sub = ctk.CTkLabel(card, text=sub_var, font=("Segoe UI", 14), text_color=COLOR_TEXT_SUB)
        lbl_sub.pack(padx=18, pady=(0, 16), anchor="w")
        return card, lbl_main, lbl_sub

    def make_dual_stat_card(self, parent, label, accent_color, usd_var, usd_sub, dzd_var, dzd_sub):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16, border_width=1, border_color=COLOR_BORDER)
        ctk.CTkFrame(card, fg_color=accent_color, height=3, corner_radius=2).pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(card, text=label, font=("Segoe UI Semibold", 12), text_color=COLOR_TEXT_SUB).pack(padx=18, pady=(10, 4), anchor="w")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(padx=18, pady=(0, 16), fill="x")
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left")
        lbl_usd = ctk.CTkLabel(left, text=usd_var, font=("Segoe UI Bold", 26), text_color=accent_color)
        lbl_usd.pack(anchor="w")
        lbl_usd_sub = ctk.CTkLabel(left, text=usd_sub, font=("Segoe UI", 14), text_color=COLOR_TEXT_SUB)
        lbl_usd_sub.pack(anchor="w")
        ctk.CTkFrame(row, fg_color=COLOR_ACCENT_LINE, width=1).pack(side="left", fill="y", padx=20, pady=2)
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="left")
        lbl_dzd = ctk.CTkLabel(right, text=dzd_var, font=("Segoe UI Bold", 26), text_color=accent_color)
        lbl_dzd.pack(anchor="w")
        lbl_dzd_sub = ctk.CTkLabel(right, text=dzd_sub, font=("Segoe UI", 14), text_color=COLOR_TEXT_SUB)
        lbl_dzd_sub.pack(anchor="w")
        return card, lbl_usd, lbl_usd_sub, lbl_dzd, lbl_dzd_sub

    # ================================================================
    # DASHBOARD
    # ================================================================
    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["dashboard"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(4, weight=1)

        content = ctk.CTkFrame(frame, fg_color="transparent")
        content.grid(row=0, column=0, sticky="nsew", padx=30, pady=(25, 0))
        content.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(content, fg_color="transparent")
        header.pack(fill="x", pady=(0, 22))
        nav_wrap = ctk.CTkFrame(header, fg_color="transparent")
        nav_wrap.pack(side="left")
        nav = ctk.CTkFrame(nav_wrap, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER)
        nav.pack(side="left")
        ctk.CTkButton(nav, text="‹", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI", 18), command=lambda: self.change_time('month', -1)).pack(side="left", padx=2)
        self.lbl_month_selector = ctk.CTkLabel(nav, text="Month", font=("Segoe UI Semibold", 13), width=90, text_color=COLOR_TEXT_MAIN)
        self.lbl_month_selector.pack(side="left")
        ctk.CTkButton(nav, text="›", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI", 18), command=lambda: self.change_time('month', 1)).pack(side="left", padx=2)
        nav_yr = ctk.CTkFrame(nav_wrap, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER)
        nav_yr.pack(side="left", padx=8)
        ctk.CTkButton(nav_yr, text="‹", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI", 18), command=lambda: self.change_time('year', -1)).pack(side="left", padx=2)
        self.lbl_year_selector = ctk.CTkLabel(nav_yr, text="Year", font=("Segoe UI Semibold", 13), width=55, text_color=COLOR_TEXT_MAIN)
        self.lbl_year_selector.pack(side="left")
        ctk.CTkButton(nav_yr, text="›", width=32, height=36, corner_radius=10, fg_color="transparent", hover_color=COLOR_HOVER, font=("Segoe UI", 18), command=lambda: self.change_time('year', 1)).pack(side="left", padx=2)

        nw_frame = ctk.CTkFrame(header, fg_color="transparent")
        nw_frame.pack(side="right")
        ctk.CTkLabel(nw_frame, text="NET WORTH", font=("Segoe UI Bold", 10), text_color=COLOR_TEXT_DIM).pack(anchor="e")
        self.lbl_header_nw = ctk.CTkLabel(nw_frame, text="...", font=("Segoe UI Bold", 26), text_color=COLOR_TEXT_MAIN)
        self.lbl_header_nw.pack(anchor="e")
        self.lbl_header_nw_sub = ctk.CTkLabel(nw_frame, text="", font=("Segoe UI", 15), text_color=COLOR_TEXT_SUB)
        self.lbl_header_nw_sub.pack(anchor="e")

        row1 = ctk.CTkFrame(content, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 12))
        row1.grid_columnconfigure((0, 1), weight=1, uniform="r1")
        c_inc, self.lbl_inc_usd, self.lbl_inc_usd_sub, self.lbl_inc_dzd, self.lbl_inc_dzd_sub = self.make_dual_stat_card(row1, "INCOME THIS MONTH", COLOR_SUCCESS, "$0", "≈ 0 DZD", "0 DZD", "≈ $0")
        c_inc.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        c_exp, self.lbl_exp_usd, self.lbl_exp_usd_sub, self.lbl_exp_dzd, self.lbl_exp_dzd_sub = self.make_dual_stat_card(row1, "EXPENSES THIS MONTH", COLOR_DANGER, "$0", "≈ 0 DZD", "0 DZD", "≈ $0")
        c_exp.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        row2 = ctk.CTkFrame(content, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 20))
        row2.grid_columnconfigure((0, 1, 2), weight=1, uniform="r2")
        c_pp, self.lbl_paypal, self.lbl_paypal_sub = self.make_stat_card(row2, "PAYPAL (PENDING)", COLOR_WARNING, "$0", "≈ 0 DZD")
        c_pp.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        c_bk, self.lbl_usd_savings, self.lbl_usd_savings_sub = self.make_stat_card(row2, "BANK (AVAILABLE)", COLOR_PRIMARY, "$0", "≈ 0 DZD")
        c_bk.grid(row=0, column=1, sticky="ew", padx=6)
        c_lc, self.lbl_dzd_cash, self.lbl_dzd_cash_sub = self.make_stat_card(row2, "LOCAL CASH", COLOR_TEXT_MAIN, "0 DZD", "≈ $0")
        c_lc.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        filter_row = ctk.CTkFrame(content, fg_color="transparent")
        filter_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(filter_row, text="Transactions", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(side="left")
        self.dash_filter_sort = ctk.CTkOptionMenu(filter_row, values=["Newest First", "Oldest First", "Highest Amount", "Lowest Amount"], fg_color=COLOR_INPUT, button_color=COLOR_INPUT, button_hover_color=COLOR_HOVER, corner_radius=10, height=32, font=FONT_TINY, command=lambda _: self.update_dashboard_history())
        self.dash_filter_sort.pack(side="right", padx=(8, 0))
        self.dash_filter_type = ctk.CTkOptionMenu(filter_row, values=["All Types", "Income", "Expense", "Transfer", "Savings", "Loan"], fg_color=COLOR_INPUT, button_color=COLOR_INPUT, button_hover_color=COLOR_HOVER, corner_radius=10, height=32, font=FONT_TINY, command=lambda _: self.update_dashboard_history())
        self.dash_filter_type.pack(side="right")

        self.dashboard_history_frame = ctk.CTkScrollableFrame(frame, fg_color="transparent", scrollbar_button_color=COLOR_BORDER, scrollbar_button_hover_color=COLOR_TEXT_SUB)
        self.dashboard_history_frame.grid(row=4, column=0, sticky="nsew", padx=30, pady=(0, 20))

    def update_dashboard_history(self):
        for w in self.dashboard_history_frame.winfo_children():
            w.destroy()
        target = self.get_monthly_key()
        f_type = self.dash_filter_type.get()
        f_sort = self.dash_filter_sort.get()
        filtered = []
        for t in self.data["transactions"]:
            td = str(t.get('date', ''))
            tv = str(t.get('type', ''))
            if not td.startswith(target):
                continue
            match = (f_type == "All Types" or
                     (f_type == "Income" and tv == 'income') or
                     (f_type == "Expense" and tv == 'expense') or
                     (f_type == "Transfer" and 'transfer' in tv) or
                     (f_type == "Savings" and 'savings' in tv) or
                     (f_type == "Loan" and tv in ('loan_out', 'loan_repaid')))
            if match:
                filtered.append(t)

        def sa(x):
            try:
                return float(x.get('amount', x.get('amount_usd', x.get('amount_sent', 0))))
            except (ValueError, TypeError):
                return 0.0

        if f_sort == "Newest First":
            filtered.sort(key=lambda x: str(x.get('date', '')), reverse=True)
        elif f_sort == "Oldest First":
            filtered.sort(key=lambda x: str(x.get('date', '')))
        elif f_sort == "Highest Amount":
            filtered.sort(key=sa, reverse=True)
        elif f_sort == "Lowest Amount":
            filtered.sort(key=sa)

        if not filtered:
            ctk.CTkLabel(self.dashboard_history_frame, text="No transactions this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=40)
        else:
            for t in filtered:
                self.create_list_row(self.dashboard_history_frame, t, simple=True)

    # ================================================================
    # REFRESH
    # ================================================================
    def refresh_ui(self):
        try:
            st = self.calculate_stats()
            dr = self.get_disp_rate()

            self.lbl_paypal.configure(text=f"${st['paypal_balance']:,.2f}")
            self.lbl_paypal_sub.configure(text=f"≈ {st['paypal_balance'] * dr:,.0f} DZD")
            self.lbl_usd_savings.configure(text=f"${st['usd_savings']:,.2f}")
            self.lbl_usd_savings_sub.configure(text=f"≈ {st['usd_savings'] * dr:,.0f} DZD")
            self.lbl_dzd_cash.configure(text=f"{st['dzd_cash']:,.2f} DZD")
            self.lbl_dzd_cash_sub.configure(text=f"≈ ${st['dzd_cash'] / dr:,.2f}")

            self.lbl_inc_usd.configure(text=f"+ ${st['month_earned_usd']:,.2f}")
            self.lbl_inc_usd_sub.configure(text=f"≈ {st['month_earned_usd'] * dr:,.0f} DZD")
            self.lbl_inc_dzd.configure(text=f"+ {st['month_earned_dzd']:,.0f} DZD")
            self.lbl_inc_dzd_sub.configure(text=f"≈ ${st['month_earned_dzd'] / dr:,.2f}" if dr > 0 else "")

            self.lbl_exp_usd.configure(text=f"${st['month_spent_usd']:,.2f}")
            self.lbl_exp_usd_sub.configure(text=f"≈ {st['month_spent_usd'] * dr:,.0f} DZD")
            self.lbl_exp_dzd.configure(text=f"{st['month_spent_dzd']:,.0f} DZD")
            self.lbl_exp_dzd_sub.configure(text=f"≈ ${st['month_spent_dzd'] / dr:,.2f}" if dr > 0 else "")

            total_usd = st['usd_savings'] + st['paypal_balance'] + st['usd_locked']
            total_dzd = st['dzd_cash'] + st['dzd_locked']
            nw_usd = total_usd + (total_dzd / dr)
            nw_dzd = total_dzd + (total_usd * dr)
            self.lbl_header_nw.configure(text=f"${nw_usd:,.2f}")
            self.lbl_header_nw_sub.configure(text=f"≈ {nw_dzd:,.0f} DZD")
            self.lbl_month_selector.configure(text=datetime(self.selected_year, self.selected_month, 1).strftime('%B'))
            self.lbl_year_selector.configure(text=str(self.selected_year))

            if "savings" in self.frames:
                self.lbl_vault_usd.configure(text=f"${st['usd_locked']:,.2f}")
                self.lbl_vault_usd_sub.configure(text=f"≈ {st['usd_locked'] * dr:,.0f} DZD")
                self.lbl_vault_dzd.configure(text=f"{st['dzd_locked']:,.2f} DZD")
                self.lbl_vault_dzd_sub.configure(text=f"≈ ${st['dzd_locked'] / dr:,.2f}")

            if "lending" in self.frames:
                self.lbl_lent_usd.configure(text=f"${st['total_lent_usd']:,.2f}")
                self.lbl_lent_usd_sub.configure(text=f"≈ {st['total_lent_usd'] * dr:,.0f} DZD")
                self.lbl_lent_dzd.configure(text=f"{st['total_lent_dzd']:,.2f} DZD")
                self.lbl_lent_dzd_sub.configure(text=f"≈ ${st['total_lent_dzd'] / dr:,.2f}")

            self.update_income_list()
            self.update_expense_list()
            self.update_transfer_list()
            self.update_savings_list()
            self.update_dashboard_history()
            self.update_lending_list()
        except Exception as e:
            messagebox.showerror("Refresh Error", f"{e}\n\n{traceback.format_exc()}")

    def change_time(self, unit, direction):
        if unit == 'month':
            if direction == 1:
                if self.selected_month == 12:
                    self.selected_month = 1
                    self.selected_year += 1
                else:
                    self.selected_month += 1
            else:
                if self.selected_month == 1:
                    self.selected_month = 12
                    self.selected_year -= 1
                else:
                    self.selected_month -= 1
        elif unit == 'year':
            self.selected_year += direction
        self._cached_stats = None
        self.refresh_ui()

    # ================================================================
    # FORM HELPERS
    # ================================================================
    def create_form_container(self, parent, title):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="w", padx=30, pady=(25, 15))
        ctk.CTkLabel(hdr, text=title, font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        c = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        c.grid(row=1, column=0, sticky="new", padx=30, pady=(0, 10))
        inner = ctk.CTkFrame(c, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=24)
        inner.grid_columnconfigure((0, 1), weight=1)
        return inner

    def create_input(self, p, ph):
        return ctk.CTkEntry(p, height=44, corner_radius=12, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", placeholder_text=ph, placeholder_text_color=COLOR_TEXT_DIM)

    def create_textbox(self, p, ph):
        tb = ctk.CTkTextbox(p, height=80, corner_radius=12, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, text_color="white", font=FONT_MAIN)
        return tb

    def on_income_curr_change(self, choice):
        if choice == "DZD (Cash)":
            self.combo_fee_type.set("No Fee")
            self.combo_fee_type.configure(state="disabled")
            self.entry_fee_val.configure(state="disabled")
            self.chk_paypal_var.set("off")
            self.chk_paypal.configure(state="disabled")
        else:
            self.combo_fee_type.configure(state="normal")
            self.entry_fee_val.configure(state="disabled")
            self.chk_paypal.configure(state="normal")

    # ================================================================
    # INCOME FRAME
    # ================================================================
    def create_income_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["income"] = f
        form = self.create_form_container(f, "Add Income")
        self.entry_inc_name = self.create_input(form, "Source Name")
        self.entry_inc_name.grid(row=0, column=0, padx=(0, 6), pady=6, sticky="ew")
        self.entry_inc_amount = self.create_input(form, "Gross Amount")
        self.entry_inc_amount.grid(row=0, column=1, padx=(6, 0), pady=6, sticky="ew")
        self.combo_inc_curr = ctk.CTkComboBox(form, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["USD (Online)", "DZD (Cash)"], command=self.on_income_curr_change)
        self.combo_inc_curr.grid(row=1, column=0, columnspan=2, pady=6, sticky="ew")
        self.combo_fee_type = ctk.CTkComboBox(form, height=44, corner_radius=12, border_width=1, border_color=COLOR_BORDER, fg_color=COLOR_INPUT, values=["No Fee", "Upwork (10%)", "Transaction Fee (Manual)"], command=lambda c: self.entry_fee_val.configure(state="normal" if "Manual" in c else "disabled"))
        self.combo_fee_type.grid(row=2, column=0, padx=(0, 6), pady=6, sticky="ew")
        self.entry_fee_val = self.create_input(form, "Fee Amount ($)")
        self.entry_fee_val.grid(row=2, column=1, padx=(6, 0), pady=6, sticky="ew")
        self.entry_fee_val.configure(state="disabled")
        self.chk_paypal_var = ctk.StringVar(value="on")
        self.chk_paypal = ctk.CTkCheckBox(form, text="Add to PayPal Balance", variable=self.chk_paypal_var, onvalue="on", offvalue="off", font=FONT_MAIN, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, border_color=COLOR_BORDER)
        self.chk_paypal.grid(row=3, column=0, columnspan=2, pady=12, sticky="w")
        ctk.CTkButton(form, text="Add Income", height=46, corner_radius=12, fg_color=COLOR_SUCCESS, hover_color=COLOR_SUCCESS_DIM, font=FONT_BOLD, command=self.add_income).grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="ew")
        ctk.CTkLabel(f, text="History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=2, column=0, sticky="w", padx=30, pady=(20, 8))
        self.income_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER)
        self.income_list_frame.grid(row=3, column=0, sticky="nsew", padx=30, pady=(0, 20))

    # ================================================================
    # TRANSFER FRAME
    # ================================================================
    def fill_max_paypal(self):
        b = self.calculate_stats()['paypal_balance']
        self.entry_pp_amount.delete(0, 'end')
        self.entry_pp_amount.insert(0, str(round(b, 2)))

    def create_transfer_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["transfer"] = f
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.pack(fill="x", padx=30, pady=(25, 15))
        ctk.CTkLabel(hdr, text="Transfers", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        tv = ctk.CTkTabview(f, fg_color=COLOR_CARD, segmented_button_fg_color=COLOR_INPUT, segmented_button_selected_color=COLOR_PRIMARY, segmented_button_unselected_color=COLOR_INPUT, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        tv.pack(fill="x", padx=30)
        t1 = tv.add("PayPal → Bank")
        t2 = tv.add("Sell USD → DZD")

        c1 = ctk.CTkFrame(t1, fg_color="transparent")
        c1.pack(fill="x", padx=20, pady=10)
        pp_row = ctk.CTkFrame(c1, fg_color="transparent")
        pp_row.pack(fill="x", pady=6)
        self.entry_pp_amount = self.create_input(pp_row, "Amount ($)")
        self.entry_pp_amount.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(pp_row, text="MAX", width=60, height=44, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, border_width=1, border_color=COLOR_BORDER, font=FONT_TINY, command=self.fill_max_paypal).pack(side="right")
        self.combo_pp_method = ctk.CTkComboBox(c1, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["Automatic (Free)", "Manual ($5 Fee)"])
        self.combo_pp_method.pack(fill="x", pady=6)
        ctk.CTkButton(c1, text="Process Transfer", height=46, corner_radius=12, fg_color=COLOR_WARNING, hover_color=COLOR_WARNING_DIM, font=FONT_BOLD, text_color="#000", command=self.transfer_paypal_to_bank).pack(fill="x", pady=(12, 6))

        c2 = ctk.CTkFrame(t2, fg_color="transparent")
        c2.pack(fill="x", padx=20, pady=10)
        self.entry_ex_usd = self.create_input(c2, "Amount ($)")
        self.entry_ex_usd.pack(fill="x", pady=6)
        self.entry_ex_rate = self.create_input(c2, "Rate (1 USD = ? DZD)")
        self.entry_ex_rate.pack(fill="x", pady=6)
        ctk.CTkButton(c2, text="Confirm Sale", height=46, corner_radius=12, fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DIM, font=FONT_BOLD, command=self.transfer_usd_to_dzd).pack(fill="x", pady=(12, 6))

        ctk.CTkLabel(f, text="Transfer History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w", padx=30, pady=(20, 8))
        self.transfer_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER)
        self.transfer_list_frame.pack(fill="both", expand=True, padx=30, pady=(0, 20))

    # ================================================================
    # EXPENSES FRAME
    # ================================================================
    def create_expenses_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["expenses"] = f
        form = self.create_form_container(f, "Record Expense")
        self.entry_exp_desc = self.create_input(form, "Description")
        self.entry_exp_desc.grid(row=0, column=0, padx=(0, 6), pady=6, sticky="ew")
        self.entry_exp_amount = self.create_input(form, "Amount")
        self.entry_exp_amount.grid(row=0, column=1, padx=(6, 0), pady=6, sticky="ew")
        self.combo_exp_cat = ctk.CTkComboBox(form, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["Essentials", "Debt", "Luxury", "Business", "Other"])
        self.combo_exp_cat.grid(row=1, column=0, padx=(0, 6), pady=6, sticky="ew")
        self.combo_exp_curr = ctk.CTkComboBox(form, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["DZD (Cash)", "USD (Online)"])
        self.combo_exp_curr.grid(row=1, column=1, padx=(6, 0), pady=6, sticky="ew")
        ctk.CTkButton(form, text="Record Expense", height=46, corner_radius=12, fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_DIM, font=FONT_BOLD, command=self.add_expense).grid(row=2, column=0, columnspan=2, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(f, text="Recent", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=2, column=0, sticky="w", padx=30, pady=(20, 8))
        self.expense_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER)
        self.expense_list_frame.grid(row=3, column=0, sticky="nsew", padx=30, pady=(0, 20))

    # ================================================================
    # SAVINGS FRAME
    # ================================================================
    def create_savings_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["savings"] = f
        f.grid_columnconfigure((0, 1), weight=1)
        f.grid_rowconfigure(4, weight=1)

        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=2, sticky="w", padx=30, pady=(25, 15))
        ctk.CTkLabel(hdr, text="Savings Vault", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w")

        row0 = ctk.CTkFrame(f, fg_color="transparent")
        row0.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 15))
        row0.grid_columnconfigure((0, 1), weight=1, uniform="sv")
        c1, self.lbl_vault_usd, self.lbl_vault_usd_sub = self.make_stat_card(row0, "USD VAULT (LOCKED)", COLOR_SAVINGS, "$0.00", "≈ 0 DZD")
        c1.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        c2, self.lbl_vault_dzd, self.lbl_vault_dzd_sub = self.make_stat_card(row0, "DZD VAULT (LOCKED)", COLOR_SAVINGS, "0 DZD", "≈ $0")
        c2.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        form_card = ctk.CTkFrame(f, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        form_card.grid(row=2, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 10))
        form = ctk.CTkFrame(form_card, fg_color="transparent")
        form.pack(fill="x", padx=24, pady=20)
        form.grid_columnconfigure((0, 1), weight=1)
        self.combo_sav_action = ctk.CTkComboBox(form, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["Lock into Savings", "Withdraw to Available"])
        self.combo_sav_action.grid(row=0, column=0, padx=(0, 6), pady=6, sticky="ew")
        self.combo_sav_curr = ctk.CTkComboBox(form, height=44, corner_radius=12, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, values=["USD", "DZD"])
        self.combo_sav_curr.grid(row=0, column=1, padx=(6, 0), pady=6, sticky="ew")
        self.entry_sav_amount = self.create_input(form, "Amount")
        self.entry_sav_amount.grid(row=1, column=0, columnspan=2, pady=6, sticky="ew")
        ctk.CTkButton(form, text="Confirm", height=46, corner_radius=12, fg_color=COLOR_SAVINGS, hover_color=COLOR_SAVINGS_DIM, font=FONT_BOLD, command=self.manage_savings).grid(row=2, column=0, columnspan=2, pady=(8, 0), sticky="ew")

        ctk.CTkLabel(f, text="Savings History", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=3, column=0, columnspan=2, sticky="w", padx=30, pady=(20, 8))
        self.savings_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER)
        self.savings_list_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=30, pady=(0, 20))

    # ================================================================
    # LENDING FRAME
    # ================================================================
    def create_lending_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.frames["lending"] = f
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(4, weight=1)

        # Header
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="w", padx=30, pady=(25, 15))
        ctk.CTkLabel(hdr, text="Lending Tracker", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        ctk.CTkLabel(hdr, text="Track money you've lent out and mark repayments", font=FONT_SMALL, text_color=COLOR_TEXT_SUB).pack(anchor="w", pady=(2, 0))

        # Summary cards
        summary_row = ctk.CTkFrame(f, fg_color="transparent")
        summary_row.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 15))
        summary_row.grid_columnconfigure((0, 1), weight=1, uniform="ln")

        c_usd, self.lbl_lent_usd, self.lbl_lent_usd_sub = self.make_stat_card(summary_row, "OUTSTANDING USD", COLOR_LOAN, "$0.00", "≈ 0 DZD")
        c_usd.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        c_dzd, self.lbl_lent_dzd, self.lbl_lent_dzd_sub = self.make_stat_card(summary_row, "OUTSTANDING DZD", COLOR_LOAN, "0 DZD", "≈ $0")
        c_dzd.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # Form card
        form_card = ctk.CTkFrame(f, fg_color=COLOR_CARD, corner_radius=18, border_width=1, border_color=COLOR_BORDER)
        form_card.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 10))

        form = ctk.CTkFrame(form_card, fg_color="transparent")
        form.pack(fill="x", padx=24, pady=24)
        form.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(form, text="Record a Loan", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.entry_loan_who = self.create_input(form, "Who are you lending to?")
        self.entry_loan_who.grid(row=1, column=0, padx=(0, 6), pady=6, sticky="ew")

        self.entry_loan_amount = self.create_input(form, "Amount")
        self.entry_loan_amount.grid(row=1, column=1, padx=(6, 0), pady=6, sticky="ew")

        self.combo_loan_curr = ctk.CTkComboBox(
            form, height=44, corner_radius=12, fg_color=COLOR_INPUT,
            border_width=1, border_color=COLOR_BORDER,
            values=["USD", "DZD"]
        )
        self.combo_loan_curr.grid(row=2, column=0, columnspan=2, pady=6, sticky="ew")

        ctk.CTkLabel(form, text="Notes (optional — when, how, context)", font=FONT_TINY, text_color=COLOR_TEXT_SUB).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))

        self.txt_loan_notes = self.create_textbox(form, "")
        self.txt_loan_notes.grid(row=4, column=0, columnspan=2, pady=(0, 6), sticky="ew")

        ctk.CTkButton(
            form, text="Record Loan", height=46, corner_radius=12,
            fg_color=COLOR_LOAN, hover_color=COLOR_LOAN_DIM,
            font=FONT_BOLD, text_color="#000", command=self.add_loan
        ).grid(row=5, column=0, columnspan=2, pady=(8, 0), sticky="ew")

        # Active loans header + filter
        list_header = ctk.CTkFrame(f, fg_color="transparent")
        list_header.grid(row=3, column=0, sticky="ew", padx=30, pady=(20, 8))
        ctk.CTkLabel(list_header, text="Loans", font=FONT_SUBHEADER, text_color=COLOR_TEXT_MAIN).pack(side="left")

        self.combo_loan_filter = ctk.CTkOptionMenu(
            list_header, values=["Active", "Repaid", "All"],
            fg_color=COLOR_INPUT, button_color=COLOR_INPUT,
            button_hover_color=COLOR_HOVER, corner_radius=10,
            height=32, font=FONT_TINY,
            command=lambda _: self.update_lending_list()
        )
        self.combo_loan_filter.pack(side="right")

        # Loan list
        self.lending_list_frame = ctk.CTkScrollableFrame(
            f, fg_color="transparent", scrollbar_button_color=COLOR_BORDER
        )
        self.lending_list_frame.grid(row=4, column=0, sticky="nsew", padx=30, pady=(0, 20))

    # ================================================================
    # LENDING ACTIONS
    # ================================================================
    def add_loan(self):
        who = self.entry_loan_who.get().strip()
        if not who:
            self.show_error_native("Enter who you're lending to.")
            return
        try:
            amt = float(self.entry_loan_amount.get())
            if amt <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive amount.")
            return

        curr = self.combo_loan_curr.get()
        notes = self.txt_loan_notes.get("1.0", "end-1c").strip()

        # Check balance
        self._cached_stats = None
        s = self.calculate_stats()
        if curr == "USD" and s['usd_savings'] < amt:
            self.show_error_native(f"Insufficient Bank USD.\nAvailable: ${s['usd_savings']:,.2f}")
            return
        if curr == "DZD" and s['dzd_cash'] < amt:
            self.show_error_native(f"Insufficient DZD cash.\nAvailable: {s['dzd_cash']:,.2f} DZD")
            return

        t = {
            "id": str(uuid.uuid4()),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "loan_out",
            "borrower": who,
            "amount": amt,
            "currency": curr,
            "notes": notes,
            "status": "active",
        }
        if self.add_transaction_to_db(t):
            self.entry_loan_who.delete(0, 'end')
            self.entry_loan_amount.delete(0, 'end')
            self.txt_loan_notes.delete("1.0", "end")
            self.show_success_native(f"Loan to {who} recorded.")

    def mark_loan_repaid(self, loan_t):
        """Mark an active loan as repaid — creates a loan_repaid transaction and updates the original."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Confirm Repayment")
        dialog.geometry("400x220")
        dialog.configure(fg_color=COLOR_CARD)
        dialog.attributes('-topmost', True)

        dr = self.get_disp_rate()
        amt = loan_t.get('amount', 0)
        curr = loan_t.get('currency', 'USD')
        who = loan_t.get('borrower', '?')

        if curr == "USD":
            amt_str = f"${amt:,.2f} (≈ {amt * dr:,.0f} DZD)"
        else:
            amt_str = f"{amt:,.2f} DZD (≈ ${amt / dr:,.2f})"

        ctk.CTkLabel(dialog, text="Mark as Repaid?", font=("Segoe UI Bold", 18), text_color=COLOR_TEXT_MAIN).pack(pady=(20, 5))
        ctk.CTkLabel(dialog, text=f"{who} owes you {amt_str}", font=FONT_MAIN, text_color=COLOR_TEXT_SUB, wraplength=350).pack(pady=(0, 5))
        ctk.CTkLabel(dialog, text="This will return the funds to your available balance.", font=FONT_TINY, text_color=COLOR_TEXT_DIM).pack(pady=(0, 15))

        def confirm():
            dialog.destroy()
            # Create a repaid transaction
            repaid_t = {
                "id": str(uuid.uuid4()),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "loan_repaid",
                "borrower": who,
                "amount": amt,
                "currency": curr,
                "original_loan_id": loan_t['id'],
                "notes": f"Repayment from {who}",
            }
            if self.add_transaction_to_db(repaid_t):
                # Update the original loan status
                updated_loan = dict(loan_t)
                updated_loan['status'] = 'repaid'
                updated_loan['repaid_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.update_transaction_in_db(updated_loan)
                self.show_success_native(f"{who} marked as repaid.")

        bf = ctk.CTkFrame(dialog, fg_color="transparent")
        bf.pack(pady=10)
        ctk.CTkButton(bf, text="Cancel", width=120, height=40, corner_radius=12, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=dialog.destroy).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="Confirm Repaid", width=140, height=40, corner_radius=12, fg_color=COLOR_SUCCESS, hover_color=COLOR_SUCCESS_DIM, font=FONT_BOLD, command=confirm).pack(side="right", padx=8)

    def update_lending_list(self):
        if not hasattr(self, 'lending_list_frame'):
            return
        for w in self.lending_list_frame.winfo_children():
            w.destroy()

        filter_val = self.combo_loan_filter.get()
        dr = self.get_disp_rate()

        # Gather all loan_out transactions
        loans = [t for t in self.data.get("transactions", []) if t.get('type') == 'loan_out']

        # Filter
        if filter_val == "Active":
            loans = [l for l in loans if l.get('status', 'active') == 'active']
        elif filter_val == "Repaid":
            loans = [l for l in loans if l.get('status') == 'repaid']

        loans.sort(key=lambda x: str(x.get('date', '')), reverse=True)

        if not loans:
            msg = "No active loans." if filter_val == "Active" else ("No repaid loans." if filter_val == "Repaid" else "No loans recorded yet.")
            ctk.CTkLabel(self.lending_list_frame, text=msg, font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)
            return

        # Group by borrower for active loans
        if filter_val == "Active":
            borrower_totals = {}
            for l in loans:
                b = l.get('borrower', '?')
                c = l.get('currency', 'USD')
                a = l.get('amount', 0)
                key = b
                if key not in borrower_totals:
                    borrower_totals[key] = {"usd": 0.0, "dzd": 0.0}
                if c == "USD":
                    borrower_totals[key]["usd"] += a
                else:
                    borrower_totals[key]["dzd"] += a

            # Show borrower summary badges
            if borrower_totals:
                summary_frame = ctk.CTkFrame(self.lending_list_frame, fg_color="transparent")
                summary_frame.pack(fill="x", pady=(0, 12))

                for borrower, totals in borrower_totals.items():
                    badge = ctk.CTkFrame(summary_frame, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color=COLOR_LOAN)
                    badge.pack(side="left", padx=(0, 8), pady=2)
                    inner = ctk.CTkFrame(badge, fg_color="transparent")
                    inner.pack(padx=12, pady=8)
                    parts = []
                    if totals["usd"] > 0:
                        parts.append(f"${totals['usd']:,.2f}")
                    if totals["dzd"] > 0:
                        parts.append(f"{totals['dzd']:,.0f} DZD")
                    ctk.CTkLabel(inner, text=borrower, font=("Segoe UI Bold", 12), text_color=COLOR_LOAN).pack(side="left", padx=(0, 8))
                    ctk.CTkLabel(inner, text=" + ".join(parts), font=("Segoe UI", 12), text_color=COLOR_TEXT_MAIN).pack(side="left")

        # Individual loan rows
        for loan in loans:
            self.create_loan_row(self.lending_list_frame, loan)

    def create_loan_row(self, parent, t):
        """Render a single loan row with status, details, and repaid button."""
        is_active = t.get('status', 'active') == 'active'
        dr = self.get_disp_rate()
        amt = t.get('amount', 0)
        curr = t.get('currency', 'USD')
        who = t.get('borrower', '?')
        notes = t.get('notes', '')
        t_date = str(t.get('date', ''))[:10]
        repaid_date = str(t.get('repaid_date', ''))[:10] if t.get('repaid_date') else None

        outer = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_LOAN if is_active else COLOR_BORDER)
        outer.pack(fill="x", pady=4)

        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="x", padx=0, pady=0)

        # Status dot
        dot_frame = ctk.CTkFrame(inner, fg_color="transparent", width=20)
        dot_frame.pack(side="left", padx=(14, 0), pady=14)
        dot_color = COLOR_LOAN if is_active else COLOR_SUCCESS
        ctk.CTkFrame(dot_frame, width=10, height=10, corner_radius=5, fg_color=dot_color).pack()

        # Text block
        tf = ctk.CTkFrame(inner, fg_color="transparent")
        tf.pack(side="left", padx=(10, 10), pady=12, fill="x", expand=True)

        # Name + status badge
        name_row = ctk.CTkFrame(tf, fg_color="transparent")
        name_row.pack(anchor="w")
        ctk.CTkLabel(name_row, text=who, font=("Segoe UI Bold", 15), text_color=COLOR_TEXT_MAIN).pack(side="left")

        if is_active:
            badge = ctk.CTkFrame(name_row, fg_color=COLOR_LOAN, corner_radius=6)
            badge.pack(side="left", padx=(10, 0))
            ctk.CTkLabel(badge, text="ACTIVE", font=("Segoe UI Bold", 9), text_color="#000").pack(padx=8, pady=2)
        else:
            badge = ctk.CTkFrame(name_row, fg_color=COLOR_SUCCESS, corner_radius=6)
            badge.pack(side="left", padx=(10, 0))
            ctk.CTkLabel(badge, text="REPAID", font=("Segoe UI Bold", 9), text_color="#000").pack(padx=8, pady=2)

        # Date line
        date_txt = f"Lent on {t_date}"
        if repaid_date:
            date_txt += f"  •  Repaid {repaid_date}"
        ctk.CTkLabel(tf, text=date_txt, font=("Segoe UI", 12), text_color=COLOR_TEXT_SUB).pack(anchor="w")

        # Notes
        if notes:
            ctk.CTkLabel(tf, text=notes, font=("Segoe UI", 12), text_color=COLOR_TEXT_DIM, wraplength=500, justify="left").pack(anchor="w", pady=(4, 0))

        # Right side: amount + actions
        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="right", padx=14, pady=12)

        if curr == "USD":
            ctk.CTkLabel(right, text=f"${amt:,.2f}", font=("Segoe UI Bold", 16), text_color=COLOR_LOAN if is_active else COLOR_TEXT_SUB).pack(anchor="e")
            ctk.CTkLabel(right, text=f"≈ {amt * dr:,.0f} DZD", font=("Segoe UI", 13), text_color=COLOR_TEXT_SUB).pack(anchor="e")
        else:
            ctk.CTkLabel(right, text=f"{amt:,.2f} DZD", font=("Segoe UI Bold", 16), text_color=COLOR_LOAN if is_active else COLOR_TEXT_SUB).pack(anchor="e")
            ctk.CTkLabel(right, text=f"≈ ${amt / dr:,.2f}", font=("Segoe UI", 13), text_color=COLOR_TEXT_SUB).pack(anchor="e")

        if is_active:
            ctk.CTkButton(
                right, text="✓ Mark Repaid", width=120, height=32, corner_radius=10,
                fg_color=COLOR_SUCCESS, hover_color=COLOR_SUCCESS_DIM,
                font=("Segoe UI Semibold", 11), text_color="#000",
                command=lambda loan=t: self.mark_loan_repaid(loan)
            ).pack(anchor="e", pady=(8, 0))

    # ================================================================
    # ACTIONS
    # ================================================================
    def add_income(self):
        name = self.entry_inc_name.get().strip()
        if not name:
            self.show_error_native("Enter a source name.")
            return
        try:
            val = float(self.entry_inc_amount.get())
            if val <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive amount.")
            return
        curr = "DZD" if "DZD" in self.combo_inc_curr.get() else "USD"
        if curr == "DZD":
            fee, fee_type, to_paypal = 0.0, "No Fee", False
        else:
            fee_type = self.combo_fee_type.get()
            if "Manual" in fee_type:
                try:
                    fee = float(self.entry_fee_val.get())
                    if fee < 0:
                        raise ValueError
                except ValueError:
                    self.show_error_native("Enter a valid fee.")
                    return
            elif "Upwork" in fee_type:
                fee = val * 0.1
            else:
                fee = 0
            to_paypal = self.chk_paypal_var.get() == "on"
        if fee > val:
            self.show_error_native("Fee exceeds income.")
            return
        t = {"id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "income", "category": name, "amount": val, "currency": curr, "fee_type": fee_type, "fee_amount": fee, "net_amount": val - fee, "to_paypal": to_paypal}
        if self.add_transaction_to_db(t):
            self.entry_inc_name.delete(0, 'end')
            self.entry_inc_amount.delete(0, 'end')
            self.entry_fee_val.configure(state="normal")
            self.entry_fee_val.delete(0, 'end')
            self.entry_fee_val.configure(state="disabled")
            self.show_success_native("Income added.")

    def transfer_paypal_to_bank(self):
        try:
            amt = float(self.entry_pp_amount.get())
            if amt <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid amount.")
            return
        fee = 5.0 if "Manual" in self.combo_pp_method.get() else 0
        self._cached_stats = None
        st = self.calculate_stats()
        if st['paypal_balance'] < amt:
            self.show_error_native(f"Insufficient PayPal.\nAvailable: ${st['paypal_balance']:,.2f}")
            return
        if amt - fee <= 0:
            self.show_error_native("Amount after fee is zero or negative.")
            return
        t = {"id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "transfer_paypal_bank", "amount_sent": amt, "fee_paid": fee, "amount_received": amt - fee}
        if self.add_transaction_to_db(t):
            self.entry_pp_amount.delete(0, 'end')
            self.show_success_native("Transfer complete.")

    def transfer_usd_to_dzd(self):
        try:
            usd = float(self.entry_ex_usd.get())
            rate = float(self.entry_ex_rate.get())
            if usd <= 0 or rate <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter valid positive numbers.")
            return
        self._cached_stats = None
        st = self.calculate_stats()
        if st['usd_savings'] < usd:
            self.show_error_native(f"Insufficient Bank USD.\nAvailable: ${st['usd_savings']:,.2f}")
            return
        t = {"id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "transfer_usd_dzd", "amount_usd": usd, "rate": rate, "amount_dzd": usd * rate}
        if self.add_transaction_to_db(t):
            self.entry_ex_usd.delete(0, 'end')
            self.entry_ex_rate.delete(0, 'end')
            self.show_success_native("Exchange complete.")

    def add_expense(self):
        desc = self.entry_exp_desc.get().strip()
        if not desc:
            self.show_error_native("Enter a description.")
            return
        try:
            amt = float(self.entry_exp_amount.get())
            if amt <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive amount.")
            return
        curr = "USD" if "USD" in self.combo_exp_curr.get() else "DZD"
        self._cached_stats = None
        s = self.calculate_stats()
        if curr == "USD" and s['usd_savings'] < amt:
            self.show_error_native(f"Insufficient Bank USD.\nAvailable: ${s['usd_savings']:,.2f}")
            return
        if curr == "DZD" and s['dzd_cash'] < amt:
            self.show_error_native(f"Insufficient DZD.\nAvailable: {s['dzd_cash']:,.2f} DZD")
            return
        t = {"id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "expense", "category": f"{self.combo_exp_cat.get()} - {desc}", "amount": amt, "currency": curr}
        if self.add_transaction_to_db(t):
            self.entry_exp_desc.delete(0, 'end')
            self.entry_exp_amount.delete(0, 'end')
            self.show_success_native("Expense recorded.")

    def manage_savings(self):
        try:
            amt = float(self.entry_sav_amount.get())
            if amt <= 0:
                raise ValueError
        except ValueError:
            self.show_error_native("Enter a valid positive amount.")
            return
        curr = self.combo_sav_curr.get()
        action = self.combo_sav_action.get()
        t_type = 'savings_deposit' if 'Lock' in action else 'savings_withdraw'
        self._cached_stats = None
        s = self.calculate_stats()
        if t_type == 'savings_deposit':
            if curr == 'USD' and s['usd_savings'] < amt:
                self.show_error_native(f"Insufficient Bank USD.\nAvailable: ${s['usd_savings']:,.2f}")
                return
            if curr == 'DZD' and s['dzd_cash'] < amt:
                self.show_error_native(f"Insufficient DZD.\nAvailable: {s['dzd_cash']:,.2f} DZD")
                return
        else:
            if curr == 'USD' and s['usd_locked'] < amt:
                self.show_error_native(f"Insufficient locked USD.\nLocked: ${s['usd_locked']:,.2f}")
                return
            if curr == 'DZD' and s['dzd_locked'] < amt:
                self.show_error_native(f"Insufficient locked DZD.\nLocked: {s['dzd_locked']:,.2f} DZD")
                return
        t = {"id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": t_type, "amount": amt, "currency": curr}
        if self.add_transaction_to_db(t):
            self.entry_sav_amount.delete(0, 'end')
            self.show_success_native("Vault updated.")

    # ================================================================
    # LIST ROW (general transactions)
    # ================================================================
    def create_list_row(self, parent, t, simple=False):
        t_type = t.get('type', 'unknown')
        t_date = str(t.get('date', 'Unknown Date'))
        display_date = t_date[:10] if t_date != 'Unknown Date' else t_date
        dr = self.get_disp_rate()

        def sf(key):
            try:
                return float(t.get(key, 0.0))
            except (ValueError, TypeError):
                return 0.0

        net = sf('net_amount')
        if net == 0.0:
            net = sf('amount')
        base = sf('amount')
        main_txt, sub_txt, amt_txt, amt_sub_txt, col = "", "", "", "", COLOR_TEXT_MAIN

        if t_type == 'income':
            main_txt = str(t.get('category', 'Income'))
            dest = 'Cash' if t.get('currency') == 'DZD' else ('PayPal' if t.get('to_paypal') else 'Bank')
            sub_txt = f"{display_date}  •  {dest}"
            if t.get('currency') == 'DZD':
                amt_txt = f"+ {net:,.2f} DZD"
                amt_sub_txt = f"≈ ${net / dr:,.2f}"
            else:
                amt_txt = f"+ ${net:,.2f}"
                amt_sub_txt = f"≈ {net * dr:,.0f} DZD"
            col = COLOR_SUCCESS
        elif t_type == 'expense':
            main_txt = str(t.get('category', 'Expense'))
            sub_txt = display_date
            if t.get('currency') == 'USD':
                amt_txt = f"- ${base:,.2f}"
                amt_sub_txt = f"≈ {base * dr:,.0f} DZD"
            else:
                amt_txt = f"- {base:,.2f} DZD"
                amt_sub_txt = f"≈ ${base / dr:,.2f}"
            col = COLOR_DANGER
        elif t_type == 'transfer_usd_dzd':
            main_txt = "Sold USD → DZD"
            sub_txt = f"{display_date}  •  Rate: {t.get('rate', '?')}"
            amt_txt = f"+ {sf('amount_dzd'):,.2f} DZD"
            amt_sub_txt = f"- ${sf('amount_usd'):,.2f}"
            col = COLOR_PRIMARY
        elif t_type == 'transfer_paypal_bank':
            main_txt = "PayPal → Bank"
            sub_txt = f"{display_date}  •  Fee: ${sf('fee_paid'):,.2f}"
            amt_txt = f"+ ${sf('amount_received'):,.2f}"
            amt_sub_txt = f"≈ {sf('amount_received') * dr:,.0f} DZD"
            col = COLOR_WARNING
        elif t_type == 'savings_deposit':
            main_txt = "Locked to Vault"
            sub_txt = display_date
            if t.get('currency') == 'USD':
                amt_txt = f"${base:,.2f}"
                amt_sub_txt = f"≈ {base * dr:,.0f} DZD"
            else:
                amt_txt = f"{base:,.2f} DZD"
                amt_sub_txt = f"≈ ${base / dr:,.2f}"
            col = COLOR_SAVINGS
        elif t_type == 'savings_withdraw':
            main_txt = "Unlocked from Vault"
            sub_txt = display_date
            if t.get('currency') == 'USD':
                amt_txt = f"${base:,.2f}"
                amt_sub_txt = f"≈ {base * dr:,.0f} DZD"
            else:
                amt_txt = f"{base:,.2f} DZD"
                amt_sub_txt = f"≈ ${base / dr:,.2f}"
            col = COLOR_TEXT_SUB
        elif t_type == 'loan_out':
            who = t.get('borrower', '?')
            status = t.get('status', 'active')
            main_txt = f"Lent to {who}"
            sub_txt = f"{display_date}  •  {'Active' if status == 'active' else 'Repaid'}"
            if t.get('currency') == 'USD':
                amt_txt = f"${base:,.2f}"
                amt_sub_txt = f"≈ {base * dr:,.0f} DZD"
            else:
                amt_txt = f"{base:,.2f} DZD"
                amt_sub_txt = f"≈ ${base / dr:,.2f}"
            col = COLOR_LOAN
        elif t_type == 'loan_repaid':
            who = t.get('borrower', '?')
            main_txt = f"Repaid by {who}"
            sub_txt = display_date
            if t.get('currency') == 'USD':
                amt_txt = f"+ ${base:,.2f}"
                amt_sub_txt = f"≈ {base * dr:,.0f} DZD"
            else:
                amt_txt = f"+ {base:,.2f} DZD"
                amt_sub_txt = f"≈ ${base / dr:,.2f}"
            col = COLOR_SUCCESS
        else:
            main_txt = "Legacy Transfer"
            sub_txt = display_date
            amt_txt = "Processed"
            col = COLOR_PRIMARY

        outer = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=12, border_width=1, border_color=COLOR_BORDER)
        outer.pack(fill="x", pady=3)
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="x")
        dot_frame = ctk.CTkFrame(inner, fg_color="transparent", width=20)
        dot_frame.pack(side="left", padx=(12, 0), pady=14)
        ctk.CTkFrame(dot_frame, width=8, height=8, corner_radius=4, fg_color=col).pack()
        tf = ctk.CTkFrame(inner, fg_color="transparent")
        tf.pack(side="left", padx=(8, 10), pady=10)
        ctk.CTkLabel(tf, text=main_txt, font=("Segoe UI Semibold", 14), text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        ctk.CTkLabel(tf, text=sub_txt, font=("Segoe UI", 12), text_color=COLOR_TEXT_SUB).pack(anchor="w")
        if not simple:
            ctk.CTkButton(inner, text="×", width=28, height=28, corner_radius=8, fg_color="transparent", hover_color=COLOR_DANGER, font=("Segoe UI", 16), text_color=COLOR_TEXT_DIM, command=lambda _id=t.get('id', ''): self.delete_transaction(_id)).pack(side="right", padx=(4, 12))
        af = ctk.CTkFrame(inner, fg_color="transparent")
        af.pack(side="right", padx=12, pady=10)
        ctk.CTkLabel(af, text=amt_txt, font=("Segoe UI Bold", 15), text_color=col).pack(anchor="e")
        if amt_sub_txt:
            ctk.CTkLabel(af, text=amt_sub_txt, font=("Segoe UI", 13), text_color=COLOR_TEXT_SUB).pack(anchor="e")

    # ================================================================
    # LIST UPDATES
    # ================================================================
    def update_income_list(self):
        for w in self.income_list_frame.winfo_children():
            w.destroy()
        mk = self.get_monthly_key()
        found = False
        for t in reversed(self.data.get("transactions", [])):
            if t.get('type') == 'income' and str(t.get('date', '')).startswith(mk):
                self.create_list_row(self.income_list_frame, t)
                found = True
        if not found:
            ctk.CTkLabel(self.income_list_frame, text="No income this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_expense_list(self):
        for w in self.expense_list_frame.winfo_children():
            w.destroy()
        mk = self.get_monthly_key()
        found = False
        for t in reversed(self.data.get("transactions", [])):
            if t.get('type') == 'expense' and str(t.get('date', '')).startswith(mk):
                self.create_list_row(self.expense_list_frame, t)
                found = True
        if not found:
            ctk.CTkLabel(self.expense_list_frame, text="No expenses this month.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_transfer_list(self):
        for w in self.transfer_list_frame.winfo_children():
            w.destroy()
        found = False
        for t in reversed(self.data.get("transactions", [])):
            if 'transfer' in str(t.get('type', '')):
                self.create_list_row(self.transfer_list_frame, t)
                found = True
        if not found:
            ctk.CTkLabel(self.transfer_list_frame, text="No transfers yet.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)

    def update_savings_list(self):
        if not hasattr(self, 'savings_list_frame'):
            return
        for w in self.savings_list_frame.winfo_children():
            w.destroy()
        found = False
        for t in reversed(self.data.get("transactions", [])):
            if 'savings' in str(t.get('type', '')):
                self.create_list_row(self.savings_list_frame, t)
                found = True
        if not found:
            ctk.CTkLabel(self.savings_list_frame, text="No savings yet.", font=FONT_MAIN, text_color=COLOR_TEXT_DIM).pack(pady=30)


if __name__ == "__main__":
    app = FinancialTrackerApp()
    app.mainloop()
