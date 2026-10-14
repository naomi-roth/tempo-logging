"""The entries file shared by the widget (worklog.py) and the uploader (tempo_sync.py)."""
import json, re, threading, uuid
from pathlib import Path

STORE = Path.home() / ".worklog" / "entries.json"
LOCK = threading.RLock()  # the widget thread and the upload threads both write the file


def load_config(path):
    """tasks.json with // comments allowed (whole-line or trailing; '//' inside a string, e.g. a URL, is kept)."""
    text = re.sub(r'"(?:[^"\\]|\\.)*"|//[^\n]*', lambda m: m[0] if m[0][0] == '"' else "",
                  Path(path).read_text(encoding="utf-8"))
    return json.loads(text)


def load():
    with LOCK:
        entries = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
        if any("id" not in e for e in entries):  # legacy entries: give stable ids
            for e in entries:
                e.setdefault("id", uuid.uuid4().hex)
            save(entries)
        return entries


def save(entries):
    with LOCK:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def _absence_path():
    return STORE.with_name("absences.json")  # next to entries.json; never read by the uploader


def load_absences():
    """Days off imported from Outlook: [{'date', 'seconds', 'title', 'key'}]. Display-only, never uploaded."""
    with LOCK:
        p = _absence_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_absences(absences):
    with LOCK:
        p = _absence_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(absences, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_synced(entry_id, task_name, worklog_id):
    """Flag one entry as uploaded. Re-reads the file first so concurrent edits aren't overwritten."""
    with LOCK:
        entries = load()
        for e in entries:
            if e["id"] == entry_id:
                e.update(synced=True, task=task_name, jiraWorklogId=worklog_id)
        save(entries)
