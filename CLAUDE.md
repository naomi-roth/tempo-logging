# CLAUDE.md

Personal worklog tool: a Tkinter widget for logging time, plus an uploader to **Jira Server/Data Center** (Tempo reads native Jira worklogs). Windows only, Python 3 stdlib only (no pip dependencies, keep it that way). User docs are in `README.md`.

## Layout

| File | Role |
|---|---|
| `worklog.py` | The widget (`Widget` class), duration/date parsing, `confirm()` dialog, `--install-startup` |
| `tempo_sync.py` | Upload logic + CLI: `plan_jobs`, `post_jobs`, `call`, `check_login`, `auth_headers`, `normalize_cookie` |
| `store.py` | `load`/`save`/`mark_synced` for `~/.worklog/entries.json`, guarded by a shared `LOCK` |
| `outlook.py` | `read(first, last)` returns raw calendar events via a PowerShell COM script; `meetings()` and `absences()` classify them (absence = BusyStatus 3 or subject regex; split per day with the overlap in seconds) |
| `winshell.py` | ctypes: tray icon (`Tray`), global hotkey, balloon `notify`, `style_titlebar`, `single_instance` |
| `tickets.py` | `fetch(cfg)`: my in-progress tickets from a second, read-only Jira (Bearer PAT, `tickets.token` in `tasks.json`). `Widget.load_tickets`/`pick_ticket` use it |
| `tasks.json` | Config (read with `store.load_config`, which allows `//` comments; never use plain `json.loads` on it) (Jira URL, task codes, settings). Keys are listed in `README.md` |
| `cookie.txt` | **Secret**, git-ignored. Format example in `example/cookie.txt` |

Data lives outside the repo: `~/.worklog/entries.json`, a list of
`{"id", "date": "YYYY-MM-DD", "code", "seconds", "desc"}`. Added by import: `"outlook": "<start>|<subject>"` (dedupe key). Added by upload: `"synced": true, "task", "jiraWorklogId"`.
**Synced vs unsynced is just the `synced` flag.** `plan_jobs` uploads every entry without it.

## Auth model (important)

PATs are disabled on this Jira. Requests authenticate with browser session cookies: `MRHSession` (VW F5 gateway) **and** `JSESSIONID` (Jira) are both required, plus the header `X-Atlassian-Token: no-check` on POSTs. A gateway rejection shows up as a 302 or 401, not JSON. `tempo_sync.call` turns that into a `SyncError` with `.fatal`.
`normalize_cookie` reads one `name=value` per line (spaces dropped, split on the first `=`, `#` comment lines, `;` tolerated), so don't parse `cookie.txt` anywhere else.

Uploads use the native `POST /rest/api/2/issue/<key>/worklog` (not Tempo's own API). `started` needs a UTC offset (`+0200`).

## Conventions and gotchas

- **Do not call the real Jira or read the user's calendar/cookies unprompted.** Tests must use fakes (see below). The user's cookie is a live credential: never print it, write it to files, or commit it.
- **Tkinter is single-threaded.** Background work (Outlook read, login check, upload) runs in a thread and writes into a dict; the Tk thread polls it with `root.after(200, ...)`. Never touch widgets from a worker thread. Follow `import_meetings` / `sync` / `check_login`.
- **Every mutation of entries calls `self.snapshot()` first** (Undo). `undo()` restores the snapshot but re-applies `SYNC_KEYS` from the current file so an undo can never un-sync an entry (that would double-post).
- Task codes must **not contain hyphens**: the input is split on the first two `-`.
- A bare number in the time field means **hours**; minutes need a unit. `.5` is half an hour.
- `check()` enforces `max_entry_hours` on typed entries only. Imported meetings bypass it on purpose.
- Outlook: **classic Outlook only** (COM). `Items.Restrict` returned nothing on this machine, so `outlook.py` walks the sorted items instead. Don't reintroduce `Restrict`. Only `ResponseStatus` 1 (organised) and 3 (accepted) are imported.
- **Absences are display-only.** They live in `~/.worklog/absences.json` (`store.load_absences/save_absences`), never in `entries.json`, so `plan_jobs` can never upload them. `Widget.day_target(d)` = daily target minus that day's absence seconds (min 0, weekends 0); use it instead of `self.target_s` for any "is this day complete" logic. Absence rows are `("absence", dict)` in `self.rows`. They are not covered by Undo.
- The tray runs in its own thread with a hidden window. It talks to Tk through `Tray.events` (a queue polled by `Widget.poll`). The tray icon is `worklog.ico` if present.
- Start times are not stored; `plan_jobs` stacks entries from `day_start` per day in file order.
- Source files are UTF-8. Print with `PYTHONIOENCODING=utf-8` when testing from a cp1250 console (✓ and · characters).
- Match the existing style: small functions, short comments explaining *why*, colours as the constants near the top of `worklog.py`.

## Running and testing

```powershell
python worklog.py                  # widget
python tempo_sync.py               # dry run
python -m py_compile worklog.py tempo_sync.py store.py winshell.py outlook.py
```

There is no test suite. Smoke-test by building a `Widget` against a temp file and faking the network:

```python
import tempfile, pathlib, tkinter as tk, store, worklog as w, tempo_sync as ts
store.STORE = pathlib.Path(tempfile.mkdtemp()) / "e.json"      # never touch the real entries
ts.check_login = lambda cfg: (True, "Test")                    # no network
root = tk.Tk(); x = w.Widget(root)                             # then call x.check/add/undo/sync...
```
Replace `w.confirm`, `ts.auth_headers` and `ts.post_jobs` with fakes to exercise Sync. Use `x.root.update()` plus short sleeps to let polled threads finish.

## Not built (ideas the user has heard)

Edit/delete synced entries in Jira, fill-the-gap button, auto-reload on file change, timer, week summary/CSV export.
