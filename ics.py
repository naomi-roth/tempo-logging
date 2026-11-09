"""Read events from an ICS calendar feed (a published URL or a local .ics file). Python stdlib only.

`read()` returns raw events; `meetings()` and `absences()` classify them. The feed has no response
status, so every event counts as accepted unless it is tentative; cancelled events are skipped.
The feed URL is a secret (anyone with it can read the calendar): never put it in an error message.
"""
import re, urllib.error, urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# Out-of-office is BusyStatus 3; the subject pattern catches absences a calendar shows differently.
ABSENCE_PATTERN = r"absence|out of office|\booo\b|vacation|holiday|sick"
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
BUSY = {"FREE": 0, "TENTATIVE": 1, "BUSY": 2, "OOF": 3}  # same numbers as Outlook's BusyStatus


def fetch(source, timeout=30):
    src = source.strip()
    if src.lower().startswith("webcal://"):
        src = "https://" + src[9:]
    if re.match(r"https?://", src, re.I):
        try:
            with urllib.request.urlopen(src, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Calendar feed refused the request (HTTP {e.code}). The link may have expired")
        except OSError:
            raise RuntimeError("Can't download the calendar feed. Check your connection or VPN")
    else:
        try:
            raw = Path(src).expanduser().read_bytes()
        except OSError as e:
            raise RuntimeError(f"Can't read the calendar file: {e.strerror}")
    return raw.decode("utf-8-sig", "replace")


# --- text -> properties -------------------------------------------------------------------------

def unfold(text):
    """Long lines are folded: a line starting with a space or tab continues the previous one."""
    lines = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        elif ln:
            lines.append(ln)
    return lines


def split_line(ln):
    """'NAME;P=V:value' -> (NAME, {P: V}, value). A colon inside a quoted parameter doesn't split."""
    quoted = False
    for i, c in enumerate(ln):
        if c == '"':
            quoted = not quoted
        elif c == ":" and not quoted:
            head, value = ln[:i], ln[i + 1:]
            break
    else:
        return None
    name, *ps = head.split(";")
    params = {}
    for p in ps:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name.upper(), params, value


def unescape(s):
    return re.sub(r"\\([nN,;\\])", lambda m: " " if m[1] in "nN" else m[1], s)


def first_prop(props, name):
    v = props.get(name)
    return v[0] if v else ({}, None)


def parse_offset(s):
    sign = -1 if s[0] == "-" else 1
    return sign * timedelta(hours=int(s[1:3]), minutes=int(s[3:5]), seconds=int(s[5:7] or 0))


def parse_duration(s):
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s.strip().upper())
    w, d, h, mi, sec = (int(x or 0) for x in m.groups()) if m else (0, 0, 0, 0, 0)
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=sec)


def parse_dt(value, params):
    """-> (naive wall-clock datetime, tzid or None for floating or 'UTC', is_date)."""
    v = value.strip()
    if len(v) == 8:
        return datetime.strptime(v, "%Y%m%d"), None, True
    dt = datetime.strptime(v.rstrip("Z"), "%Y%m%dT%H%M%S")
    return dt, "UTC" if v.endswith("Z") else params.get("TZID"), False


def parse_rrule(value, zones):
    f = dict(kv.split("=", 1) for kv in value.split(";") if "=" in kv)
    byday = []
    for tok in filter(None, f.get("BYDAY", "").split(",")):
        m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", tok.strip().upper())
        if m:
            byday.append((int(m[1]) if m[1] else 0, DAYS[m[2]]))
    until = None
    if f.get("UNTIL"):
        wall, tz, is_date = parse_dt(f["UNTIL"], {})
        if is_date:  # a date means the whole day
            wall += timedelta(days=1) - timedelta(seconds=1)
        until = to_local(wall, tz, zones)
    ints = lambda key: [int(x) for x in f.get(key, "").split(",") if x]
    return {"FREQ": f.get("FREQ", "").upper(), "INTERVAL": int(f.get("INTERVAL") or 1),
            "COUNT": int(f["COUNT"]) if f.get("COUNT") else None, "UNTIL": until,
            "BYDAY": byday, "BYMONTHDAY": ints("BYMONTHDAY"), "BYMONTH": ints("BYMONTH")}


