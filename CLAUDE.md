# CLAUDE.md

Personal worklog tool: a Tkinter widget for logging time, plus an uploader to **Jira Server/Data Center** (Tempo reads native Jira worklogs). Windows only, Python 3 stdlib only (no pip dependencies, keep it that way). User docs are in `README.md`.

## Layout

| File | Role |
|---|---|
| `worklog.py` | The widget (`Widget` class), duration/date parsing, `confirm()` dialog, `--install-startup` |
| `tempo_sync.py` | Upload logic + CLI: `plan_jobs`, `post_jobs`, `call`, `check_login`, `auth_headers`, `normalize_cookie` |
| `store.py` | `load`/`save`/`mark_synced` for `~/.worklog/entries.json`, guarded by a shared `LOCK`; `load_absences`/`save_absences`; `load_config(path)` for `tasks.json` |
| `outlook.py` | `read(first, last, holiday_calendars=())` returns raw calendar events via a PowerShell COM script (events from the named sub-calendars carry `holiday: True` and count as absences); `meetings()` and `absences()` classify them (absence = BusyStatus 3 or subject regex; split per day with the overlap in seconds) |
| `winshell.py` | ctypes: tray icon (`Tray`), global hotkey, balloon `notify`, `style_titlebar`, `single_instance` |
| `tickets.py` | `fetch(cfg)`: my in-progress tickets from a second, read-only Jira (Bearer PAT, `tickets.token` in `tasks.json`). `Widget.load_tickets`/`pick_ticket`/`ticket_tip` use it. Never writes to that Jira; raises `TicketError` with a short message shown in the list header |
| `tasks.json` | **Secret** (holds `tickets.token`), git-ignored. Read it only with `store.load_config`, which allows `//` comments; never plain `json.loads`. `example/tasks.json` is the tracked template and documents every key; keep it, the README key table and the code in step when you add a setting |
| `cookie.txt` | **Secret**, git-ignored. Format example in `example/cookie.txt` |

Data lives outside the repo: `~/.worklog/entries.json`, a list of
`{"id", "date": "YYYY-MM-DD", "code", "seconds", "desc"}`. Added by import: `"outlook": "<start>|<subject>"` (dedupe key). Added by upload: `"synced": true, "task", "jiraWorklogId"`.
**Synced vs unsynced is just the `synced` flag.** `plan_jobs` uploads every entry without it.

## Auth model (important)

PATs are disabled on this Jira. Requests authenticate with browser session cookies: `MRHSession` (VW F5 gateway) **and** `JSESSIONID` (Jira) are both required, plus the header `X-Atlassian-Token: no-check` on POSTs. A gateway rejection shows up as a 302 or 401, not JSON. `tempo_sync.call` turns that into a `SyncError` with `.fatal`.
`normalize_cookie` reads one `name=value` or `name:"value"` per line (spaces dropped, quotes stripped, split on the first `=` or `:`, `#` comment lines, `;` tolerated), so don't parse `cookie.txt` anywhere else.

The **tickets Jira** (`tickets.url`, a different server) is separate: read-only `GET /rest/api/2/search` with `Authorization: Bearer <PAT>` (`tickets.token` in `tasks.json`). Server/Data Center only; Jira Cloud (email + API token) is not supported. No cookie is involved.

