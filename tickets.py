"""Read-only list of my in-progress tickets from a second Jira (used to prefill worklog lines).
Config lives under "tickets" in tasks.json (git-ignored): "url", "token", optional "jql" and "code".
Never print the token.
"""
import json
import urllib.error, urllib.parse, urllib.request

JQL = "assignee = currentUser() AND statusCategory = 'In Progress' ORDER BY updated DESC"


class TicketError(RuntimeError):
    pass


def fetch(cfg):
    """-> [{"key", "summary"}], newest first. Raises TicketError with a short message."""
    token = cfg.get("token")
    if not token or token.startswith("<"):  # empty, or the example placeholder
        raise TicketError("no token (set 'token' under 'tickets' in tasks.json)")
    query = urllib.parse.urlencode({"jql": cfg.get("jql", JQL), "fields": "summary", "maxResults": 50})
    req = urllib.request.Request(f"{cfg['url'].rstrip('/')}/rest/api/2/search?{query}",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise TicketError("token rejected" if e.code in (401, 403) else f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TicketError(f"unreachable: {e}") from e
    return [{"key": i["key"], "summary": i["fields"]["summary"]} for i in data.get("issues", [])]
