#!/usr/bin/env python3
"""Push worklog entries to Jira (Tempo reads native Jira worklogs).
Dry-run by default; --apply to post.

  python tempo_sync.py                      # all unsynced entries, dry run
  python tempo_sync.py --date 2026-10-05    # one day (repeatable)
  python tempo_sync.py --from 2026-10-01 --to 2026-10-05 --apply
"""
import argparse, http.client, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import store

CONFIG = Path(__file__).with_name("tasks.json")
COOKIE_FILE = Path(__file__).with_name("cookie.txt")
WORKERS = 4  # parallel posts; override with "workers" in tasks.json


class SyncError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def fatal(self):
        """Auth or network trouble: retrying the other entries would only fail the same way."""
        return self.status is None or self.status in (401, 403) or 300 <= self.status < 400


def read_cookie_file():
    """Cookie header value built from cookie.txt."""
    if not COOKIE_FILE.exists():
        return None
    return normalize_cookie(COOKIE_FILE.read_text(encoding="utf-8"))


def normalize_cookie(text):
    """Accepts name=value or name:"value" pairs (quoted or not, ';' or line separated,
    optional leading 'Cookie:') and returns a valid Cookie header value, or None."""
    text = " ".join(text.split())
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    pairs = re.findall(r'([A-Za-z_][\w.~-]*)\s*[:=]\s*"?([^";\s]+)"?', text)
    return "; ".join(f"{k}={v}" for k, v in pairs) or None


def daterange(a, b):
    while a <= b:
        yield a
        a += timedelta(days=1)


def match(code, tasks):
    return next((t for t in tasks if t["code"].lower() == (code or "").lower()), None)


def ask(entry, tasks):
    print(f"\nUnknown code '{entry.get('code')}' for: \"{entry['desc']}\" ({entry['seconds'] // 60}m)")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}) {t['code']} - {t['name']}")
    while True:
        c = input("Pick number, or s to skip: ").strip().lower()
        if c == "s":
            return None
        if c.isdigit() and 1 <= int(c) <= len(tasks):
            return tasks[int(c) - 1]


def auth_headers(cfg):
    """Request headers with credentials. Raises SyncError if there are none."""
    env_cookie = os.environ.get(cfg.get("cookie_env", "JIRA_COOKIE"))
    cookie = (normalize_cookie(env_cookie) if env_cookie else None) or read_cookie_file()
    token = os.environ.get(cfg.get("token_env", "JIRA_TOKEN"))
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "X-Atlassian-Token": "no-check"}
    if cookie:
        headers["Cookie"] = cookie
    elif token:
        headers["Authorization"] = "Bearer " + token
    else:
        raise SyncError(f"No credentials. Put 'MRHSession=...; JSESSIONID=...' in {COOKIE_FILE.name}", 0)
    return headers


_local = threading.local()  # one keep-alive connection per worker thread


def call(cfg, headers, method, path, body=None):
    """One request over a reused HTTPS connection. Raises SyncError on any failure."""
    u = urlsplit(cfg["jira_url"])
    payload = json.dumps(body).encode() if body is not None else None
    for attempt in (1, 2):
        conn = getattr(_local, "conn", None)
        fresh = conn is None
        if fresh:
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            conn = _local.conn = cls(u.netloc, timeout=30)
        try:
            conn.request(method, u.path.rstrip("/") + path, payload, headers)
            r = conn.getresponse()
            data = r.read()
            break
        except (http.client.HTTPException, OSError) as e:
            conn.close()
            _local.conn = None
            if fresh or attempt == 2:  # a stale keep-alive connection gets one retry; a fresh one doesn't
                raise SyncError(f"{method} {path} failed: {e}") from e
    if 300 <= r.status < 400:
        raise SyncError(f"{method} {path} redirected ({r.status}): cookie expired or rejected", r.status)
    if r.status >= 400:
        raise SyncError(f"{method} {path} failed: {r.status} {data.decode(errors='replace')[:300]}", r.status)
    return json.loads(data) if data else None


def check_login(cfg):
    """-> (ok, message). Cheap read-only call that tells whether the cookie is accepted."""
    try:
        me = call(cfg, auth_headers(cfg), "GET", "/rest/api/2/myself")
        return True, (me or {}).get("displayName") or (me or {}).get("name") or "ok"
    except SyncError as e:
        if e.status == 0:
            return False, "no cookie"
        return False, "cookie expired" if e.fatal and e.status else str(e)