def parse(text):
    """-> (events, zones). An event is {PROP: [(params, value), ...]}. zones maps a TZID to its
    STANDARD/DAYLIGHT rules (start, offset_from, offset_to, rrule), read from the feed's VTIMEZONE
    blocks because Windows Python has no time zone database."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise RuntimeError("That link did not return a calendar. The link may be wrong or expired")
    events, zones, stack = [], {}, []
    for ln in unfold(text):
        parts = split_line(ln)
        if not parts:
            continue
        name, params, value = parts
        if name == "BEGIN":
            stack.append((value.upper(), {}))
        elif name == "END" and stack:
            comp, props = stack.pop()
            if comp == "VEVENT":
                events.append(props)
            elif comp in ("STANDARD", "DAYLIGHT") and stack and stack[-1][0] == "VTIMEZONE":
                rule = (first_prop(props, "DTSTART")[1], first_prop(props, "TZOFFSETFROM")[1],
                        first_prop(props, "TZOFFSETTO")[1], first_prop(props, "RRULE")[1])
                if None not in rule[:3]:
                    stack[-1][1].setdefault("_RULES", []).append(
                        (parse_dt(rule[0], {})[0], parse_offset(rule[1]), parse_offset(rule[2]),
                         parse_rrule(rule[3], {}) if rule[3] else None))
            elif comp == "VTIMEZONE":
                zones[first_prop(props, "TZID")[1]] = props.get("_RULES", [])
        elif stack:
            stack[-1][1].setdefault(name, []).append((params, value))
    return events, zones


# --- time zones ---------------------------------------------------------------------------------

def nth_weekday(year, month, n, weekday):
    """The n-th `weekday` of a month; n = -1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-n - 1))


def zone_offset(rules, wall):
    """UTC offset of a zone at a wall-clock time: the latest switch that has already happened."""
    best = None
    for start, _, off_to, rule in rules:
        if rule is None:
            switches = [start]
        else:
            switches = []
            for y in (wall.year - 1, wall.year):
                if y < start.year:
                    continue
                month = (rule["BYMONTH"] or [start.month])[0]
                if rule["BYDAY"]:
                    n, wd = rule["BYDAY"][0]
                    d = nth_weekday(y, month, n or 1, wd)
                else:
                    d = date(y, month, (rule["BYMONTHDAY"] or [start.day])[0])
                switches.append(datetime.combine(d, start.time()))
        for t in switches:
            if t <= wall and (best is None or t > best[0]):
                best = (t, off_to)
    return best[1] if best else min(rules, key=lambda r: r[0])[1]


def to_local(wall, tzid, zones):
    """Wall-clock time in `tzid` -> naive local time, which is what the widget works in."""
    if tzid is None:  # floating: already local
        return wall
    if tzid == "UTC":
        off = timedelta(0)
    elif zones.get(tzid):
        off = zone_offset(zones[tzid], wall)
    else:
        try:
            from zoneinfo import ZoneInfo
            off = wall.replace(tzinfo=ZoneInfo(tzid)).utcoffset()
        except Exception:
            raise RuntimeError(f"The feed uses time zone '{tzid}' without defining it")
    aware = (wall - off).replace(tzinfo=timezone.utc).astimezone()
    return aware.replace(tzinfo=None)


# --- recurrence ---------------------------------------------------------------------------------

def days_in_month(d):
    return (date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)).day


