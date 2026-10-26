#!/usr/bin/env python3
"""Always-on-top week view for jotting down worklog entries.
Line format:  CODE-time-description   e.g.  BE-2h-worked on ticket   (spaces around the hyphens optional)
Entries are saved to ~/.worklog/entries.json (read by tempo_sync.py).
"""
import json, os, re, subprocess, sys, threading, uuid
import tkinter as tk
from tkinter import ttk
from datetime import date, datetime, timedelta
from pathlib import Path

import store
from store import load, save

CONFIG = Path(__file__).with_name("tasks.json")
SYNC_KEYS = ("synced", "task", "jiraWorklogId")  # upload state that Undo must never roll back


def parse_duration(s):
    """'2hrs', '1h30m', '1,5h', '90min', '2', '.5', '1:30' -> seconds, or None.
    A bare number is hours ('2' = 2h, '.5' = 30min); minutes need an explicit unit ('45m')."""
    s = s.strip().lower().replace(",", ".")
    if m := re.fullmatch(r"(\d+):(\d{1,2})", s):
        return int(m[1]) * 3600 + int(m[2]) * 60
    num = r"(\d+(?:\.\d+)?|\.\d+)"
    if re.fullmatch(num, s):
        return round(float(s) * 3600)
    parts = re.findall(num + r"\s*(h[a-z]*|m[a-z]*)", s)
    if not parts or re.sub(num + r"\s*(h[a-z]*|m[a-z]*)|\s", "", s):
        return None
    return round(sum(float(n) * (3600 if u[0] == "h" else 60) for n, u in parts))


def parse_date(s):
    s = s.strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m."):
        try:
            d = datetime.strptime(s, f).date()
            return d.replace(year=date.today().year) if f == "%d.%m." else d
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", s):
        return parse_date(s + ".")
    return None


def fmt(seconds):
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


BG, PANEL, FG, MUTED = "#1e1f26", "#2a2c36", "#e8e9ef", "#8b8fa3"
ACCENT, OK, ERR, SEL = "#7c8cff", "#5ccf9a", "#ff7a8a", "#3a3d4f"
HOLIDAY = "#f2c94c"
MAC = sys.platform == "darwin"
MOD = "Command" if MAC else "Control"  # Tk event modifier for the shortcut key
MOD_NAME = "Cmd" if MAC else "Ctrl"  # the same, as shown in hints
MONO = "Menlo" if MAC else "Consolas"


def font(size, bold=False):
    if MAC:
        return ("Helvetica Neue", size, "bold") if bold else ("Helvetica Neue", size)
    return ("Segoe UI Semibold" if bold else "Segoe UI", size)


FONT, FONT_B = font(10), font(10, True)
CHECK, DOT = "✓", "·"


def confirm(parent, title, lines, ok_text="OK"):
    """Small dark modal dialog. Returns True if the user confirms."""
    win = tk.Toplevel(parent, bg=BG)
    win.title(title)
    win.transient(parent)
    win.attributes("-topmost", True)
    win.resizable(False, False)
    answer = []
    tk.Label(win, text=title, bg=BG, fg=FG, font=font(12, True)).pack(anchor="w", padx=20, pady=(16, 4))
    for line in lines:
        tk.Label(win, text=line, bg=BG, fg=MUTED, font=FONT, justify="left").pack(anchor="w", padx=20)
    row = tk.Frame(win, bg=BG)
    row.pack(fill="x", padx=20, pady=16)

    def done(value):
        answer.append(value)
        win.destroy()

    ok = button(row, ok_text, lambda: done(True))
    ok.config(bg=ACCENT, fg=BG)
    ok.bind("<Enter>", lambda _: ok.config(bg=FG))
    ok.bind("<Leave>", lambda _: ok.config(bg=ACCENT))
    ok.pack(side="right")
    button(row, "Cancel", lambda: done(False)).pack(side="right", padx=8)
    win.bind("<Return>", lambda _: done(True))
    win.bind("<Escape>", lambda _: done(False))
    win.protocol("WM_DELETE_WINDOW", lambda: done(False))
    win.update_idletasks()
    win.geometry(f"+{parent.winfo_x() + 60}+{parent.winfo_y() + 80}")
    win.grab_set()
    win.focus_force()
    parent.wait_window(win)
    return bool(answer and answer[0])


class Tip:
    """Hover hint. `text` is a string, or a callable(event) -> string/None for per-row hints."""
    def __init__(self, widget, text, per_row=False):
        self.widget, self.text, self.per_row, self.win, self.job, self.shown = widget, text, per_row, None, None, None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Motion>", self.schedule if per_row else lambda _: None, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, event):
        text = self.text(event) if self.per_row else self.text
        if text != self.shown:
            self.hide()
        if text and not self.win:
            self.job = self.widget.after(500, lambda: self.show(text, event.x_root, event.y_root))

    def show(self, text, x, y):
        self.shown = text
        self.win = tk.Toplevel(self.widget)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        tk.Label(self.win, text=text, bg=SEL, fg=FG, font=font(9), padx=8, pady=4,
                 justify="left", wraplength=420).pack()
        self.win.geometry(f"+{x + 12}+{y + 18}")

    def hide(self, _=None):
        if self.job:
            self.widget.after_cancel(self.job)
        self.job = self.shown = None
        if self.win:
            self.win.destroy()
            self.win = None