def plan_jobs(entries, tasks, day_start, days=None, resolve=None):
    """Unsynced entries as upload jobs. Start times are stacked from day_start per day in entry order;
    synced entries still occupy their slot. `resolve(entry)` may pick a task for an unknown code.
    -> (jobs, skipped_entries)"""
    days = days or sorted({e["date"] for e in entries if not e.get("synced")})
    jobs, skipped = [], []
    for day in days:
        cursor = day_start
        for e in (x for x in entries if x["date"] == day):
            start, cursor = cursor, cursor + timedelta(seconds=e["seconds"])
            if e.get("synced"):
                continue
            task = match(e.get("code"), tasks) or (resolve(e) if resolve else None)
            if not task:
                skipped.append(e)
                continue
            # native Jira worklog; Tempo reads these. Needs a UTC offset in "started".
            started = datetime.fromisoformat(f"{day}T{start.strftime('%H:%M:%S')}").astimezone()
            jobs.append({"id": e["id"], "date": day, "desc": e["desc"], "seconds": e["seconds"],
                         "task": task, "started": started.strftime("%Y-%m-%dT%H:%M:%S.000%z")})
    return jobs, skipped


def post_jobs(cfg, headers, jobs, on_result):
    """Posts jobs in parallel. `on_result(job, response, error)` is called from worker threads;
    exactly one of response / error is set. After a fatal error the remaining jobs are not attempted."""
    stop = threading.Event()

    def one(job):
        if stop.is_set():
            return
        try:
            res = call(cfg, headers, "POST", f"/rest/api/2/issue/{job['task']['issueKey']}/worklog", {
                "started": job["started"], "timeSpentSeconds": job["seconds"], "comment": job["desc"]})
        except SyncError as err:
            if err.fatal:
                stop.set()
            on_result(job, None, err)
            return
        on_result(job, res or {}, None)

    with ThreadPoolExecutor(max_workers=cfg.get("workers", WORKERS)) as pool:
        list(pool.map(one, jobs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if a.start:
        days = [d.isoformat() for d in daterange(date.fromisoformat(a.start),
                                                  date.fromisoformat(a.end or a.start))]
    else:
        days = a.date

    cfg = json.loads(CONFIG.read_text())
    tasks = cfg["tasks"]
    entries = store.load()
    if days is None:  # default: every day that still has unsynced entries
        days = sorted({e["date"] for e in entries if not e.get("synced")})
        if not days:
            print("Nothing to sync.")
            return
    day_start = datetime.strptime(cfg.get("day_start", "09:00"), "%H:%M")

    if not a.apply:
        jobs, unknown = plan_jobs(entries, tasks, day_start, days)
        for day in sorted({j["date"] for j in jobs}):
            print(f"\n== {day} ==")
            for j in (j for j in jobs if j["date"] == day):
                t = j["task"]
                print(f"  {j['seconds'] // 60:>4}m  [{t['code']}] {j['desc']}  ->  {t['name']} ({t['issueKey']})")
        for e in unknown:
            print(f"  {e['seconds'] // 60:>4}m  [{e.get('code', '?')}] {e['desc']}  ({e['date']})  ->  unknown code, will ask")
        total = sum(j["seconds"] for j in jobs)
        print(f"\n{len(jobs)} entries, {total // 3600}h{total % 3600 // 60:02d}m would be posted."
              " Dry run. Add --apply to post.")
        return

    try:
        headers = auth_headers(cfg)
    except SyncError as e:
        sys.exit(str(e))
    # Plan first (and ask about unknown codes) so nothing interactive happens mid-upload.
    jobs, _ = plan_jobs(entries, tasks, day_start, days, resolve=lambda e: ask(e, tasks))
    if not jobs:
        print("\nNothing to post.")
        return

    lock, done = threading.Lock(), 0

    def on_result(job, res, err):
        nonlocal done
        with lock:
            if err:
                print(f"  FAILED {job['date']} {job['desc']}: {err}")
                return
            store.mark_synced(job["id"], job["task"]["name"], res.get("id"))
            done += 1
            print(f"  posted {job['date']} {job['seconds'] // 60}m -> {job['task']['name']}: {job['desc']}")

    t0 = time.perf_counter()
    print(f"\nPosting {len(jobs)} entries...")
    post_jobs(cfg, headers, jobs, on_result)
    print(f"\nPosted {done}/{len(jobs)} in {time.perf_counter() - t0:.1f}s.")
    if done < len(jobs):
        print(f"{len(jobs) - done} not posted. Fix the error above and rerun; synced entries are skipped.")
        sys.exit(1)


if __name__ == "__main__":
    main()
