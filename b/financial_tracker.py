import customtkinter as ctk
import tkinter as tk
import json
import os
import uuid
import sys
import psycopg2
from psycopg2.extras import Json
from datetime import datetime

# --- MAC OS .APP FIX ---
if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
if sys.stderr is None: sys.stderr = open(os.devnull, 'w')

# --- Theme Configuration ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

FONT_MAIN = ("Roboto", 13)
FONT_BOLD = ("Roboto Medium", 13)
FONT_HEADER = ("Roboto Medium", 26)
FONT_SUBHEADER = ("Roboto Medium", 18)
FONT_NUMBERS = ("Roboto", 28, "bold")

COLOR_BG = "#111111"         
COLOR_SIDEBAR = "#161616"    
COLOR_CARD = "#1E1E1E"       
COLOR_INPUT = "#2B2B2B"      
COLOR_PRIMARY = "#3B8ED0"    
COLOR_HOVER = "#333333"      
COLOR_SUCCESS = "#2ecc71"    
COLOR_DANGER = "#e74c3c"     
COLOR_WARNING = "#f39c12"    
COLOR_TEXT_MAIN = "#FFFFFF"
COLOR_TEXT_SUB = "#A0A0A0"

class FinancialTrackerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Personal Financial Tracker")
        self.geometry("1280x850")
        self.minsize(1000, 700)
        self.configure(fg_color=COLOR_BG)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.db_url = ""
        self.data = {"settings": {"display_rate": 200.0}, "transactions": []}
        self.config_file = os.path.expanduser("~/finance_tracker_db_config.json")
        
        self.frames = {}
        self.nav_buttons = {}

        # Start the app safely after UI initializes
        self.after(100, self.check_db_connection)

    # --- STARTUP LOGIC ---
    def check_db_connection(self):
        """Checks if we have a saved, working DB connection."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    self.db_url = json.load(f).get("db_url", "").strip()
                
                # Test connection silently
                with psycopg2.connect(self.db_url) as conn:
                    pass 
                
                self.start_full_app()
                return
            except Exception:
                pass # Connection failed or file bad, fall through to setup screen

        self.show_setup_screen()

    def show_setup_screen(self):
        """Builds a native setup screen inside the window instead of a crash-prone popup."""
        # Clear anything currently on screen
        for widget in self.winfo_children():
            widget.destroy()

        self.grid_columnconfigure(0, weight=1) # Center the setup box

        setup_container = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=20)
        setup_container.grid(row=0, column=0, padx=50, pady=50)

        ctk.CTkLabel(setup_container, text="Database Setup", font=FONT_HEADER, text_color=COLOR_TEXT_MAIN).pack(pady=(40, 10))
        ctk.CTkLabel(setup_container, text="Connect your Neon PostgreSQL Database to sync across devices.", font=FONT_MAIN, text_color=COLOR_TEXT_SUB).pack(padx=40, pady=(0, 30))

        self.entry_db_url = ctk.CTkEntry(setup_container, width=500, height=50, corner_radius=25, border_width=0, fg_color=COLOR_INPUT, text_color="white", placeholder_text="postgresql://user:pass@ep-...neon.tech/neondb")
        self.entry_db_url.pack(padx=40, pady=10)

        self.lbl_setup_error = ctk.CTkLabel(setup_container, text="", font=FONT_BOLD, text_color=COLOR_DANGER)
        self.lbl_setup_error.pack(pady=5)

        self.btn_connect = ctk.CTkButton(setup_container, text="Connect & Sync", width=200, height=50, corner_radius=25, font=FONT_BOLD, fg_color=COLOR_PRIMARY, command=self.attempt_connection)
        self.btn_connect.pack(pady=(10, 40))

    def attempt_connection(self):
        """Triggered by the Setup Screen button."""
        url = self.entry_db_url.get().strip()
        if not url:
            self.lbl_setup_error.configure(text="Please enter a valid URL.")
            return

        if "sslmode=require" not in url:
            url += "&sslmode=require" if "?" in url else "?sslmode=require"

        self.lbl_setup_error.configure(text="Connecting... (This may take a few seconds)", text_color=COLOR_WARNING)
        self.update() # Force UI to show loading message

        try:
            with psycopg2.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value FLOAT)")
                    cur.execute("CREATE TABLE IF NOT EXISTS transactions (id VARCHAR(255) PRIMARY KEY, t_date VARCHAR(50), t_type VARCHAR(50), payload JSONB)")
                    cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate', 200.0) ON CONFLICT DO NOTHING")
                conn.commit()
            
            # Save it
            self.db_url = url
            with open(self.config_file, "w") as f:
                json.dump({"db_url": self.db_url}, f)
            
            # Start App
            self.start_full_app()

        except Exception as e:
            self.lbl_setup_error.configure(text="Connection failed. Check your link or internet.", text_color=COLOR_DANGER)
            print(f"DB Error: {e}") # Invisible on Mac, but good practice

    # --- MAIN APP INITIALIZATION ---
    def start_full_app(self):
        """Builds the main UI once connected."""
        # Clear Setup Screen
        for widget in self.winfo_children():
            widget.destroy()

        self.grid_columnconfigure(0, weight=0) # Reset grid for sidebar
        self.grid_columnconfigure(1, weight=1)

        self.current_date = datetime.now()
        self.selected_month = self.current_date.month
        self.selected_year = self.current_date.year

        # Load Data
        self.data = self.fetch_data_from_db()

        # Build UI
        self.sidebar_frame = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=COLOR_SIDEBAR)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(6, weight=1) 
        self.create_sidebar()

        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=30, pady=30)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=1)

        self.create_dashboard_frame()
        self.create_income_frame()
        self.create_transfer_frame()
        self.create_expenses_frame()

        self.show_frame("dashboard")

    # --- DATABASE OPERATIONS ---
    def get_db_connection(self):
        return psycopg2.connect(self.db_url)

    def fetch_data_from_db(self):
        data = {"settings": {"display_rate": 200.0}, "transactions": []}
        try:
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key = 'display_rate'")
                    row = cur.fetchone()
                    if row: data["settings"]["display_rate"] = row[0]
                    cur.execute("SELECT payload FROM transactions ORDER BY t_date ASC")
                    rows = cur.fetchall()
                    data["transactions"] = [r[0] for r in rows]
        except Exception:
            self.show_error_native("Could not fetch data from database.")
        return data

    def add_transaction_to_db(self, t):
        self.data["transactions"].append(t) 
        try:
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO transactions (id, t_date, t_type, payload) VALUES (%s, %s, %s, %s)",
                        (t['id'], t['date'], t['type'], Json(t))
                    )
            self.refresh_ui()
            return True
        except Exception:
            self.show_error_native("Failed to save transaction to database.")
            return False

    def delete_transaction(self, tid):
        # Native CTK dialog substitute for messagebox
        dialog = ctk.CTkToplevel(self)
        dialog.title("Confirm Delete")
        dialog.geometry("300x150")
        dialog.attributes('-topmost', True)
        
        ctk.CTkLabel(dialog, text="Delete this transaction?", font=FONT_BOLD).pack(pady=20)
        
        def confirm():
            dialog.destroy()
            self.data["transactions"] = [t for t in self.data["transactions"] if t.get("id") != tid]
            try:
                with self.get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM transactions WHERE id = %s", (tid,))
                self.refresh_ui()
            except Exception:
                self.show_error_native("Failed to delete from database.")
        
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=10)
        ctk.CTkButton(btn_frame, text="Cancel", width=100, fg_color=COLOR_INPUT, hover_color=COLOR_HOVER, command=dialog.destroy).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Delete", width=100, fg_color=COLOR_DANGER, command=confirm).pack(side="right", padx=10)

    def update_display_rate(self):
        try:
            new_rate = float(self.entry_display_rate.get())
            self.data["settings"]["display_rate"] = new_rate
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE settings SET value = %s WHERE key = 'display_rate'", (new_rate,))
            self.refresh_ui()
            self.show_success_native("Rate Updated successfully.")
        except ValueError:
            self.show_error_native("Invalid Number entered.")
        except Exception:
            self.show_error_native("Failed to update database.")

    def show_error_native(self, msg):
        """Safe error display that won't crash Mac .app"""
        err = ctk.CTkToplevel(self)
        err.title("Error")
        err.geometry("400x150")
        err.attributes('-topmost', True)
        ctk.CTkLabel(err, text=msg, font=FONT_BOLD, text_color=COLOR_DANGER, wraplength=350).pack(pady=30)
        ctk.CTkButton(err, text="OK", width=100, fg_color=COLOR_INPUT, command=err.destroy).pack()

    def show_success_native(self, msg):
        succ = ctk.CTkToplevel(self)
        succ.title("Success")
        succ.geometry("300x150")
        succ.attributes('-topmost', True)
        ctk.CTkLabel(succ, text=msg, font=FONT_BOLD, text_color=COLOR_SUCCESS).pack(pady=30)
        ctk.CTkButton(succ, text="OK", width=100, fg_color=COLOR_PRIMARY, command=succ.destroy).pack()


    # --- UI LAYOUTS ---
    def create_sidebar(self):
        logo_label = ctk.CTkLabel(self.sidebar_frame, text="FINANCE\nTRACKER", font=("Roboto", 22, "bold"), text_color=COLOR_TEXT_MAIN)
        logo_label.grid(row=0, column=0, padx=20, pady=(50, 40), sticky="w")
        buttons = [("Dashboard", "dashboard"), ("Add Income", "income"), ("Transfers", "transfer"), ("Expenses", "expenses")]

        for i, (text, name) in enumerate(buttons):
            btn = ctk.CTkButton(self.sidebar_frame, text=text, height=45, corner_radius=22, font=FONT_BOLD, fg_color="transparent", text_color=COLOR_TEXT_SUB, hover_color=COLOR_HOVER, anchor="w", border_spacing=20, command=lambda n=name: self.show_frame(n))
            btn.grid(row=i+1, column=0, sticky="ew", padx=15, pady=5)
            self.nav_buttons[name] = btn

        settings_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        settings_frame.grid(row=7, column=0, padx=20, pady=30, sticky="ew")
        
        ctk.CTkLabel(settings_frame, text="☁ Synced with Neon DB", font=("Roboto", 11, "bold"), text_color=COLOR_SUCCESS).pack(anchor="w", pady=(0, 15))
        ctk.CTkLabel(settings_frame, text="1 USD = ? DZD", font=("Roboto", 11), text_color=COLOR_TEXT_SUB).pack(anchor="w", pady=(0, 5))
        
        self.entry_display_rate = ctk.CTkEntry(settings_frame, height=35, corner_radius=17, fg_color=COLOR_INPUT, border_width=0, text_color="white", placeholder_text=str(self.data["settings"]["display_rate"]))
        self.entry_display_rate.insert(0, str(self.data["settings"]["display_rate"]))
        self.entry_display_rate.pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(settings_frame, text="Update Rate", height=35, corner_radius=17, fg_color=COLOR_HOVER, hover_color="#444", font=("Roboto", 12), command=self.update_display_rate).pack(fill="x")

    def show_frame(self, name):
        for frame in self.frames.values(): frame.grid_forget()
        self.frames[name].grid(row=0, column=0, sticky="nsew")
        for btn_name, btn in self.nav_buttons.items():
            btn.configure(fg_color=COLOR_HOVER if btn_name == name else "transparent", text_color="white" if btn_name == name else COLOR_TEXT_SUB)
        self.data = self.fetch_data_from_db() # Live refresh
        self.refresh_ui()

    def get_monthly_key(self): return f"{self.selected_year}-{self.selected_month:02d}"

    def calculate_stats(self):
        target_month = self.get_monthly_key()
        stats = {"usd_savings": 0.0, "paypal_balance": 0.0, "dzd_cash": 0.0, "month_earned": 0.0, "month_spent_usd": 0.0, "month_spent_dzd": 0.0}
        for t in self.data["transactions"]:
            if t['type'] == 'income':
                if t.get('to_paypal', False): stats['paypal_balance'] += t['net_amount']
                else: stats['usd_savings'] += t['net_amount']
            elif t['type'] == 'expense':
                if t['currency'] == 'USD': stats['usd_savings'] -= t['amount']
                else: stats['dzd_cash'] -= t['amount']
            elif t['type'] == 'transfer_usd_dzd':
                stats['usd_savings'] -= t['amount_usd']; stats['dzd_cash'] += t['amount_dzd']
            elif t['type'] == 'transfer_paypal_bank':
                stats['paypal_balance'] -= t['amount_sent']; stats['usd_savings'] += t['amount_received']
            if t['date'].startswith(target_month):
                if t['type'] == 'income': stats['month_earned'] += t['net_amount']
                elif t['type'] == 'expense':
                    if t['currency'] == 'USD': stats['month_spent_usd'] += t['amount']
                    else: stats['month_spent_dzd'] += t['amount']
        return stats

    def refresh_ui(self):
        stats = self.calculate_stats(); disp_rate = self.data["settings"]["display_rate"]
        self.lbl_paypal.configure(text=f"${stats['paypal_balance']:,.2f}")
        self.lbl_paypal_sub.configure(text=f"≈ {stats['paypal_balance']*disp_rate:,.0f} DZD")
        self.lbl_usd_savings.configure(text=f"${stats['usd_savings']:,.2f}")
        self.lbl_usd_sub.configure(text=f"≈ {stats['usd_savings']*disp_rate:,.0f} DZD")
        self.lbl_dzd_cash.configure(text=f"{stats['dzd_cash']:,.2f} DZD")
        self.lbl_month_earned.configure(text=f"+ ${stats['month_earned']:,.2f}")
        self.lbl_month_earned_sub.configure(text=f"≈ {stats['month_earned']*disp_rate:,.0f} DZD")
        self.lbl_month_spent_usd.configure(text=f"${stats['month_spent_usd']:,.2f}")
        self.lbl_month_spent_dzd.configure(text=f"{stats['month_spent_dzd']:,.2f} DZD")
        total_nw = stats['usd_savings'] + stats['paypal_balance'] + (stats['dzd_cash']/disp_rate if disp_rate > 0 else 0)
        self.lbl_header_nw.configure(text=f"${total_nw:,.2f}")
        self.lbl_month_selector.configure(text=f"{datetime(self.selected_year, self.selected_month, 1).strftime('%B %Y')}")
        self.update_income_list(); self.update_expense_list(); self.update_transfer_list(); self.update_dashboard_history()

    def change_month(self, direction):
        if direction == 1:
            if self.selected_month == 12: self.selected_month = 1; self.selected_year += 1
            else: self.selected_month += 1
        else:
            if self.selected_month == 1: self.selected_month = 12; self.selected_year -= 1
            else: self.selected_month -= 1
        self.refresh_ui()

    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["dashboard"] = frame
        frame.grid_columnconfigure(0, weight=1); frame.grid_rowconfigure(4, weight=1) 
        header = ctk.CTkFrame(frame, fg_color="transparent"); header.grid(row=0, column=0, sticky="ew", pady=(0, 25))
        nav = ctk.CTkFrame(header, fg_color=COLOR_CARD, corner_radius=50, height=40); nav.pack(side="left")
        ctk.CTkButton(nav, text="<", width=40, height=40, corner_radius=20, fg_color="transparent", hover_color=COLOR_HOVER, command=lambda: self.change_month(-1)).pack(side="left")
        self.lbl_month_selector = ctk.CTkLabel(nav, text="Month Year", font=("Roboto Medium", 14), width=120); self.lbl_month_selector.pack(side="left", padx=10)
        ctk.CTkButton(nav, text=">", width=40, height=40, corner_radius=20, fg_color="transparent", hover_color=COLOR_HOVER, command=lambda: self.change_month(1)).pack(side="left")
        nw = ctk.CTkFrame(header, fg_color="transparent"); nw.pack(side="right")
        ctk.CTkLabel(nw, text="NET WORTH", font=("Roboto", 11, "bold"), text_color=COLOR_TEXT_SUB).pack(anchor="e")
        self.lbl_header_nw = ctk.CTkLabel(nw, text="$0.00", font=("Roboto", 32, "bold"), text_color="white"); self.lbl_header_nw.pack(anchor="e")

        row1 = ctk.CTkFrame(frame, fg_color="transparent"); row1.grid(row=1, column=0, sticky="ew", pady=(0, 15)); row1.grid_columnconfigure((0, 1), weight=1, uniform="g1")
        c1 = ctk.CTkFrame(row1, fg_color=COLOR_CARD, corner_radius=20); c1.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(c1, text="INCOME THIS MONTH", font=("Roboto", 13), text_color=COLOR_TEXT_SUB).pack(padx=20, pady=(20, 5), anchor="w")
        self.lbl_month_earned = ctk.CTkLabel(c1, text="$0.00", font=FONT_NUMBERS, text_color=COLOR_SUCCESS); self.lbl_month_earned.pack(padx=20, anchor="w")
        self.lbl_month_earned_sub = ctk.CTkLabel(c1, text="≈ 0 DZD", font=("Roboto", 12), text_color=COLOR_TEXT_SUB); self.lbl_month_earned_sub.pack(padx=20, pady=(0, 20), anchor="w")
        
        c2 = ctk.CTkFrame(row1, fg_color=COLOR_CARD, corner_radius=20); c2.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ctk.CTkLabel(c2, text="EXPENSES THIS MONTH", font=("Roboto", 13), text_color=COLOR_TEXT_SUB).pack(padx=20, pady=(20, 5), anchor="w")
        c2i = ctk.CTkFrame(c2, fg_color="transparent"); c2i.pack(padx=20, pady=(0, 20), anchor="w")
        self.lbl_month_spent_usd = ctk.CTkLabel(c2i, text="$0.00", font=("Roboto", 24, "bold"), text_color=COLOR_DANGER); self.lbl_month_spent_usd.pack(side="left")
        ctk.CTkLabel(c2i, text="  |  ", font=("Roboto", 20), text_color=COLOR_TEXT_SUB).pack(side="left")
        self.lbl_month_spent_dzd = ctk.CTkLabel(c2i, text="0 DZD", font=("Roboto", 24, "bold"), text_color=COLOR_DANGER); self.lbl_month_spent_dzd.pack(side="left")

        row2 = ctk.CTkFrame(frame, fg_color="transparent"); row2.grid(row=2, column=0, sticky="ew", pady=(0, 25)); row2.grid_columnconfigure((0, 1, 2), weight=1, uniform="g2")
        c3 = ctk.CTkFrame(row2, fg_color=COLOR_CARD, corner_radius=20); c3.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(c3, text="PAYPAL (PENDING)", font=("Roboto", 13), text_color=COLOR_TEXT_SUB).pack(padx=20, pady=(20, 5), anchor="w")
        self.lbl_paypal = ctk.CTkLabel(c3, text="$0.00", font=FONT_NUMBERS, text_color=COLOR_WARNING); self.lbl_paypal.pack(padx=20, anchor="w")
        self.lbl_paypal_sub = ctk.CTkLabel(c3, text="≈ 0 DZD", font=("Roboto", 12), text_color=COLOR_TEXT_SUB); self.lbl_paypal_sub.pack(padx=20, pady=(0, 20), anchor="w")
        
        c4 = ctk.CTkFrame(row2, fg_color=COLOR_CARD, corner_radius=20); c4.grid(row=0, column=1, sticky="ew", padx=10)
        ctk.CTkLabel(c4, text="BANK SAVINGS (USABLE)", font=("Roboto", 13), text_color=COLOR_TEXT_SUB).pack(padx=20, pady=(20, 5), anchor="w")
        self.lbl_usd_savings = ctk.CTkLabel(c4, text="$0.00", font=FONT_NUMBERS, text_color=COLOR_TEXT_MAIN); self.lbl_usd_savings.pack(padx=20, anchor="w")
        self.lbl_usd_sub = ctk.CTkLabel(c4, text="≈ 0 DZD", font=("Roboto", 12), text_color=COLOR_TEXT_SUB); self.lbl_usd_sub.pack(padx=20, pady=(0, 20), anchor="w")
        
        c5 = ctk.CTkFrame(row2, fg_color=COLOR_CARD, corner_radius=20); c5.grid(row=0, column=2, sticky="ew", padx=(10, 0))
        ctk.CTkLabel(c5, text="LOCAL CASH", font=("Roboto", 13), text_color=COLOR_TEXT_SUB).pack(padx=20, pady=(20, 5), anchor="w")
        self.lbl_dzd_cash = ctk.CTkLabel(c5, text="--", font=FONT_NUMBERS, text_color=COLOR_TEXT_MAIN); self.lbl_dzd_cash.pack(padx=20, pady=(0, 20), anchor="w")

        ctk.CTkLabel(frame, text="Recent Activity", font=FONT_SUBHEADER).grid(row=3, column=0, sticky="w", pady=(0, 10))
        self.dashboard_history_frame = ctk.CTkScrollableFrame(frame, fg_color="transparent", height=200); self.dashboard_history_frame.grid(row=4, column=0, sticky="nsew")

    def update_dashboard_history(self):
        for w in self.dashboard_history_frame.winfo_children(): w.destroy()
        for t in sorted(self.data["transactions"], key=lambda x: x['date'], reverse=True)[:20]: self.create_list_row_modern(self.dashboard_history_frame, t, simple=True)

    def create_form_container(self, parent, title):
        parent.grid_columnconfigure(0, weight=1); parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text=title, font=FONT_HEADER).grid(row=0, column=0, sticky="w", pady=(0, 20))
        c = ctk.CTkFrame(parent, fg_color="transparent"); c.grid(row=1, column=0, sticky="new"); c.grid_columnconfigure((0, 1), weight=1)
        return c

    def create_input(self, p, ph): return ctk.CTkEntry(p, height=45, corner_radius=22, border_width=0, fg_color=COLOR_INPUT, text_color="white", placeholder_text=ph)

    def create_income_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["income"] = f
        form = self.create_form_container(f, "Add Income")
        self.entry_inc_name = self.create_input(form, "Source Name"); self.entry_inc_name.grid(row=0, column=0, padx=(0, 10), pady=10, sticky="ew")
        self.entry_inc_amount = self.create_input(form, "Gross Amount (USD)"); self.entry_inc_amount.grid(row=0, column=1, padx=(10, 0), pady=10, sticky="ew")
        self.combo_fee_type = ctk.CTkComboBox(form, height=45, corner_radius=22, border_width=0, fg_color=COLOR_INPUT, values=["No Fee", "Upwork (10%)", "Transaction Fee (Manual)"], command=lambda c: self.entry_fee_val.configure(state="normal" if "Manual" in c else "disabled")); self.combo_fee_type.grid(row=1, column=0, padx=(0, 10), pady=10, sticky="ew")
        self.entry_fee_val = self.create_input(form, "Fee Amount ($)"); self.entry_fee_val.grid(row=1, column=1, padx=(10, 0), pady=10, sticky="ew"); self.entry_fee_val.configure(state="disabled")
        self.chk_paypal_var = ctk.StringVar(value="on"); ctk.CTkCheckBox(form, text="Add to PayPal Balance (Unusable)", variable=self.chk_paypal_var, onvalue="on", offvalue="off", font=FONT_MAIN, fg_color=COLOR_PRIMARY).grid(row=2, column=0, columnspan=2, pady=15, sticky="w")
        ctk.CTkButton(form, text="Add Income", height=50, corner_radius=25, fg_color=COLOR_SUCCESS, font=FONT_BOLD, command=self.add_income).grid(row=3, column=0, columnspan=2, pady=20, sticky="ew")
        ctk.CTkLabel(f, text="History", font=FONT_SUBHEADER).grid(row=2, column=0, sticky="w", pady=(30, 10)); self.income_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent"); self.income_list_frame.grid(row=3, column=0, sticky="nsew")

    def create_transfer_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["transfer"] = f
        tv = ctk.CTkTabview(f, fg_color="transparent", segmented_button_fg_color=COLOR_INPUT, segmented_button_selected_color=COLOR_PRIMARY, corner_radius=20); tv.pack(fill="both", expand=True)
        t1 = tv.add("PayPal -> Bank"); t2 = tv.add("Sell USD -> DZD")
        
        c1 = ctk.CTkFrame(t1, fg_color="transparent"); c1.pack(fill="x", padx=20, pady=20)
        self.entry_pp_amount = self.create_input(c1, "Amount ($)"); self.entry_pp_amount.pack(fill="x", pady=10)
        self.combo_pp_method = ctk.CTkComboBox(c1, height=45, corner_radius=22, fg_color=COLOR_INPUT, border_width=0, values=["Automatic (Free)", "Manual ($5 Fee)"]); self.combo_pp_method.pack(fill="x", pady=10)
        ctk.CTkButton(c1, text="Process", height=50, corner_radius=25, fg_color=COLOR_WARNING, font=FONT_BOLD, command=self.transfer_paypal_to_bank).pack(fill="x", pady=20)
        
        c2 = ctk.CTkFrame(t2, fg_color="transparent"); c2.pack(fill="x", padx=20, pady=20)
        self.entry_ex_usd = self.create_input(c2, "Amount ($)"); self.entry_ex_usd.pack(fill="x", pady=10)
        self.entry_ex_rate = self.create_input(c2, "Rate"); self.entry_ex_rate.pack(fill="x", pady=10)
        ctk.CTkButton(c2, text="Confirm Sale", height=50, corner_radius=25, fg_color=COLOR_PRIMARY, font=FONT_BOLD, command=self.transfer_usd_to_dzd).pack(fill="x", pady=20)
        self.transfer_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent"); self.transfer_list_frame.pack(fill="both", expand=True)

    def create_expenses_frame(self):
        f = ctk.CTkFrame(self.main_frame, fg_color="transparent"); self.frames["expenses"] = f
        form = self.create_form_container(f, "Record Expense")
        self.entry_exp_desc = self.create_input(form, "Description"); self.entry_exp_desc.grid(row=0, column=0, padx=(0, 10), pady=10, sticky="ew")
        self.entry_exp_amount = self.create_input(form, "Amount"); self.entry_exp_amount.grid(row=0, column=1, padx=(10, 0), pady=10, sticky="ew")
        self.combo_exp_cat = ctk.CTkComboBox(form, height=45, corner_radius=22, fg_color=COLOR_INPUT, border_width=0, values=["Essentials", "Debt", "Luxury", "Business", "Other"]); self.combo_exp_cat.grid(row=1, column=0, padx=(0, 10), pady=10, sticky="ew")
        self.combo_exp_curr = ctk.CTkComboBox(form, height=45, corner_radius=22, fg_color=COLOR_INPUT, border_width=0, values=["DZD (Cash)", "USD (Online)"]); self.combo_exp_curr.grid(row=1, column=1, padx=(10, 0), pady=10, sticky="ew")
        ctk.CTkButton(form, text="Record", height=50, corner_radius=25, fg_color=COLOR_DANGER, font=FONT_BOLD, command=self.add_expense).grid(row=2, column=0, columnspan=2, pady=20, sticky="ew")
        ctk.CTkLabel(f, text="Recent", font=FONT_SUBHEADER).grid(row=2, column=0, sticky="w", pady=(30, 10)); self.expense_list_frame = ctk.CTkScrollableFrame(f, fg_color="transparent"); self.expense_list_frame.grid(row=3, column=0, sticky="nsew")

    def add_income(self):
        try:
            val = float(self.entry_inc_amount.get())
            fee = float(self.entry_fee_val.get()) if "Manual" in self.combo_fee_type.get() else (val*0.1 if "Upwork" in self.combo_fee_type.get() else 0)
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type":"income", "category":self.entry_inc_name.get(), "amount":val, "fee_amount":fee, "net_amount":val-fee, "to_paypal":self.chk_paypal_var.get()=="on"}
            if self.add_transaction_to_db(t):
                self.entry_inc_name.delete(0, 'end'); self.entry_inc_amount.delete(0, 'end'); self.entry_fee_val.delete(0, 'end')
                self.show_success_native("Income Added")
        except Exception: self.show_error_native("Invalid Input")

    def transfer_paypal_to_bank(self):
        try:
            amt = float(self.entry_pp_amount.get()); fee = 5.0 if "Manual" in self.combo_pp_method.get() else 0
            if self.calculate_stats()['paypal_balance'] < amt: self.show_error_native("Insufficient Funds"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type":"transfer_paypal_bank", "amount_sent":amt, "fee_paid":fee, "amount_received":amt-fee}
            if self.add_transaction_to_db(t):
                self.entry_pp_amount.delete(0, 'end')
                self.show_success_native("Transfer Complete")
        except Exception: self.show_error_native("Invalid Input")

    def transfer_usd_to_dzd(self):
        try:
            usd = float(self.entry_ex_usd.get()); rate = float(self.entry_ex_rate.get())
            if self.calculate_stats()['usd_savings'] < usd: self.show_error_native("Insufficient USD"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type":"transfer_usd_dzd", "amount_usd":usd, "rate":rate, "amount_dzd":usd*rate}
            if self.add_transaction_to_db(t):
                self.entry_ex_usd.delete(0, 'end'); self.entry_ex_rate.delete(0, 'end')
                self.show_success_native("Exchange Complete")
        except Exception: self.show_error_native("Invalid Input")

    def add_expense(self):
        try:
            amt = float(self.entry_exp_amount.get()); curr = "USD" if "USD" in self.combo_exp_curr.get() else "DZD"
            s = self.calculate_stats()
            if (curr=="USD" and s['usd_savings']<amt) or (curr=="DZD" and s['dzd_cash']<amt): self.show_error_native("Insufficient Funds"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type":"expense", "category":f"{self.combo_exp_cat.get()} - {self.entry_exp_desc.get()}", "amount":amt, "currency":curr}
            if self.add_transaction_to_db(t):
                self.entry_exp_desc.delete(0, 'end'); self.entry_exp_amount.delete(0, 'end')
                self.show_success_native("Expense Added")
        except Exception: self.show_error_native("Invalid Input")

    def create_list_row_modern(self, parent, t, simple=False):
        row = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=15); row.pack(fill="x", pady=4)
        main_txt, sub_txt, amt_txt, col = "", "", "", COLOR_TEXT_MAIN
        display_date = t['date'][:16] 
        if t['type'] == 'income': 
            main_txt = t['category']; sub_txt = f"{display_date} • {'PayPal' if t.get('to_paypal') else 'Bank'}"; amt_txt = f"+ ${t['net_amount']:,.2f}"; col = COLOR_SUCCESS
        elif t['type'] == 'expense': 
            main_txt = t['category']; sub_txt = display_date; amt_txt = f"- {'$' if t['currency']=='USD' else 'DZD'} {t['amount']:,.2f}"; col = COLOR_DANGER
        elif t['type'] == 'transfer_usd_dzd':
            main_txt = "Sold USD"; sub_txt = f"{display_date} • Rate: {t.get('rate', '')}"; amt_txt = f"+ {t.get('amount_dzd', 0):,.2f} DZD"; col = COLOR_PRIMARY
        elif t['type'] == 'transfer_paypal_bank':
            main_txt = "PayPal Transfer"; sub_txt = f"{display_date} • Fee: ${t.get('fee_paid', 0)}"; amt_txt = f"${t.get('amount_received', 0):,.2f}"; col = COLOR_WARNING
        
        tf = ctk.CTkFrame(row, fg_color="transparent"); tf.pack(side="left", padx=15, pady=12)
        ctk.CTkLabel(tf, text=main_txt, font=("Roboto Medium", 14)).pack(anchor="w"); ctk.CTkLabel(tf, text=sub_txt, font=("Roboto", 11), text_color=COLOR_TEXT_SUB).pack(anchor="w")
        if not simple: ctk.CTkButton(row, text="×", width=30, height=30, corner_radius=15, fg_color=COLOR_HOVER, hover_color=COLOR_DANGER, command=lambda: self.delete_transaction(t['id'])).pack(side="right", padx=(5, 15))
        ctk.CTkLabel(row, text=amt_txt, font=("Roboto", 14, "bold"), text_color=col).pack(side="right", padx=10)

    def update_income_list(self): 
        for w in self.income_list_frame.winfo_children(): w.destroy()
        for t in reversed(self.data["transactions"]): 
            if t['type'] == 'income' and t['date'].startswith(self.get_monthly_key()): self.create_list_row_modern(self.income_list_frame, t)
    
    def update_expense_list(self):
        for w in self.expense_list_frame.winfo_children(): w.destroy()
        for t in reversed(self.data["transactions"]): 
            if t['type'] == 'expense' and t['date'].startswith(self.get_monthly_key()): self.create_list_row_modern(self.expense_list_frame, t)

    def update_transfer_list(self):
        for w in self.transfer_list_frame.winfo_children(): w.destroy()
        for t in reversed(self.data["transactions"]): 
            if 'transfer' in t['type']: self.create_list_row_modern(self.transfer_list_frame, t)

if __name__ == "__main__":
    app = FinancialTrackerApp()
    app.mainloop()
