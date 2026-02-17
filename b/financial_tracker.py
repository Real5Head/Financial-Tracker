import customtkinter as ctk
from tkinter import messagebox
import json
import os
import uuid
import sys
import psycopg2
from psycopg2.extras import Json
from datetime import datetime

# --- Theme Configuration ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

# --- Modern Palette ---
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

        # Setup Main Window First (So errors show up correctly)
        self.title("Personal Financial Tracker")
        self.geometry("1280x850")
        self.minsize(1000, 700)
        self.configure(fg_color=COLOR_BG)

        # 1. Setup Database Connection (Safe Loop)
        self.initialize_db_connection()

        # 2. Load Data from Cloud
        self.data = self.load_data()
        
        self.current_date = datetime.now()
        self.selected_month = self.current_date.month
        self.selected_year = self.current_date.year

        # --- Grid Layout ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar ---
        self.sidebar_frame = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=COLOR_SIDEBAR)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(6, weight=1) 
        
        self.nav_buttons = {} 
        self.create_sidebar()

        # --- Main Content Area ---
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=30, pady=30)
        
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=1)

        self.frames = {}
        self.create_dashboard_frame()
        self.create_income_frame()
        self.create_transfer_frame()
        self.create_expenses_frame()

        self.show_frame("dashboard")

    # --- DATABASE LOGIC ---
    def initialize_db_connection(self):
        """Safely connects to the DB. Loops if it fails so the app doesn't crash."""
        config_file = "db_config.json"
        
        while True:
            db_url = ""
            
            # Check for saved URL
            if os.path.exists(config_file):
                try:
                    with open(config_file, "r") as f:
                        db_url = json.load(f).get("db_url", "").strip()
                except: pass
            
            # If no URL is saved, prompt the user
            if not db_url:
                dialog = ctk.CTkInputDialog(
                    text="Enter Neon PostgreSQL Connection String:\n(Make sure there are no blank spaces)", 
                    title="Database Setup"
                )
                db_url = dialog.get_input()
                
                # If user clicks cancel or closes prompt, exit app
                if not db_url:
                    sys.exit()
                    
                # Clean up the string (removes accidental spaces)
                db_url = db_url.strip()
                
                # Neon requires SSL. Force add it if missing.
                if "sslmode=require" not in db_url:
                    if "?" in db_url:
                        db_url += "&sslmode=require"
                    else:
                        db_url += "?sslmode=require"
                        
                # Save the new URL
                with open(config_file, "w") as f:
                    json.dump({"db_url": db_url}, f)
            
            # Test the connection
            try:
                self.db_url = db_url
                with psycopg2.connect(self.db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value FLOAT)")
                        cur.execute("CREATE TABLE IF NOT EXISTS transactions (id VARCHAR(255) PRIMARY KEY, t_date VARCHAR(50), t_type VARCHAR(50), payload JSONB)")
                        cur.execute("INSERT INTO settings (key, value) VALUES ('display_rate', 200.0) ON CONFLICT DO NOTHING")
                    conn.commit()
                # If we get here, connection is successful! Break the loop.
                break 
                
            except Exception as e:
                # Connection failed! Wipe the bad config and show error.
                if os.path.exists(config_file):
                    os.remove(config_file)
                messagebox.showerror(
                    "Connection Error", 
                    f"Could not connect to the database. The database might be waking up, or the link is invalid.\n\nError details:\n{e}\n\nPlease try again."
                )

    def get_db_connection(self):
        return psycopg2.connect(self.db_url)

    def load_data(self):
        """Fetches all data from Neon DB."""
        data = {"settings": {"display_rate": 200.0}, "transactions": []}
        try:
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Get Setting
                    cur.execute("SELECT value FROM settings WHERE key = 'display_rate'")
                    row = cur.fetchone()
                    if row: data["settings"]["display_rate"] = row[0]
                    # Get Transactions
                    cur.execute("SELECT payload FROM transactions ORDER BY t_date ASC")
                    rows = cur.fetchall()
                    data["transactions"] = [r[0] for r in rows]
        except Exception as e:
            messagebox.showerror("Database Error", f"Could not fetch data:\n{e}")
        return data

    def add_transaction_to_db(self, t):
        """Inserts a single transaction into Neon DB."""
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
        except Exception as e:
            messagebox.showerror("Database Error", f"Failed to save:\n{e}")
            return False

    def delete_transaction(self, tid):
        if messagebox.askyesno("Confirm", "Delete this transaction?"):
            self.data["transactions"] = [t for t in self.data["transactions"] if t.get("id") != tid]
            try:
                with self.get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM transactions WHERE id = %s", (tid,))
                self.refresh_ui()
            except Exception as e:
                messagebox.showerror("Database Error", str(e))

    def update_display_rate(self):
        try:
            new_rate = float(self.entry_display_rate.get())
            self.data["settings"]["display_rate"] = new_rate
            with self.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE settings SET value = %s WHERE key = 'display_rate'", (new_rate,))
            messagebox.showinfo("Success", "Rate Updated across all devices.")
            self.refresh_ui()
        except ValueError: messagebox.showerror("Error", "Invalid Number")
        except Exception as e: messagebox.showerror("Database Error", str(e))

    # --- Sidebar ---
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
        # Fetch fresh data from cloud when switching tabs
        self.data = self.load_data()
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

    # --- UI GENERATORS ---
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

    # --- FORMS ---
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

    # --- ACTIONS ---
    def add_income(self):
        try:
            val = float(self.entry_inc_amount.get())
            fee = float(self.entry_fee_val.get()) if "Manual" in self.combo_fee_type.get() else (val*0.1 if "Upwork" in self.combo_fee_type.get() else 0)
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d"), "type":"income", "category":self.entry_inc_name.get(), "amount":val, "fee_amount":fee, "net_amount":val-fee, "to_paypal":self.chk_paypal_var.get()=="on"}
            if self.add_transaction_to_db(t):
                self.entry_inc_name.delete(0, 'end'); self.entry_inc_amount.delete(0, 'end'); self.entry_fee_val.delete(0, 'end')
                messagebox.showinfo("Success", "Income Added")
        except: messagebox.showerror("Error", "Invalid Input")

    def transfer_paypal_to_bank(self):
        try:
            amt = float(self.entry_pp_amount.get()); fee = 5.0 if "Manual" in self.combo_pp_method.get() else 0
            if self.calculate_stats()['paypal_balance'] < amt: messagebox.showerror("Error", "Insufficient Funds"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d"), "type":"transfer_paypal_bank", "amount_sent":amt, "fee_paid":fee, "amount_received":amt-fee}
            if self.add_transaction_to_db(t):
                self.entry_pp_amount.delete(0, 'end')
                messagebox.showinfo("Success", "Transfer Complete")
        except: messagebox.showerror("Error", "Invalid Input")

    def transfer_usd_to_dzd(self):
        try:
            usd = float(self.entry_ex_usd.get()); rate = float(self.entry_ex_rate.get())
            if self.calculate_stats()['usd_savings'] < usd: messagebox.showerror("Error", "Insufficient USD"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d"), "type":"transfer_usd_dzd", "amount_usd":usd, "rate":rate, "amount_dzd":usd*rate}
            if self.add_transaction_to_db(t):
                self.entry_ex_usd.delete(0, 'end'); self.entry_ex_rate.delete(0, 'end')
                messagebox.showinfo("Success", "Exchange Complete")
        except: messagebox.showerror("Error", "Invalid Input")

    def add_expense(self):
        try:
            amt = float(self.entry_exp_amount.get()); curr = "USD" if "USD" in self.combo_exp_curr.get() else "DZD"
            s = self.calculate_stats()
            if (curr=="USD" and s['usd_savings']<amt) or (curr=="DZD" and s['dzd_cash']<amt): messagebox.showerror("Error", "Insufficient Funds"); return
            t = {"id":str(uuid.uuid4()), "date":datetime.now().strftime("%Y-%m-%d"), "type":"expense", "category":f"{self.combo_exp_cat.get()} - {self.entry_exp_desc.get()}", "amount":amt, "currency":curr}
            if self.add_transaction_to_db(t):
                self.entry_exp_desc.delete(0, 'end'); self.entry_exp_amount.delete(0, 'end')
                messagebox.showinfo("Success", "Expense Added")
        except: messagebox.showerror("Error", "Invalid Input")

    def create_list_row_modern(self, parent, t, simple=False):
        row = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=15); row.pack(fill="x", pady=4)
        main_txt, sub_txt, amt_txt, col = "", "", "", COLOR_TEXT_MAIN
        if t['type'] == 'income': main_txt, sub_txt, amt_txt, col = t['category'], f"{t['date']} • {'PayPal' if t.get('to_paypal') else 'Bank'}", f"+ ${t['net_amount']:,.2f}", COLOR_SUCCESS
        elif t['type'] == 'expense': main_txt, sub_txt, amt_txt, col = t['category'], t['date'], f"- {'$' if t['currency']=='USD' else 'DZD'} {t['amount']:,.2f}", COLOR_DANGER
        elif 'transfer' in t['type']: main_txt, sub_txt, amt_txt, col = "Transfer", t['date'], "Processed", COLOR_PRIMARY
        
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