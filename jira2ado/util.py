import json
import os
import re
import time
from datetime import datetime, timezone, timedelta


def parse_dt(s):
    """Parse a Jira timestamp such as 2024-03-01T10:15:30.123-0500 into UTC."""
    if not s:
        return None
    s = s.strip().replace("Z", "+0000")
    m = re.match(r"(.*[+-]\d\d):(\d\d)$", s)
    if m:
        s = m.group(1) + m.group(2)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def bump(dt, last):
    """ADO needs every revision date to be later than the one before it."""
    if last and dt <= last:
        dt = last + timedelta(milliseconds=1)
    return dt


def node_name(name):
    """Make a sprint name legal as an ADO iteration node name."""
    name = re.sub(r'[\\/$?*:"&<>#%|+]', "-", name or "").strip(" .")
    return name[:255] or "Unnamed"


def name_key(name):
    """Compare sprint and iteration names the way ADO stores them: 'Fin&Corp Sprint 28' = 'fin-corp sprint 28'."""
    return re.sub(r"\s+", " ", node_name(name)).lower()


def iteration_node(path):
    """An iteration path below the project root, each level made legal for ADO. '/' and '\\' both separate levels."""
    return "\\".join(node_name(p) for p in (path or "").replace("/", "\\").split("\\") if p.strip())


def slug(text):
    """Folder-friendly name: 'Fin&Corp Sprint 28' -> 'fin-corp-sprint-28'."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80] or "unnamed"


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Journal:
    """Append-only import log. Each applied step is flushed to disk at once,
    so a crashed or stopped run resumes without creating duplicates."""

    def __init__(self, path, readonly=False):
        self.path = path
        self.items = {}       # key -> {"id": int, "revs": int, "last": iso, "links": bool}
        self.held = {}        # key -> why it was left out (a possible duplicate)
        self.atts = {}        # jira attachment id -> ado url
        self.iterations = set()
        self.areas = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._apply(json.loads(line))
        self._fh = None
        if not readonly:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def _apply(self, e):
        if e["t"] == "rev":
            self.items[e["k"]] = {**self.items.get(e["k"], {}), "id": e["id"], "revs": e["n"], "last": e["d"]}
            if e.get("found"):  # was already in the target before this log; not created by it
                self.items[e["k"]]["found"] = True
            self.held.pop(e["k"], None)
        elif e["t"] == "held":
            self.held[e["k"]] = e.get("why", "")
        elif e["t"] == "att":
            self.atts[e["a"]] = e["u"]
        elif e["t"] == "links":
            self.items.setdefault(e["k"], {})["links"] = True
        elif e["t"] == "iter":
            self.iterations.add(e["n"])
        elif e["t"] == "area":
            self.areas.add(e["n"])

    def log(self, **e):
        self._apply(e)
        self._fh.write(json.dumps(e) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self):
        if self._fh:
            self._fh.close()


def backoff_sleep(attempt, retry_after=None):
    time.sleep(float(retry_after) if retry_after else min(60, 2 ** attempt))