def button(parent, text, cmd, width=None, tip=None):
    if MAC:  # Aqua's tk.Button ignores bg/fg, so a clickable Label gives the same flat look
        b = tk.Label(parent, text=text, width=width, font=FONT, bg=PANEL, fg=FG, padx=10, pady=4, cursor="pointinghand")
        b.bind("<Button-1>", lambda _: cmd())
    else:
        b = tk.Button(parent, text=text, command=cmd, width=width, font=FONT, bg=PANEL, fg=FG,
                      activebackground=SEL, activeforeground=FG, relief="flat", bd=0,
                      padx=10, pady=4, cursor="hand2")
    b.bind("<Enter>", lambda _: b.config(bg=SEL))
    b.bind("<Leave>", lambda _: b.config(bg=PANEL))
    if tip:
        Tip(b, tip)
    return b


class Widget:
    def __init__(self, root, tray=None):
        self.root, self.target, self.clip, self.rows = root, date.today(), [], []
        self.tray = tray
        self.editing = None  # id of the entry being edited, if any
        self.last_days = []  # days of the last multi-day add, re-selected afterwards
        cfg = store.load_config(CONFIG)
        self.tasks = cfg["tasks"]
        self.target_s = round(float(cfg.get("target_hours", 8)) * 3600)  # per weekday
        self.meeting_code = next((t["code"] for t in self.tasks
                                  if t["code"].lower() == cfg.get("meeting_code", "MTG").lower()), None)
        self.importing = False
        hours = float(cfg.get("max_entry_hours", 4))
        self.max_s = round(hours * 3600) if hours >= 0 else None  # longest typed entry; -1 = no limit
        self.cfg = cfg
        self.undo_stack = []  # JSON snapshots of the entries file, newest last
        self.history, self.hist_i, self.hist_draft = [], -1, ""  # Up/Down recall in the input
        self.syncing = False
        self.reminder_at = cfg.get("reminder_time", "15:00")  # "HH:MM"; empty string turns it off
        self.reminded_on = None
        root.title("Worklog")
        icon = Path(__file__).with_name("worklog.ico")
        if icon.exists():
            try:
                root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        root.attributes("-topmost", True)
        root.geometry("700x580")
        root.configure(bg=BG)
        root.minsize(480, 360)

        style = ttk.Style(root)
        style.theme_use("clam")  # the native Windows theme ignores scrollbar colours
        style.layout("Slim.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Slim.Vertical.TScrollbar", troughcolor=PANEL, background=SEL, bordercolor=PANEL,
                        lightcolor=SEL, darkcolor=SEL, relief="flat", width=8, gripcount=0)
        style.map("Slim.Vertical.TScrollbar", background=[("pressed", ACCENT), ("active", MUTED)],
                  lightcolor=[("pressed", ACCENT), ("active", MUTED)],
                  darkcolor=[("pressed", ACCENT), ("active", MUTED)])

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 6))
        button(top, "‹", lambda: self.shift(-7), 2, "Previous week").pack(side="left")
        self.date_var = tk.StringVar()
        de = tk.Entry(top, textvariable=self.date_var, width=11, justify="center", font=FONT_B,
                      bg=PANEL, fg=FG, insertbackground=FG, relief="flat", bd=0)
        de.pack(side="left", padx=6, ipady=5)
        de.bind("<Return>", self.set_date)
        button(top, "›", lambda: self.shift(7), 2, "Next week").pack(side="left")
        button(top, "Today", lambda: self.goto(date.today()), tip="Jump to this week").pack(side="left", padx=6)
        button(top, "↻ Reload", self.reload, tip="Re-read entries from disk (F5)").pack(side="left")
        root.bind("<F5>", lambda _: self.reload())
        self.total = tk.Label(top, bg=BG, fg=ACCENT, font=font(12, True))
        self.total.pack(side="right")
        self.login = tk.Label(top, text="● Jira ...", bg=BG, fg=MUTED, font=font(9), cursor="hand2")
        self.login.pack(side="right", padx=12)
        self.login.bind("<Button-1>", lambda _: self.login_click())
        Tip(self.login, "Whether your Jira session cookie works. Click to recheck; "
                        "if it is expired, click opens cookie.txt so you can paste a new one")

        wrap = tk.Frame(root, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=12, pady=6)
        self.box = tk.Listbox(wrap, activestyle="none", selectmode="extended", exportselection=False,
                              font=(MONO, 10), bg=PANEL, fg=FG, selectbackground=SEL,
                              selectforeground=FG, relief="flat", bd=0, highlightthickness=0)
        self.box.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(wrap, orient="vertical", style="Slim.Vertical.TScrollbar", command=self.box.yview)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=8)
        self.box.config(yscrollcommand=sb.set)
        self.box.bind("<<ListboxSelect>>", self.on_select)
        self.box.bind("<Double-Button-1>", lambda _: self.edit())
        self.box.bind("<F2>", lambda _: self.edit())
        self.box.bind(f"<{MOD}-c>", lambda _: self.copy())
        self.box.bind(f"<{MOD}-v>", lambda _: self.paste())
        self.box.bind("<Delete>", lambda _: self.delete())

        self.tickets = []  # in-progress tickets from the read-only Jira; see tickets.py
        if cfg.get("tickets"):
            head = tk.Label(root, text="● My tickets in progress  (click to fill the line, ↻ to reload)", anchor="w",
                            bg=BG, fg=MUTED, font=font(9), cursor="hand2")
            head.pack(fill="x", padx=14)
            head.bind("<Button-1>", lambda _: self.load_tickets())
            self.tickets_head = head
            self.ticket_box = tk.Listbox(root, height=4, activestyle="none", exportselection=False,
                                         font=font(10), bg=PANEL, fg=FG, selectbackground=SEL,
                                         selectforeground=FG, relief="flat", bd=0, highlightthickness=0)
            self.ticket_box.pack(fill="x", padx=12, pady=(2, 4))
            self.ticket_box.bind("<<ListboxSelect>>", self.pick_ticket)
            Tip(self.ticket_box, lambda e: self.ticket_tip(e), per_row=True)

        self.target_lbl = tk.Label(root, anchor="w", bg=BG, fg=MUTED, font=font(9))
        self.target_lbl.pack(fill="x", padx=14)
        self.entry = tk.Entry(root, font=font(11), bg=PANEL, fg=FG, insertbackground=ACCENT,
                              relief="flat", bd=0, highlightthickness=2,
                              highlightbackground=PANEL, highlightcolor=ACCENT)
        self.entry.pack(fill="x", padx=12, pady=(2, 6), ipady=7)
        self.entry.bind("<Return>", self.add)
        self.entry.bind("<Escape>", lambda _: self.on_escape())
        self.entry.bind("<KeyRelease>", self.live_check)
        self.entry.bind("<Up>", lambda _: self.recall(1))
        self.entry.bind("<Down>", lambda _: self.recall(-1))
        self.entry.bind("<Tab>", self.complete)
        self.entry.focus()
        root.bind(f"<{MOD}-z>", self.on_undo_key)

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", padx=12, pady=(0, 4))
        button(bottom, "Edit", self.edit, tip="Edit the selected entry (F2 or double-click)").pack(side="left")
        button(bottom, "Copy", self.copy, tip=f"Copy the selected entries ({MOD_NAME}+C)").pack(side="left", padx=6)
        button(bottom, "Paste", self.paste, tip=f"Paste copied entries onto the selected day ({MOD_NAME}+V)").pack(side="left")
        button(bottom, "Delete", self.delete, tip="Delete the selected entries (Del). Undo brings them back").pack(side="left", padx=6)
        button(bottom, "Undo", self.undo, tip=f"Undo the last change ({MOD_NAME}+Z). Never un-syncs an entry").pack(side="left")
        button(bottom, "Mon–Fri", self.select_weekdays, tip="Select all weekdays, so one line is added to each").pack(side="left", padx=6)
        if sys.platform == "win32" and self.outlook_flag("import_meetings_from_outlook"):  # needs Outlook COM; null or false hides it
            button(bottom, "Meetings", self.import_meetings,
                   tip="Import accepted meetings from Outlook for the selected days").pack(side="left")
        button(bottom, "Sync to Jira", self.sync, tip="Upload all unsynced entries to Jira (asks to confirm first)").pack(side="right")
        self.status = tk.Label(root, anchor="w", bg=BG, fg=MUTED, font=font(9))
        self.status.pack(fill="x", padx=14, pady=(0, 10))
        self.say("CODE-time-description  ·  Up: earlier lines  ·  Tab: autocomplete")
        self.refresh()
        self.check_login()
        if cfg.get("tickets"):
            self.load_tickets()
        if sys.platform == "win32" and (self.outlook_flag("import_absences_from_outlook") or self.holiday_names()):
            self.load_absences_ahead()
        if self.reminder_at:
            root.after(5000, self.reminder_tick)
        if sys.platform == "win32":
            import winshell
            winshell.style_titlebar(root, BG, FG, PANEL)
        if tray:
            root.protocol("WM_DELETE_WINDOW", self.hide)  # X hides to tray; use the tray menu to quit
            if tray.hotkey_error:
                self.say(tray.hotkey_error + ". Change 'hotkey' in tasks.json", "err")
            root.after(100, self.poll)

    # --- tray / hotkey -------------------------------------------------
    def show(self):
        self.refresh()  # pick up sync marks and a new day
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.entry.focus_set()

    def hide(self):
        self.root.withdraw()

    def toggle(self):
        visible = self.root.state() == "normal" and self.root.focus_displayof() is not None
        self.hide() if visible else self.show()

    def poll(self):
        try:
            while True:
                event = self.tray.events.get_nowait()
                if event == "toggle":
                    self.toggle()
                elif event == "show":
                    self.show()
                elif event == "quit":
                    self.tray.close()
                    self.root.destroy()
                    return
        except Exception:  # queue.Empty
            pass
        self.root.after(100, self.poll)

    def on_escape(self):
        if self.editing:
            self.cancel_edit()
        elif self.tray:
            self.hide()

    # --- undo ----------------------------------------------------------
    def snapshot(self):
        """Call before any change to the entries file."""
        self.undo_stack.append(json.dumps(load()))
        del self.undo_stack[:-30]

    def on_undo_key(self, _):
        if self.root.focus_get() is self.entry and self.entry.get():
            return  # let the input handle its own text
        self.undo()

    def undo(self):
        if not self.undo_stack:
            self.say("Nothing to undo", "err")
            return
        previous = json.loads(self.undo_stack.pop())
        current = {e["id"]: e for e in load()}
        for e in previous:  # never roll back upload state, or a synced entry could be posted twice
            now = current.get(e["id"])
            if now and now.get("synced"):
                e.update({k: now[k] for k in SYNC_KEYS if k in now})
        save(previous)
        self.editing = None
        self.reload(quiet=True)
        self.say("Undone", "ok")

    # --- input history and completion ------------------------------------
    def build_history(self, entries):
        """Distinct past lines as 'CODE - 30min - description', newest first."""
        seen, lines = set(), []
        for e in reversed(entries):
            line = f"{e.get('code', '')} - {e['seconds'] // 60}min - {e['desc']}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        self.history = lines

    def recall(self, step):
        """Up/Down in the input: walk through earlier lines; Down past the newest restores your draft."""
        if not self.history or self.editing:
            return "break"
        if self.hist_i == -1:
            if step < 0:
                return "break"
            self.hist_draft = self.entry.get()
        self.hist_i = max(-1, min(len(self.history) - 1, self.hist_i + step))
        self.entry.delete(0, "end")
        self.entry.insert(0, self.hist_draft if self.hist_i == -1 else self.history[self.hist_i])
        self.entry.icursor("end")
        self.live_check(None, keep_history=True)
        return "break"

    def suggestion(self, text):
        """-> (text Tab would insert, label to show) or None.
        While the code is still being typed, suggest task codes; after that, earlier lines."""
        if text and not re.search(r"[-\s]", text):
            codes = [t for t in self.tasks if t["code"].lower().startswith(text.lower())]
            if codes:
                label = ", ".join(t["code"] for t in codes[:4])
                return codes[0]["code"] + "-", f"{codes[0]['code']}-   ({codes[0]['name']})" if len(codes) == 1 \
                    else f"{codes[0]['code']}-   (also {label})"

        def norm(s):  # 'mtg-3' must match 'MTG - 30min - ...'
            return re.sub(r"\s*-\s*", "-", s.lower())
        low = norm(text)
        line = next((h for h in self.history if len(norm(h)) > len(low) and norm(h).startswith(low)), None)
        return (line, line) if line else None

    def complete(self, _):
        """Tab: finish the line from the newest matching earlier entry."""
        text = self.entry.get()
        match = self.suggestion(text) if text else None
        if match:
            self.entry.delete(0, "end")
            self.entry.insert(0, match[0])
            self.entry.icursor("end")
            self.live_check(None, keep_history=True)
        return "break"  # never move focus out of the input

    # --- Jira login badge and sync -------------------------------------
    def check_login(self):
        """Check the cookie in the background and colour the badge."""
        import tempo_sync
        self.login.config(text="● Jira ...", fg=MUTED)
        result = {}

        def work():
            result["ok"], result["msg"] = tempo_sync.check_login(self.cfg)

        def poll():
            if not result:
                self.root.after(200, poll)
                return
            self.login_ok = result["ok"]
            self.login.config(text="● Jira" if result["ok"] else f"● Jira: {result['msg']}",
                              fg=OK if result["ok"] else ERR)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    def login_click(self):
        """Working cookie: recheck. Broken cookie: open cookie.txt, recheck when the user is back."""
        if getattr(self, "login_ok", True):
            return self.check_login()
        import tempo_sync
        path = tempo_sync.COOKIE_FILE
        if not path.exists():
            path.touch()  # startfile needs a file to open
        os.startfile(path)

        def arm():
            # one-shot: the first time this window regains focus after the editor
            def back(_):
                self.root.unbind("<FocusIn>", fid)
                self.check_login()
            fid = self.root.bind("<FocusIn>", back, add="+")

        self.root.after(1500, arm)  # let focus leave for the editor first

    # --- my in-progress tickets (read-only second Jira) ------------------
    def load_tickets(self):
        """Fetch in the background; the Tk thread polls the result (same pattern as check_login)."""
        import tickets
        self.tickets_head.config(text="● My tickets ...", fg=MUTED)
        result = {}

        def work():
            try:
                result["tickets"] = tickets.fetch(self.cfg["tickets"])
            except tickets.TicketError as e:
                result["error"] = str(e)

        def poll():
            if not result:
                self.root.after(200, poll)
                return
            self.ticket_box.delete(0, "end")
            if "error" in result:
                self.tickets_head.config(text=f"● Tickets: {result['error']}  (click to retry)", fg=ERR)
                return
            self.tickets = result["tickets"]
            for t in self.tickets:
                self.ticket_box.insert("end", f"{t['key']}   {t['summary']}")
            self.tickets_head.config(text=f"● My tickets in progress: {len(self.tickets)}  (click one to fill the line)",
                                     fg=OK)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    def ticket_tip(self, event):
        """Hover text for a ticket row: key and the full, unedited title."""
        i = self.ticket_box.nearest(event.y)
        box = self.ticket_box.bbox(i) if 0 <= i < len(self.tickets) else None
        if not box or event.y > box[1] + box[3]:  # pointer is in the empty space below the last row
            return None
        t = self.tickets[i]
        return t["key"] + "\n" + t["summary"]

    def pick_ticket(self, _):
        """Fill the input with 'BE -  - <title>' and park the cursor on the duration."""
        sel = self.ticket_box.curselection()
        if not sel or self.editing:
            return
        code = self.cfg["tickets"].get("code", "BE")
        prefix = f"{code} - "
        summary = self.tickets[sel[0]]["summary"]
        title = " ".join(re.sub(r"\[[^\]]*\]", " ", summary).split()) or summary  # drop '[TAG]' prefixes
        self.entry.delete(0, "end")
        self.entry.insert(0, f"{prefix} - {title}")
        self.ticket_box.selection_clear(0, "end")

        def park():  # after the click finishes, or the listbox takes the focus back
            self.entry.focus_set()
            self.entry.icursor(len(prefix))
            self.live_check(None)
        self.root.after(30, park)

    def sync(self):
        """Upload every unsynced entry after a confirmation, then refresh the ✓ marks."""
        if self.syncing:
            return
        import tempo_sync
        entries = load()
        day_start = datetime.strptime(self.cfg.get("day_start", "09:00"), "%H:%M")
        jobs, skipped = tempo_sync.plan_jobs(entries, self.tasks, day_start)
        if not jobs:
            self.say("Nothing to sync" + (f" ({len(skipped)} with unknown codes)" if skipped else ""), "ok")
            return
        try:
            headers = tempo_sync.auth_headers(self.cfg)
        except tempo_sync.SyncError as e:
            self.say(str(e), "err")
            return
        total = sum(j["seconds"] for j in jobs)
        days = sorted({j["date"] for j in jobs})
        lines = [f"{len(jobs)} entries, {fmt(total)}, {len(days)} day{'s' if len(days) != 1 else ''} "
                 f"({days[0]}{' to ' + days[-1] if len(days) > 1 else ''})"]
        if skipped:
            lines.append(f"{len(skipped)} skipped: unknown task code")
        if not confirm(self.root, "Sync to Jira", lines, "Upload"):
            return

        self.syncing = True
        state = {"done": 0, "failed": [], "finished": False}

        def on_result(job, res, err):
            if err:
                state["failed"].append(f"{job['date']} {job['desc']}: {err}")
                return
            store.mark_synced(job["id"], job["task"]["name"], res.get("id"))
            state["done"] += 1

        def work():
            try:
                tempo_sync.post_jobs(self.cfg, headers, jobs, on_result)
            finally:
                state["finished"] = True

        def poll():
            self.say(f"Uploading {state['done']}/{len(jobs)}...")
            if not state["finished"]:
                self.root.after(200, poll)
                return
            self.syncing = False
            self.reload(quiet=True)
            if state["failed"]:
                self.say(f"Uploaded {state['done']}/{len(jobs)}. {state['failed'][0]}", "err")
                self.check_login()
            else:
                self.say(f"Uploaded {state['done']} entries ({fmt(total)})", "ok")

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    # --- end-of-day nudge ------------------------------------------------
    def reminder_tick(self):
        """Once per weekday, at or after reminder_time, nudge if today is below the target."""
        today = date.today()
        try:
            hh, mm = map(int, self.reminder_at.split(":"))
        except ValueError:
            return  # bad setting: reminder off
        if today.weekday() < 5 and self.reminded_on != today and datetime.now() >= datetime.combine(
                today, datetime.min.time()).replace(hour=hh, minute=mm):
            self.reminded_on = today
            logged = sum(e["seconds"] for e in load() if e["date"] == today.isoformat())
            goal = self.day_target(today)  # lower (or 0) on days with an absence
            if logged < goal:
                text = f"Logged {fmt(logged)} of {fmt(goal)} today. Missing {fmt(goal - logged)}."
                if self.tray:
                    self.tray.notify("Worklog", text)
                self.say(text, "err")
        self.root.after(60000, self.reminder_tick)

    def day_target(self, d, absences=None):
        """Hours to log on day d: the daily target minus any absence, never below 0. Weekends have none."""
        if d.weekday() >= 5:
            return 0
        absent = sum(a["seconds"] for a in (store.load_absences() if absences is None else absences)
                     if a["date"] == d.isoformat())
        return max(0, self.target_s - absent)

    def say(self, text, kind="info"):
        self.status.config(text=text, fg={"info": MUTED, "ok": OK, "err": ERR}[kind])

    @staticmethod
    def row_day(row):
        kind, v = row
        return v if kind == "day" else date.fromisoformat(v["date"])

    def week_start(self):
        return self.target - timedelta(days=self.target.weekday())

    def target_days(self):
        """Days that Enter / Paste write to: every day touched by the selection, else the current day."""
        days = sorted({self.row_day(self.rows[i]) for i in self.box.curselection()})
        return days or [self.target]

    def select_days(self, days):
        self.box.selection_clear(0, "end")
        for i, row in enumerate(self.rows):
            if row[0] == "day" and row[1] in days:
                self.box.selection_set(i)
        self.show_target()

    def select_weekdays(self):
        start = self.week_start()
        self.select_days({start + timedelta(days=i) for i in range(5)})
        self.entry.focus_set()

    def show_target(self):
        self.date_var.set(self.target.strftime("%Y-%m-%d"))
        if self.editing:
            text = "Editing entry  ·  Enter to save, Esc to cancel"
        else:
            days = self.target_days()
            names = ", ".join(d.strftime("%a %d.%m.") for d in days)
            hint = f"{MOD_NAME}+click days to add to several" if len(days) == 1 else f"{len(days)} days"
            text = f"Adding to {names}  ·  {hint}"
        self.target_lbl.config(text=text, fg=ACCENT if self.editing else MUTED)

    def refresh(self):
        entries = load()
        absences = store.load_absences()
        self.build_history(entries)
        self.rows = []
        self.box.delete(0, "end")
        week_total = week_goal = 0
        for i in range(7):
            d = self.week_start() + timedelta(days=i)
            es = [e for e in entries if e["date"] == d.isoformat()]
            ab = [a for a in absences if a["date"] == d.isoformat()]
            if i >= 5 and not es and not ab:
                continue
            tot = sum(e["seconds"] for e in es)
            week_total += tot
            weekday = i < 5
            goal = self.day_target(d, absences)
            week_goal += goal
            mark = "● " if d == date.today() else "  "
            self.rows.append(("day", d))
            shown = (fmt(tot) if tot else "0m") + (f" / {fmt(goal)}" if weekday else "")
            self.box.insert("end", f"{mark}{d:%a %d.%m.}   {shown}")
            met = weekday and tot >= goal
            self.box.itemconfig(self.box.size() - 1, fg=OK if met else ACCENT,
                                bg=SEL if d == self.target else BG)
            for a in ab:  # display only: never in entries.json, so never uploaded
                self.rows.append(("absence", a))
                length = "all day" if a["seconds"] >= self.target_s else fmt(a["seconds"])
                label = "Public holiday" if a.get("holiday") else "Absence"
                self.box.insert("end", f"   ⊘  {label}  {length}  ·  {a['title']}  (not uploaded)")
                self.box.itemconfig(self.box.size() - 1, fg=HOLIDAY if a.get("holiday") else MUTED)
            for e in es:
                self.rows.append(("entry", e))
                tick = CHECK if e.get("synced") else DOT
                self.box.insert("end", f"   {tick}  {e.get('code', '?'):<5} {fmt(e['seconds']):>7}   {e['desc']}")
                self.box.itemconfig(self.box.size() - 1, fg=MUTED if e.get("synced") else FG)
        self.total.config(text=f"Week {fmt(week_total)} / {fmt(week_goal)}",
                          fg=OK if week_total >= week_goal else ACCENT)
        self.show_target()

    def reload(self, quiet=False):
        """Re-read entries.json (e.g. after tempo_sync.py marked entries synced), keeping the selection."""
        ids = {e["id"] for e in self.selected_entries()}
        days = {self.rows[i][1] for i in self.box.curselection() if self.rows[i][0] == "day"}
        self.refresh()
        self.box.selection_clear(0, "end")
        for i, (kind, v) in enumerate(self.rows):
            if (kind == "entry" and v["id"] in ids) or (kind == "day" and v in days):
                self.box.selection_set(i)
        self.show_target()
        if not quiet:
            self.say("Reloaded from disk", "ok")

    def on_select(self, _=None):
        sel = self.box.curselection()
        if sel and not self.editing:
            self.target = self.row_day(self.rows[sel[0]])
            self.show_target()

    def goto(self, d):
        self.target = d
        self.refresh()

    def shift(self, n):
        self.goto(self.target + timedelta(days=n))

    def set_date(self, _):
        d = parse_date(self.date_var.get())
        if d:
            self.goto(d)
        else:
            self.say("Bad date. Use 24.02, 24.02.2026 or 2026-02-24", "err")

    def selected_entries(self):
        return [self.rows[i][1] for i in self.box.curselection() if self.rows[i][0] == "entry"]

    def check(self, text):
        """-> (parsed dict, None) if valid, else (None, error message)."""
        # Split on the first two hyphens, spaces optional: 'BE-2h-fix' and 'BE - 2h - fix' both work.
        # The description keeps any further hyphens. (Task codes must not contain a hyphen.)
        parts = [p.strip() for p in text.split("-", 2)]
        if len(parts) < 3:
            return None, "Format: CODE-time-description (spaces optional)"
        code, dur, desc = parts
        valid = {t["code"].lower(): t["code"] for t in self.tasks}
        if code.lower() not in valid:
            return None, "Unknown code '" + code + "'. Valid: " + ", ".join(valid.values())
        seconds = parse_duration(dur)
        if not seconds:
            return None, f"Can't read duration '{dur}'. Try 2, .5, 1h30m, 90min, 1:30"
        if self.max_s is not None and seconds > self.max_s:  # typed entries only; imported meetings don't go through check()
            return None, f"Max {fmt(self.max_s)} per entry (this is {fmt(seconds)}). Split it into blocks"
        if not desc:
            return None, "Add a description after the duration"
        return {"code": valid[code.lower()], "seconds": seconds, "desc": desc}, None

    def live_check(self, event=None, keep_history=False):
        if event is not None and event.keysym in ("Up", "Down", "Tab"):
            return  # recall() / complete() already ran the check
        if not keep_history:
            self.hist_i = -1  # typing leaves history browsing
        text = self.entry.get().strip()
        if not text:
            self.entry.config(highlightcolor=ACCENT, highlightbackground=PANEL)
            self.say("CODE-time-description  ·  Up: earlier lines  ·  Tab: autocomplete")
            return
        parsed, err = self.check(text)
        colour = OK if parsed else ERR
        self.entry.config(highlightcolor=colour, highlightbackground=colour)
        hint = None if self.editing else self.suggestion(self.entry.get())
        if parsed:
            self.say(f"OK: {parsed['code']} {DOT} {fmt(parsed['seconds'])} {DOT} {parsed['desc']}"
                     + (f"   (Tab: {hint[1]})" if hint else ""), "ok")
        elif hint:
            self.entry.config(highlightcolor=ACCENT, highlightbackground=ACCENT)
            self.say(f"Tab: {hint[1]}")
        else:
            self.say(err, "err")

    def add(self, _):
        """Enter in the input: add a new entry, or save the one being edited."""
        parsed, err = self.check(self.entry.get().strip())
        if err:
            self.say(err, "err")
            return
        self.snapshot()
        entries = load()
        self.hist_i = -1
        if self.editing:
            for e in entries:
                if e["id"] == self.editing:
                    e.update(parsed)
            msg = "Saved"
            self.last_days = []
        else:
            days = self.last_days = self.target_days()
            for d in days:
                entries.append({"id": uuid.uuid4().hex, "date": d.isoformat(), **parsed})
            msg = "Added" if len(days) == 1 else f"Added to {len(days)} days"
        save(entries)
        self.editing = None
        self.entry.delete(0, "end")
        self.refresh()
        if len(self.last_days) > 1:
            self.select_days(set(self.last_days))  # keep the same days selected for the next entry
        self.live_check()
        self.say(msg, "ok")

    def edit(self):
        sel = self.selected_entries()
        if len(sel) != 1:
            self.say("Select one entry to edit (double-click or F2)", "err")
            return
        e = sel[0]
        if e.get("synced"):
            self.say("Already synced to Tempo. Fix it in Tempo, or delete locally and re-add", "err")
            return
        self.editing = e["id"]
        self.entry.delete(0, "end")
        self.entry.insert(0, f"{e.get('code', '')} - {e['seconds'] // 60}min - {e['desc']}")
        self.entry.focus()
        self.entry.icursor("end")
        self.show_target()
        self.live_check()

    def cancel_edit(self):
        if self.editing:
            self.editing = None
            self.entry.delete(0, "end")
            self.show_target()
            self.live_check()
            self.say("Edit cancelled")

    def copy(self):
        self.clip = [dict(e) for e in self.selected_entries()]
        if self.clip:
            n = len(self.clip)
            self.say(f"Copied {n} entr{'y' if n == 1 else 'ies'}. Click a day, then Paste ({MOD_NAME}+V)", "ok")
        else:
            self.say(f"Select one or more entries first ({MOD_NAME}/Shift+click)", "err")

    def paste(self):
        if not self.clip:
            self.say("Nothing copied yet", "err")
            return
        self.snapshot()
        entries, days = load(), self.target_days()
        for d in days:
            for e in self.clip:
                entries.append({"id": uuid.uuid4().hex, "date": d.isoformat(),
                                "code": e.get("code"), "seconds": e["seconds"], "desc": e["desc"]})
        save(entries)
        self.refresh()
        self.say(f"Pasted {len(self.clip)} into {len(days)} day{'s' if len(days) > 1 else ''}", "ok")

    def import_meetings(self):
        """Add meetings that have already ended, from Outlook, to every selected day."""
        if self.importing:
            return
        if not self.meeting_code:
            self.say("Set 'meeting_code' in tasks.json to a code from your task list", "err")
            return
        import outlook
        days = self.target_days()
        self.importing = True
        self.say("Reading Outlook calendar...")
        result = {}

        def work():
            try:
                result["events"] = outlook.read(min(days), max(days), holiday_calendars=self.holiday_names())
            except Exception as e:
                result["error"] = str(e)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, lambda: self.finish_import(result, days))

    def outlook_flag(self, key):
        """Outlook import switches default to on; null or false turns one off."""
        return bool(self.cfg.get(key, True))

    def holiday_names(self):
        """Sub-calendars to read public holidays from; empty when holidays are switched off."""
        if not self.outlook_flag("import_holidays_from_outlook"):
            return []
        return list(self.cfg.get("holiday_outlook_calendar_names") or [])

    def merge_absences(self, events, day_keys):
        """Absences and holidays are display-only (they lower that day's target). Re-reading replaces
        the enabled kinds for `day_keys`, so one removed from Outlook disappears here too, while a kind
        that is switched off is left as it was. Returns the fresh ones."""
        import outlook
        pattern = self.cfg.get("absence_match", outlook.ABSENCE_PATTERN)
        kinds = set()  # is_holiday values we may replace
        if self.outlook_flag("import_absences_from_outlook"):
            kinds.add(False)
        if self.holiday_names():
            kinds.add(True)
        fresh = [a for a in outlook.absences(events, pattern)
                 if a["date"] in day_keys and bool(a.get("holiday")) in kinds]
        keep = [a for a in store.load_absences()
                if a["date"] not in day_keys or bool(a.get("holiday")) not in kinds]
        store.save_absences(keep + fresh)
        return fresh

    def load_absences_ahead(self):
        """On startup: absences and public holidays for the next month, read in the background.
        Meetings are not touched. Same polling pattern as import_meetings."""
        import outlook
        first = date.today()
        days = {(first + timedelta(days=i)).isoformat() for i in range(32)}
        result = {}

        def work():
            try:
                result["events"] = outlook.read(first, first + timedelta(days=31), holiday_calendars=self.holiday_names())
            except Exception as e:
                result["error"] = str(e)

        def poll():
            if not result:
                self.root.after(200, poll)
                return
            if "error" in result:
                self.say(result["error"], "err")
                return
            self.merge_absences(result["events"], days)
            self.reload(quiet=True)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    def finish_import(self, result, days):
        if not result:  # still running
            self.root.after(200, lambda: self.finish_import(result, days))
            return
        self.importing = False
        if "error" in result:
            self.say(result["error"], "err")
            return
        import outlook
        pattern = self.cfg.get("absence_match", outlook.ABSENCE_PATTERN)
        fresh = self.merge_absences(result["events"], {d.isoformat() for d in days})

        self.snapshot()
        now, entries = datetime.now(), load()
        have = {e.get("outlook") for e in entries if e.get("outlook")}
        added = dupes = upcoming = 0
        for m in outlook.meetings(result["events"], pattern):
            if m["start"].date() not in days:
                continue
            if m["end"] > now:
                upcoming += 1
                continue
            seconds = round((m["end"] - m["start"]).total_seconds() / 60) * 60
            key = f"{m['start']:%Y-%m-%dT%H:%M}|{m['subject']}"
            if seconds < 60:
                continue
            if key in have:
                dupes += 1
                continue
            entries.append({"id": uuid.uuid4().hex, "date": m["start"].date().isoformat(),
                            "code": self.meeting_code, "seconds": seconds,
                            "desc": m["subject"], "outlook": key})
            added += 1
        save(entries)
        self.refresh()
        if len(days) > 1:
            self.select_days(set(days))
        absent_days = len({a["date"] for a in fresh})
        extra = "".join([f", {dupes} already added" if dupes else "", f", {upcoming} not finished yet" if upcoming else "",
                         f", {absent_days} absence day{'s' if absent_days != 1 else ''}" if absent_days else ""])
        self.say(f"Added {added} meeting{'s' if added != 1 else ''}{extra}", "ok")

    def delete(self):
        gone = {e["id"] for e in self.selected_entries()}
        gone_absences = {r[1]["key"] for i in self.box.curselection() if (r := self.rows[i])[0] == "absence"}
        if gone_absences:  # absence rows live in their own file and aren't covered by Undo
            store.save_absences([a for a in store.load_absences() if a["key"] not in gone_absences])
        if gone:
            self.snapshot()
            save([e for e in load() if e["id"] not in gone])
        if gone or gone_absences:
            n = len(gone) + len(gone_absences)
            self.say(f"Deleted {n} locally (Jira untouched)")
            self.refresh()


