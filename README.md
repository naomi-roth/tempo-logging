# Worklog → Tempo

A small desktop widget for jotting down what you worked on, plus a script that uploads it to **Jira Server/Data Center** (Tempo reads native Jira worklogs). Windows, Python 3 standard library only.

```
CODE-time-description      BE-2h-fixed a bug
```

## Files

| File | Purpose |
|---|---|
| `worklog.py` | The widget: week view, tray icon, hotkey, Outlook import, ticket list, sync button |
| `tempo_sync.py` | Uploads entries to Jira. Used by the widget and runnable as a CLI |
| `tickets.py` | Reads your in-progress tickets from a second, read-only Jira |
| `store.py` | Reads and writes `~/.worklog/entries.json` (shared, thread-safe) |
| `outlook.py` | Reads meetings and absences from **classic** Outlook via COM |
| `winshell.py` | Tray icon, global hotkey, notifications, dark title bar (ctypes) |
| `tasks.json` | Jira URLs, task codes, the read token and settings. **Contains a secret, git-ignored.** See `example/tasks.json` |
| `cookie.txt` | Your session cookie for the upload Jira. **Secret, git-ignored.** See `example/cookie.txt` |
| `~/.worklog/entries.json` | Your entries (created automatically) |
| `~/.worklog/absences.json` | Absences imported from Outlook (display only, created automatically) |

## Setup