Uploads use the native `POST /rest/api/2/issue/<key>/worklog` (not Tempo's own API). `started` needs a UTC offset (`+0200`).

## Conventions and gotchas

- **Do not call the real Jira (either one) or read the user's calendar/cookies/token unprompted.** Tests must use fakes (see below). `cookie.txt` and `tickets.token` are live credentials: never print them, copy them into other files (including `example/`), or commit them. To check that `tasks.json` parses, print only key names.
- **Tkinter is single-threaded.** Background work (Outlook read, login check, upload) runs in a thread and writes into a dict; the Tk thread polls it with `root.after(200, ...)`. Never touch widgets from a worker thread. Follow `import_meetings` / `sync` / `check_login` / `load_tickets`.
- **Every mutation of entries calls `self.snapshot()` first** (Undo). `undo()` restores the snapshot but re-applies `SYNC_KEYS` from the current file so an undo can never un-sync an entry (that would double-post).
- Task codes must **not contain hyphens**: the input is split on the first two `-`.
- A bare number in the time field means **hours**; minutes need a unit. `.5` is half an hour.
- `check()` enforces `max_entry_hours` on typed entries only (`-1` = no limit, stored as `self.max_s = None`). Imported meetings bypass it on purpose.
- Outlook switches in `tasks.json` (all default true, `null`/`false` = off): `import_meetings_from_outlook` controls only the Meetings button; `import_absences_from_outlook` and `import_holidays_from_outlook` (needs `holiday_outlook_calendar_names`, sub-calendars of the main Calendar) control what `load_absences_ahead` reads on startup (today + 31 days) and what the Meetings button also merges. `merge_absences` replaces only the enabled kinds. Holiday rows are absences with `"holiday": true`, shown in yellow (`HOLIDAY`).
- Outlook: **classic Outlook only** (COM). `Items.Restrict` returned nothing on this machine, so `outlook.py` walks the sorted items instead. Don't reintroduce `Restrict`. Only `ResponseStatus` 1 (organised) and 3 (accepted) are imported.
- **Absences are display-only.** They live in `~/.worklog/absences.json` (`store.load_absences/save_absences`), never in `entries.json`, so `plan_jobs` can never upload them. `Widget.day_target(d)` = daily target minus that day's absence seconds (min 0, weekends 0); use it instead of `self.target_s` for any "is this day complete" logic. Absence rows are `("absence", dict)` in `self.rows`. They are not covered by Undo.
- The tray runs in its own thread with a hidden window. It talks to Tk through `Tray.events` (a queue polled by `Widget.poll`). The tray icon is `worklog.ico` if present.
- **Ticket line.** Clicking a ticket fills `<code> -  - <title>` and puts the cursor after `<code> - ` (via `root.after`, or the listbox steals the focus back). `[...]` text is stripped from the title. The line must still pass `check()`: the description keeps any further hyphens, so titles with `-` are fine.
- **Hover hints** use the `Tip` class; pass `tip="..."` to `button()`, or `Tip(widget, text)`; `per_row=True` takes a callable `(event) -> text`. Hints must describe what the control really does.
- **`cookie.txt`** is one `name=value` per line; edit the parser only in `normalize_cookie`.
- Start times are not stored; `plan_jobs` stacks entries from `day_start` per day in file order.
- Source files are UTF-8. Print with `PYTHONIOENCODING=utf-8` when testing from a cp1250 console (✓ and · characters).
- Match the existing style: small functions, short comments explaining *why*, colours as the constants near the top of `worklog.py`.

## Running and testing

```powershell
python worklog.py                  # widget
python tempo_sync.py               # dry run
python -m py_compile worklog.py tempo_sync.py store.py winshell.py outlook.py tickets.py
```

There is no test suite. Smoke-test by building a `Widget` against a temp file and faking the network:

```python
import tempfile, pathlib, tkinter as tk, store, worklog as w, tempo_sync as ts
store.STORE = pathlib.Path(tempfile.mkdtemp()) / "e.json"      # never touch the real entries
ts.check_login = lambda cfg: (True, "Test")                    # no network
import tickets; tickets.fetch = lambda cfg: [{"key": "X-1", "summary": "[T] Demo - one"}]
root = tk.Tk(); x = w.Widget(root)                             # then call x.check/add/undo/sync...
```
Point `w.CONFIG` at a temp config file (it needs `tasks`, and `tickets` to show the ticket list). Replace `w.confirm`, `ts.auth_headers` and `ts.post_jobs` with fakes to exercise Sync. Use `x.root.update()` plus short sleeps to let polled threads finish.

## Not built (ideas the user has heard)

Edit/delete synced entries in Jira, fill-the-gap button, auto-reload on file change, timer, week summary/CSV export.
