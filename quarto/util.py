import json
import os
import re
import threading
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
    """ADO stores System.ChangedDate in SQL Server DATETIME (1/300s = 3.33ms ticks: .xx0, .xx3, .xx7),
    rounding .xx5Z up to .xx7Z. Stepping by 10ms (>= 3 SQL ticks) guarantees strictly increasing dates."""
    min_next = (last + timedelta(milliseconds=10)) if last else None
    if min_next and (dt < min_next or iso(dt) <= iso(last)):
        dt = min_next
    return dt


def node_name(name):
    """Make a sprint name legal as an ADO iteration node name."""
    name = re.sub(r'[\\/$?*:"&<>#%|+]', "-", name or "").strip(" .")
    return name[:255] or "Unnamed"


def name_key(name):
    """Compare sprint and iteration names the way ADO stores them: 'Platform&Core Sprint 28' = 'platform-core sprint 28'."""
    return re.sub(r"\s+", " ", node_name(name)).lower()


def iteration_node(path):
    """An iteration path below the project root, each level made legal for ADO. '/' and '\\' both separate levels."""
    return "\\".join(node_name(p) for p in (path or "").replace("/", "\\").split("\\") if p.strip())


def slug(text):
    """Folder-friendly name: 'Platform&Core Sprint 28' -> 'platform-core-sprint-28'."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80] or "unnamed"


def sprint_folder_slug(chosen):
    """Deterministic, filesystem-safe folder name for 1..N selected sprints (always <= 80 chars), independent of tick order."""
    if not chosen:
        return "unnamed"
    ordered = sorted(
        chosen,
        key=lambda s: (int(s["id"]) if str(s.get("id", "")).isdigit() else 0, str(s.get("id", "")), slug(s.get("name", ""))),
    )
    joined = "+".join(slug(s["name"]) for s in ordered)
    if len(ordered) <= 3 and len(joined) <= 80:
        return joined or "unnamed"
    import hashlib
    sig = hashlib.sha1(",".join(str(s.get("id", s.get("name", ""))) for s in ordered).encode()).hexdigest()[:8]
    first = slug(ordered[0]["name"])[:28].rstrip("-")
    last = slug(ordered[-1]["name"])[:28].rstrip("-")
    return f"{first}..{last}-{len(ordered)}-sprints-{sig}"


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
        self._lock = threading.RLock()
        self.items = {}       # key -> {"id": int, "revs": int, "last": iso, "links": bool}
        self.held = {}        # key -> why it was left out (a possible duplicate)
        self.atts = {}        # jira attachment id -> ado url
        self.synced = {}      # key -> {"id": int, "before_fields": dict, "before_rel_urls": list, "added_rel_urls": list|None}
        self.iterations = set()
        self.areas = set()
        self.teams = set()
        self.test_plans = {}   # plan_key -> {"id": int, "root_suite_id": int, "name": str}
        self.test_suites = {}  # suite_key -> {"plan_id": int, "id": int, "name": str}
        self.suite_cases = {}  # str(suite_id) -> set of ADO work item IDs
        self.coalesced_noops = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            self._apply(json.loads(line))
                        except (ValueError, KeyError, TypeError):
                            continue
        self._fh = None
        if not readonly:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def _apply(self, e):
        if e["t"] == "rev":
            self.items[e["k"]] = {**self.items.get(e["k"], {}), "id": e["id"], "revs": e["n"], "last": e.get("d")}
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
        elif e["t"] == "team":
            self.teams.add(e["n"])
        elif e["t"] == "tplan":
            self.test_plans[e["k"]] = {"id": e["id"], "root_suite_id": e.get("root_suite_id"), "name": e.get("n", "")}
        elif e["t"] == "tsuite":
            self.test_suites[e["k"]] = {"plan_id": e["plan_id"], "id": e["id"], "name": e.get("n", "")}
        elif e["t"] == "tsuite_cases":
            skey = str(e["suite_id"])
            self.suite_cases.setdefault(skey, set()).update(int(x) for x in (e.get("ids") or []))
        elif e["t"] == "mode":
            self.coalesced_noops = bool(e.get("coalesced_noops"))
        elif e["t"] == "sync":
            if e["k"] not in self.synced:
                self.synced[e["k"]] = {
                    "id": e["id"],
                    "before_fields": e.get("before_fields") or {},
                    "after_fields": e.get("after_fields") or {},
                    "before_rel_urls": list(e.get("before_rel_urls") or []),
                    "added_rel_urls": list(e.get("added_rel_urls")) if "added_rel_urls" in e else None,
                }
            elif e.get("added_rel_urls"):
                cur_added = self.synced[e["k"]].get("added_rel_urls")
                if cur_added is not None:
                    for u in e["added_rel_urls"]:
                        if u not in cur_added:
                            cur_added.append(u)

    def log(self, **e):
        with self._lock:
            self._apply(e)
            self._fh.write(json.dumps(e) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def backoff_sleep(attempt, retry_after=None):
    delay = min(60, 2 ** attempt)
    if retry_after:
        try:
            delay = max(0.0, min(60.0, float(retry_after)))
        except (ValueError, TypeError):
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(str(retry_after))
                delay = max(0.0, min(60.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
    time.sleep(delay)