1. **Python 3.8+** on Windows (`tkinter` is bundled).
2. **Copy `example/tasks.json` to `tasks.json`** and edit it (see [`tasks.json`](#tasksjson)).
3. **Get your credentials** (next section). There are two, for two different Jira servers.
4. Run `python worklog.py`.

## Credentials

The tool talks to two Jira servers and each needs its own credential. Neither is ever sent anywhere else.

| | Upload Jira - Tempo (`jira_url`) | Tickets Jira (`tickets.url`) |
|---|---|---|
| **What it does** | Receives your worklogs when you click **Sync to Jira** (write access) | Lists your in-progress tickets in the widget (read only) |
| **Credential** | Browser session cookie: `MRHSession` + `JSESSIONID` | Personal access token (PAT) |
| **Stored in** | `cookie.txt` | `tasks.json`, key `tickets.token` |
| **Lifetime** | Expires roughly daily | Until it expires or you revoke it |
| **Required?** | Yes, to sync | No. Without a `tickets` block the ticket list is hidden |

### Upload Jira: session cookie

PATs are disabled on this Jira, so the scripts use your browser session. Both cookies are required: `MRHSession` gets you through the VW gateway, `JSESSIONID` is the Jira login.

1. Open Jira in the browser, press `F12`, open the Network tab, click any `jira...` request.
2. Copy the `MRHSession` and `JSESSIONID` values from the request's Cookie header.
3. Save them in `cookie.txt` (copy `example/cookie.txt` for the format).

Put one `name=value` per line, two lines in total (see `example/cookie.txt`). Spaces are ignored, the line is split at the first `=`, and `#` starts a comment line. No `;` is needed, though a pasted `a=1; b=2` header still works. Re-copy it when the `● Jira` badge in the widget turns red. You can also use the `JIRA_COOKIE` environment variable (name set by `cookie_env`), or a PAT in `JIRA_TOKEN` if your Jira ever allows them.

### Tickets Jira: read-only token

1. Log in to the tickets Jira, click your avatar → **Profile** → **Personal Access Tokens** → **Create token**.
2. Give it a name and an expiry. It only needs to read, so don't give it more.
3. Copy the token (Jira shows it once) and put it in `tasks.json`: `"tickets": { ..., "token": "<token>" }`.

`tasks.json` is git-ignored, so the token isn't committed. Don't paste that file into chat or into `example/tasks.json`.

The token is sent as `Authorization: Bearer ...`, which is how Jira Server/Data Center PATs work. Jira Cloud (`*.atlassian.net`) uses email plus API token and is **not supported**.

### `tasks.json`

```json
{
  "jira_url": "https://jira.example.com",
  "hotkey": "ctrl+alt+j",
  "day_start": "09:00",
  "tickets": { "url": "https://other-jira.example.com/jira", "token": "<READ_TOKEN>", "code": "BE" },
  "tasks": [
    { "code": "BE",  "name": "Backend development", "issueKey": "PROJ-1" },
    { "code": "MTG", "name": "Meetings",            "issueKey": "PROJ-2" }
  ]
}
```

| Key | Default | Meaning |
|---|---|---|
| `jira_url` | required | Upload Jira. Base URL without a trailing slash; keep a context path such as `/jira` if it has one |
| `cookie_env` | `JIRA_COOKIE` | Environment variable that may hold the cookie instead of `cookie.txt` |
| `tickets.url` | none | Tickets Jira base URL. Leave out the whole `tickets` block to hide the ticket list |
| `tickets.token` | none | Read-only PAT for the tickets Jira (see [Credentials](#credentials)) |
| `tickets.code` | `BE` | Task code put at the start of the line when you click a ticket |
| `tickets.jql` | my tickets in "In Progress" | JQL for the list. Default: `assignee = currentUser() AND statusCategory = 'In Progress' ORDER BY updated DESC` |
| `tasks[].code` | required | Short label you type. Case-insensitive, **no hyphens** |
| `tasks[].issueKey` | required | Jira issue the time is logged against |
| `day_start` | `09:00` | When the first entry of a day starts; later ones are stacked after it |
| `hotkey` | `win+shift+w` | Global show/hide key. Modifiers (`ctrl`, `alt`, `shift`, `win`) plus a letter, digit or `F1`–`F12` |
| `target_hours` | `8` | Daily target per weekday |
| `max_entry_hours` | `4` | Longest entry you can type (imported meetings are exempt) |
| `meeting_code` | `MTG` | Code used for imported Outlook meetings |
| `reminder_time` | `15:00` | Weekday nudge if you're under target. `""` turns it off |
| `absence_match` | `absence\|out of office\|\booo\b\|vacation\|holiday\|sick` | Regex (case-insensitive) on the event subject that marks an absence. Events Outlook shows as "Out of office" always count |
| `workers` | `4` | Parallel uploads |

## The widget

```powershell
python worklog.py
```

**Entering time.** One line, press Enter: `CODE-time-description`. Spaces around the hyphens are optional, and the description may contain more hyphens.

| Time | Means |
|---|---|
| `2`, `2h`, `2hrs` | 2 hours (a bare number is **hours**) |
| `.5`, `0.5`, `1,5` | 30 min, 30 min, 1h30 |
| `45m`, `90min` | minutes need a unit |
| `1h30m`, `1:30` | 1h30 |

**My tickets.** If `tickets` is configured, the list above the input shows your in-progress tickets (loaded at start; click the header to reload). Click one to fill the input with `BE -  - <title>` and the cursor on the duration, so you only type the time (`2h`) and press Enter. Text in square brackets, such as `[TAG]`, is removed from the title. Hover a row to see the key and the full title. The header goes red with the reason if the token is missing or rejected, or the server is unreachable (it may need the VPN). The code comes from `tickets.code`.

**Hints.** Hover over a button or the `● Jira` badge for a short explanation.

**Week view.** Days are listed Mon–Fri (weekends only when they have entries). Each day shows `logged / target` and turns green at target. `‹` `›` move a week, `Today` jumps back, or type a date (`24.02`, `24.02.2026`, `2026-02-24`).

**Adding to days.** Entries go to the day you last clicked. Ctrl/Shift+click several day headers, or press **Mon–Fri**, to add one entry to all of them.

| Action | How |
|---|---|
| Edit | Double-click, `F2` or **Edit**. `Enter` saves, `Esc` cancels |
| Copy / paste | Select entries, `Ctrl+C`, click a day (or several), `Ctrl+V` |
| Delete | Select, `Delete` |
| Undo | `Ctrl+Z` or **Undo** (last 30 changes; never un-syncs an entry) |
| Recall earlier lines | `↑` / `↓` in the input |
| Autocomplete | `Tab` completes a task code (`b` → `BE-`), then earlier lines |
| Reload from disk | `F5` or **↻ Reload** (e.g. after a CLI sync) |

A ✓ marks entries already uploaded. Those can't be edited. Deleting an entry only removes it locally, never from Jira.

**Meetings.** Select days and click **Meetings** to add meetings from Outlook that have **already ended** and that you **accepted or organised**. All-day, tentative, unanswered, declined and cancelled ones are skipped. Importing twice doesn't duplicate. **This works only with classic ("old") Outlook.** The calendar is read through Outlook's local COM interface, which classic desktop Outlook provides. New Outlook (the one with the "New Outlook" toggle on) and Outlook on the web have no local calendar API, so **Meetings** finds nothing. Switch the toggle off and keep classic Outlook installed, and ideally running, when you import. Absences use the same route.

**Absences.** The same **Meetings** click also reads your absence events (Outlook "out of office" events, or a subject matching `absence`, `out of office`, `ooo`, `vacation`, `holiday` or `sick`; change it with `absence_match` in `tasks.json`). They appear under the day as `⊘ Absence all day (not uploaded)` and lower that day's target: a 4h absence makes the target 4h, an all-day absence makes it 0 (the day turns green), and the 15:00 nudge follows the lowered target. Multi-day absences, such as 8–9 October, are split per day. Future absences are imported too. Absences are stored separately in `~/.worklog/absences.json` and are **never uploaded**. Re-importing replaces the absences of the selected days. Select an absence row and press `Delete` to remove it.

**Sync to Jira.** Click **Sync to Jira**, check the summary, confirm. The `● Jira` badge shows whether your cookie works (click to recheck).

**Tray and hotkey.** The X and `Esc` hide the widget to the tray. The hotkey toggles it. Right-click the tray icon to quit.

**Start at login.**

```powershell
python worklog.py --install-startup     # adds a shortcut to the Startup folder, starts hidden
python worklog.py --uninstall-startup
```

**End-of-day nudge.** At `reminder_time` on weekdays, a tray notification says how many hours are missing.

## Command-line sync

```powershell
python tempo_sync.py                  # dry run: every unsynced entry
python tempo_sync.py --apply          # upload them
python tempo_sync.py --date 2026-10-05 --apply
python tempo_sync.py --from 2026-10-01 --to 2026-10-05 --apply
```

- Dry run is the default. `--apply` posts and marks entries synced; reruns skip synced entries.
- Entries are posted in parallel over keep-alive connections. After an auth or network error it stops instead of retrying everything.
- Start times are stacked from `day_start` in the order entries were added.
- An unknown task code asks you to pick a task or skip.

## Notes and limitations

- Worklogs go to `/rest/api/2/issue/<key>/worklog`, with the header `X-Atlassian-Token: no-check`.
- The tools never edit or delete existing Jira worklogs. Fix a wrong synced entry in Jira/Tempo.
- Outlook doesn't record attendance, so "accepted" stands in for "attended".
- Don't run a CLI sync and a widget edit at the exact same moment. Both write `entries.json` (writes are locked, but use **Reload** afterwards).
- `cookie.txt` and the token in `tasks.json` are live credentials. Keep them out of git and chat.
- The tickets Jira is only ever read (`GET /rest/api/2/search`). Nothing is written to it.