def day_matches(d, s, rule):
    """Monthly and yearly rules: does day `d` fit BYDAY (with an optional n-th), BYMONTHDAY or the start day?"""
    if rule["BYDAY"]:
        for n, wd in rule["BYDAY"]:
            if d.weekday() != wd:
                continue
            if not n or (n > 0 and (d.day - 1) // 7 + 1 == n) or (n < 0 and (days_in_month(d) - d.day) // 7 + 1 == -n):
                return True
        return False
    if rule["BYMONTHDAY"]:
        dim = days_in_month(d)
        return any(d.day == (m if m > 0 else dim + m + 1) for m in rule["BYMONTHDAY"])
    return d.day == s.day


def matches(d, start, rule):
    """Is the wall-clock day `d` an occurrence day of a series that begins at `start`?"""
    s, step, freq = start.date(), rule["INTERVAL"], rule["FREQ"]
    if d < s:
        return False
    if freq == "DAILY":
        return (d - s).days % step == 0
    if freq == "WEEKLY":
        days = {wd for _, wd in rule["BYDAY"]} or {s.weekday()}
        weeks = ((d - timedelta(days=d.weekday())) - (s - timedelta(days=s.weekday()))).days // 7
        return weeks % step == 0 and d.weekday() in days
    if freq == "MONTHLY":
        if ((d.year - s.year) * 12 + d.month - s.month) % step:
            return False
    elif freq == "YEARLY":
        if (d.year - s.year) % step or d.month not in (rule["BYMONTH"] or [s.month]):
            return False
    else:
        return False
    return day_matches(d, s, rule)


def occurrences(start_wall, tzid, duration, rule, exdates, zones, lo, hi):
    """Local (start, end) of each occurrence of a series that overlaps [lo, hi). Walks the series
    day by day in wall-clock time, so a 09:00 meeting stays at 09:00 across daylight saving."""
    d = start_wall.date()
    if not rule["COUNT"] and rule["FREQ"] in ("DAILY", "WEEKLY"):  # jump close to the range
        period = rule["INTERVAL"] * (1 if rule["FREQ"] == "DAILY" else 7)
        gap = (lo.date() - d).days - duration.days - 2
        if gap > 0:
            d += timedelta(days=gap // period * period)
    count = 0
    while d <= hi.date() + timedelta(days=1):
        if matches(d, start_wall, rule):
            count += 1
            if rule["COUNT"] and count > rule["COUNT"]:
                break
            start = to_local(datetime.combine(d, start_wall.time()), tzid, zones)
            if rule["UNTIL"] and start > rule["UNTIL"]:
                break
            if start not in exdates and start + duration > lo and start < hi:
                yield start, start + duration
        d += timedelta(days=1)


# --- events -------------------------------------------------------------------------------------

def build(p, zones):
    """One VEVENT -> (event dict, wall start, tzid), or None without a start."""
    params, v = first_prop(p, "DTSTART")
    if v is None:
        return None
    wall, tzid, all_day = parse_dt(v, params)
    start = to_local(wall, tzid, zones)
    eparams, ev = first_prop(p, "DTEND")
    if ev:
        end = to_local(*parse_dt(ev, eparams)[:2], zones)
    else:
        dur = first_prop(p, "DURATION")[1]
        end = start + (parse_duration(dur) if dur else timedelta(days=1 if all_day else 0))
    busy_v = first_prop(p, "X-MICROSOFT-CDO-BUSYSTATUS")[1]
    if busy_v:
        busy = BUSY.get(busy_v.strip().upper(), 2)
    else:
        busy = 0 if (first_prop(p, "TRANSP")[1] or "").upper() == "TRANSPARENT" else 2
    status = (first_prop(p, "STATUS")[1] or "").strip().upper()
    subject = unescape(first_prop(p, "SUMMARY")[1] or "").strip() or "(no subject)"
    event = {"subject": subject, "start": start, "end": end, "allDay": all_day, "busy": busy,
             "response": 2 if busy == 1 or status == "TENTATIVE" else 3,  # no response status in a feed
             "status": 5 if status == "CANCELLED" else 0, "holiday": False}
    return event, wall, tzid


def read(source, first, last, timeout=30):
    """All events overlapping `first`..`last` (dates, inclusive) as dicts with datetime start/end.
    `source` is an http(s)/webcal URL or a path. Raises RuntimeError with a readable message."""
    try:
        raw, zones = parse(fetch(source, timeout))
        lo, hi = datetime.combine(first, time.min), datetime.combine(last + timedelta(days=1), time.min)
        # An edited single occurrence of a series is its own VEVENT; the series must skip that slot.
        replaced = set()
        for p in raw:
            rparams, rv = first_prop(p, "RECURRENCE-ID")
            if rv:
                wall, tz, _ = parse_dt(rv, rparams)
                replaced.add((first_prop(p, "UID")[1], to_local(wall, tz, zones)))
        out = []
        for p in raw:
            built = build(p, zones)
            if not built:
                continue
            event, wall, tzid = built
            rrule = first_prop(p, "RRULE")[1]
            if rrule and "RECURRENCE-ID" not in p:
                uid, exdates = first_prop(p, "UID")[1], set()
                for xparams, xv in p.get("EXDATE", []):
                    for x in xv.split(","):
                        xwall, xtz, _ = parse_dt(x, xparams)
                        exdates.add(to_local(xwall, xtz, zones))
                for s, e in occurrences(wall, tzid, event["end"] - event["start"], parse_rrule(rrule, zones),
                                        exdates, zones, lo, hi):
                    if (uid, s) not in replaced:
                        out.append({**event, "start": s, "end": e})
            elif event["end"] > lo and event["start"] < hi:
                out.append(event)
        return sorted(out, key=lambda e: e["start"])
    except ValueError:
        raise RuntimeError("The calendar feed has an unexpected format")


# --- classification -----------------------------------------------------------------------------

def is_absence(event, pattern=ABSENCE_PATTERN):
    return event.get("holiday", False) or event["busy"] == 3 or bool(re.search(pattern, event["subject"], re.I))


def _skipped(event):
    return event["status"] in (5, 7) or event["response"] == 4  # cancelled or declined


def meetings(events, pattern=ABSENCE_PATTERN):
    """Timed meetings you organised or accepted. A feed doesn't record attendance or responses, so
    that means everything except tentative events. All-day events, cancelled meetings and absences are left out."""
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
        while datetime.combine(day, time.min) < e["end"]:
            lo = max(e["start"], datetime.combine(day, time.min))
            hi = min(e["end"], datetime.combine(day + timedelta(days=1), time.min))
            seconds = round((hi - lo).total_seconds())
            if seconds > 0:
                out.append({"date": day.isoformat(), "seconds": seconds, "title": e["subject"],
                            "holiday": bool(e.get("holiday")),
                            "key": f"{e['start']:%Y-%m-%dT%H:%M}|{e['subject']}|{day}"})
            day += timedelta(days=1)
    return out