def startup_link():
    if MAC:
        return Path.home() / "Library/LaunchAgents/com.worklog.widget.plist"
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Worklog.lnk"


def install_startup():
    """Launches the widget at login: a Startup-folder shortcut on Windows, a LaunchAgent on macOS."""
    link = startup_link()
    if MAC:
        import plistlib
        link.parent.mkdir(parents=True, exist_ok=True)
        plist = {"Label": "com.worklog.widget", "RunAtLoad": True, "WorkingDirectory": str(Path(__file__).parent),
                 "ProgramArguments": [sys.executable, str(Path(__file__).resolve())]}
        link.write_bytes(plistlib.dumps(plist))
        print("Installed:", link, "(starts at next login)")
        return
    exe = Path(sys.executable)
    exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:WL_LINK);"
              "$s.TargetPath=$env:WL_EXE;$s.Arguments=$env:WL_ARGS;$s.WorkingDirectory=$env:WL_DIR;$s.Save()")
    env = dict(os.environ, WL_LINK=str(link), WL_EXE=str(exe), WL_DIR=str(Path(__file__).parent),
               WL_ARGS=f'"{Path(__file__).resolve()}" --hidden')
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, check=True)
    print("Installed:", link)


def uninstall_startup():
    startup_link().unlink(missing_ok=True)
    print("Removed:", startup_link())


if __name__ == "__main__":
    if "--install-startup" in sys.argv:
        install_startup()
        sys.exit()
    if "--uninstall-startup" in sys.argv:
        uninstall_startup()
        sys.exit()

    tray = None
    if sys.platform == "win32":
        import winshell
        keep = winshell.single_instance()
        if keep is None:
            sys.exit("Worklog is already running (check the tray).")
        hotkey = store.load_config(CONFIG).get("hotkey", "win+shift+w")
        tray = winshell.Tray(hotkey)
    root = tk.Tk()
    Widget(root, tray)
    if "--hidden" in sys.argv and tray:
        root.withdraw()
    root.mainloop()
    if tray:
        tray.close()
