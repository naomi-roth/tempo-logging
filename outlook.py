"""Read events from classic Outlook's local calendar (COM via PowerShell, no dependencies).

Works only with classic Outlook (OUTLOOK.EXE). New Outlook (olk.exe) has no local calendar API.
`read()` returns every event in a date range; `meetings()` and `absences()` classify them.
"""
import json, os, re, subprocess
from datetime import datetime, timedelta

# Outlook marks out-of-office as BusyStatus 3; the subject pattern catches absences shown differently.
ABSENCE_PATTERN = r"absence|out of office|\booo\b|vacation|holiday|sick"

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$from = [datetime]::ParseExact($env:OL_FROM, 'yyyy-MM-dd', $null)
$to = [datetime]::ParseExact($env:OL_TO, 'yyyy-MM-dd', $null).AddDays(1)
$cal = (New-Object -ComObject Outlook.Application).GetNamespace('MAPI').GetDefaultFolder(9)
$items = $cal.Items
$items.IncludeRecurrences = $true
$items.Sort('[Start]')
$out = @()
$seen = 0
# Items.Restrict returned nothing here, so walk the date-sorted items and stop once past the range.
foreach ($i in $items) {
  if (++$seen -gt 20000) { break }
  if ($i.Start -ge $to) { break }
  if ($i.End -le $from) { continue }
  $out += [pscustomobject]@{
    subject = $i.Subject; start = $i.Start.ToString('s'); end = $i.End.ToString('s')
    allDay = [bool]$i.AllDayEvent; busy = [int]$i.BusyStatus
    response = [int]$i.ResponseStatus; status = [int]$i.MeetingStatus }
}
ConvertTo-Json -InputObject @($out) -Compress
"""


def read(first, last, timeout=120):
    """All events overlapping `first`..`last` (dates, inclusive) as dicts with datetime start/end.
    Raises RuntimeError with a readable message."""
    env = dict(os.environ, OL_FROM=first.isoformat(), OL_TO=last.isoformat())
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", SCRIPT], env=env,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Outlook did not answer in time. Open Outlook once and try again")
    if r.returncode:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("Can't read Outlook (classic Outlook required): " + (err[0] if err else "unknown error"))
    raw = json.loads(r.stdout.decode("utf-8-sig") or "[]")
    raw = [raw] if isinstance(raw, dict) else raw
    return [{**e, "subject": (e["subject"] or "(no subject)").strip(),
             "start": datetime.fromisoformat(e["start"]), "end": datetime.fromisoformat(e["end"])} for e in raw]


def is_absence(event, pattern=ABSENCE_PATTERN):
    return event["busy"] == 3 or bool(re.search(pattern, event["subject"], re.I))


def _skipped(event):
    return event["status"] in (5, 7) or event["response"] == 4  # cancelled or declined


def meetings(events, pattern=ABSENCE_PATTERN):
    """Timed meetings you organised or accepted. Outlook doesn't record attendance, so that is the
    stand-in for 'attended'. All-day events, cancelled meetings and absences are left out."""
    return [e for e in events
            if not e["allDay"] and not _skipped(e) and e["response"] in (1, 3) and not is_absence(e, pattern)]


def absences(events, pattern=ABSENCE_PATTERN):
    """Absence events split per calendar day: [{'date', 'seconds', 'title', 'key'}].
    'seconds' is the overlap with that day, so an all-day event gives 24h and a 12:00-16:00 one gives 4h."""
    out = []
    for e in events:
        if _skipped(e) or not is_absence(e, pattern):
            continue
        day = e["start"].date()
        while datetime.combine(day, datetime.min.time()) < e["end"]:
            lo = max(e["start"], datetime.combine(day, datetime.min.time()))
            hi = min(e["end"], datetime.combine(day + timedelta(days=1), datetime.min.time()))
            seconds = round((hi - lo).total_seconds())
            if seconds > 0:
                out.append({"date": day.isoformat(), "seconds": seconds, "title": e["subject"],
                            "key": f"{e['start']:%Y-%m-%dT%H:%M}|{e['subject']}|{day}"})
            day += timedelta(days=1)
    return out
