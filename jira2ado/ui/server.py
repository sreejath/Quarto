"""Local browser view for jira2ado.

It listens on 127.0.0.1 only, so nothing outside this computer can reach it. Every request to
/api needs the random key that is printed when it starts, and the Host header must be this
computer, so other web pages open in the browser cannot use it either. Tokens are held in
memory for as long as it runs and are passed to each step in its environment, the same way
run.sh does it. Nothing secret is written to disk.
"""
import csv
import hmac
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import yaml

from .. import config
from ..ado_client import AdoClient, AdoError
from ..jira_client import JiraClient

if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    KIT = config.frozen_workspace_dir()
    STATIC = os.path.join(BUNDLE_DIR, "jira2ado", "ui", "static")
    if not os.path.isdir(STATIC):
        STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    CMD = [sys.executable, "--cli"]
else:
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    KIT = BUNDLE_DIR
    STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    CMD = [sys.executable, "-m", "jira2ado"]  # the tests swap this for a stand-in Azure DevOps

BASE = os.path.join("configs", "enterprise-development.yaml")
TARGETS = os.path.join("configs", "targets.yaml")
SETTINGS = "ui-settings.json"          # last email, URLs, and choices; never tokens

FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/app.css": ("app.css", "text/css; charset=utf-8"),
         "/logo.svg": ("logo.svg", "image/svg+xml")}


class State:
    def __init__(self):
        self.key = secrets.token_urlsafe(24)
        self.port = None
        self.tokens = {}     # jira_url, jira_email, jira_token, ado_url, ado_token
        self.names = {}      # jira, ado: signed-in names
        self.jobs = {}
        self.current = None  # the job that is running, if any
        self.lock = threading.Lock()
        self.server = None


S = State()


def _norm_url(u):
    u = (u or "").strip().rstrip("/")
    if u.lower().startswith("http://"):
        raise ApiError("Use an https:// URL so your API tokens are encrypted in transit.")
    if u and not u.lower().startswith("https://"):
        u = "https://" + u
    return u


def _is_double_paste(tok):
    return len(tok) >= 8 and len(tok) % 2 == 0 and tok[:len(tok) // 2] == tok[len(tok) // 2:]


def _cfg_path(rel):
    roots = (KIT,) if rel.startswith(os.path.join("configs", "local")) and getattr(sys, "frozen", False) else (
        (KIT, BUNDLE_DIR) if getattr(sys, "frozen", False) else (os.getcwd(), KIT, BUNDLE_DIR)
    )
    for root in roots:
        cand = os.path.join(root, rel) if root else rel
        if os.path.exists(cand):
            return cand
    return os.path.join(KIT, rel)


def base_cfg():
    local_base = _cfg_path(os.path.join("configs", "local", "base.yaml"))
    if os.path.exists(local_base):
        return config.load(local_base)
    return config.load(_cfg_path(BASE))


def active_jira_url():
    return _norm_url(S.tokens.get("jira_url") or load_settings().get("jira_url") or base_cfg()["jira"]["base_url"])


def active_ado_url():
    return _norm_url(S.tokens.get("ado_url") or load_settings().get("ado_url") or base_cfg()["ado"]["org_url"])


def jira():
    t = S.tokens
    if not t.get("jira_token"):
        raise ApiError("Connect to Jira first.", 401)
    return JiraClient(active_jira_url(), t["jira_email"], t["jira_token"])


def ado(project=""):
    if not S.tokens.get("ado_token"):
        raise ApiError("Connect to Azure DevOps first.", 401)
    return AdoClient(active_ado_url(), project, S.tokens["ado_token"])


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    data = {**load_settings(), **{k: v for k, v in values.items() if v is not None}}
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def inside_runs(path):
    """The real path of a file or folder under runs/, or an error. Keeps every file action inside the runs."""
    full = os.path.realpath(os.path.join(KIT, path))
    runs = os.path.realpath(os.path.join(KIT, "runs"))
    if not (full == runs or full.startswith(runs + os.sep)):
        raise ApiError("Only files inside the runs folder can be used here.", 403)
    return full


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind, steps):
        self.id = secrets.token_hex(4)
        self.kind = kind
        self.steps = [{"label": label, "status": "waiting"} for label in steps]
        self.log = []
        self.status = "running"
        self.error = None
        self.result = {}
        self.proc = None
        self.stopped = False

    def public(self, since=0):
        return {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
                "steps": self.steps, "log": self.log[since:], "next": len(self.log), "result": self.result}


def env_with_tokens():
    env = dict(os.environ)
    env.update(
        JIRA_BASE_URL=active_jira_url(),
        JIRA_EMAIL=S.tokens.get("jira_email", ""),
        JIRA_API_TOKEN=S.tokens.get("jira_token", ""),
        ADO_ORG_URL=active_ado_url(),
        ADO_PAT=S.tokens.get("ado_token", ""),
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
    )
    return env


def run_step(job, index, args, ok=(0,)):
    """Run one jira2ado command and stream its output into the job log. Returns (exit code, stdout of prepare)."""
    step = job.steps[index]
    if job.stopped:
        step["status"] = "stopped"
        raise StopRun()
    step["status"] = "running"
    job.log.append(f"$ jira2ado {' '.join(args)}")
    capture = args and args[0] == "prepare"  # prepare prints only the config path on stdout
    popen_kw = {
        "cwd": KIT,
        "env": env_with_tokens(),
        "text": True,
        "bufsize": 1,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE if capture else subprocess.STDOUT,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(CMD + list(args), **popen_kw)
    job.proc = proc
    if capture:
        out, err = proc.communicate()
        job.log += [line for line in err.splitlines() if line.strip()]
    else:
        out = ""
        for line in proc.stdout:
            job.log.append(line.rstrip("\n"))
        proc.wait()
    job.proc = None
    if job.stopped:
        step["status"] = "stopped"
        raise StopRun()
    step["status"] = "done" if proc.returncode in ok else "failed"
    return proc.returncode, out


class StopRun(Exception):
    pass


def last_message(job):
    """The most useful line for an error banner: the 'Stopped:' message if there is one."""
    for line in reversed(job.log):
        if line.startswith("Stopped") or "did not accept" in line or "was not found" in line:
            return line.replace("Stopped: ", "")
    return next((line for line in reversed(job.log) if line.strip()), "The step failed.")


def start(job, target):
    with S.lock:
        if S.current and S.jobs[S.current].status == "running":
            raise ApiError("Another run is still going. Wait for it to finish or stop it.", 409)
        S.jobs[job.id] = job
        S.current = job.id

    def body():
        try:
            target(job)
            if job.status == "running":
                job.status = "done"
        except StopRun:
            job.status = "stopped"
            job.log.append("Stopped at your request. Anything already written stays, and the next run continues from there.")
        except Exception as e:  # shown in the page instead of a silent failure
            job.status = "failed"
            job.error = job.error or str(e)

    threading.Thread(target=body, daemon=True).start()
    return job


def check_job(params):
    """Prepare the run, check the tokens and the query, export, and preview. Writes nothing to Azure DevOps."""
    job = Job("check", ["Look up the board, sprint and project", "Check access", "Export from Jira",
                        "Resolve user identities", "Compare with Azure DevOps & map statuses", "Plan the work items"])

    def work(job):
        args = ["prepare", "--from", params["source"], "--to", params["target"]]
        if params.get("board"):
            args += ["--board", str(params["board"])]
        for sp in params["sprints"]:
            args += ["--sprint", str(sp)]
        if params.get("iteration"):
            args += ["--iteration", params["iteration"]]
        if params.get("area"):
            args += ["--area", params["area"]]
        if params.get("include_done"):
            args += ["--include-done"]
        if params.get("include_hierarchy"):
            args += ["--include-hierarchy"]
        if params.get("history") is not None:
            args += ["--history", "true" if params["history"] else "false"]
        code, out = run_step(job, 0, args)
        if code != 0:
            job.status, job.error = "failed", last_message(job)
            return
        cfg = out.strip().splitlines()[-1]
        job.result["config"] = cfg
        for i, step in enumerate([["access"], ["export", "--force"], ["users"], ["check", "--online"], ["plan"]], 1):
            code, _ = run_step(job, i, ["-c", cfg] + step)
            if code != 0:
                job.status, job.error = "failed", last_message(job)
                return
        job.result["run"] = run_data(cfg)

    return start(job, work)


def apply_overrides_to_config(cfg_path, body):
    """Persist per-ticket status overrides, global status overrides, and duplicate action overrides into config.yaml
    and preview.json after validating values."""
    full_cfg = inside_runs(cfg_path)
    if isinstance(body.get("ticket_actions"), dict):
        for k, v in body["ticket_actions"].items():
            if v and v not in ("create", "skip", "duplicate", "sync"):
                raise ApiError(f"Invalid duplicate action '{v}' for {k}.")
    for field_name in ("ticket_states", "ticket_types", "status_overrides"):
        if field_name in body and body[field_name] is not None:
            if not isinstance(body[field_name], dict):
                raise ApiError(f"{field_name} must be an object.")
            for k, v in body[field_name].items():
                if v is not None and not isinstance(v, str):
                    raise ApiError(f"Invalid value in {field_name} for {k}.")
    with open(full_cfg, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    m = raw.setdefault("mapping", {})
    changed = False
    if isinstance(body.get("ticket_states"), dict):
        m.setdefault("ticket_states", {}).update({k: v.strip() for k, v in body["ticket_states"].items() if v and v.strip()})
        changed = True
    if isinstance(body.get("ticket_types"), dict):
        m.setdefault("ticket_types", {}).update({k: v.strip() for k, v in body["ticket_types"].items() if v and v.strip()})
        changed = True
    if isinstance(body.get("ticket_actions"), dict):
        m.setdefault("ticket_actions", {}).update({k: v for k, v in body["ticket_actions"].items() if v})
        changed = True
    if isinstance(body.get("status_overrides"), dict):
        m.setdefault("status_overrides", {}).update({k: v.strip() for k, v in body["status_overrides"].items() if v and v.strip()})
        changed = True
    if changed:
        with open(full_cfg, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True, width=120)
        pv_path = os.path.join(os.path.dirname(full_cfg), "preview.json")
        pv = read_json_file(pv_path)
        if pv and isinstance(pv.get("tickets"), list):
            ts = m.get("ticket_states") or {}
            tt = m.get("ticket_types") or {}
            ta = m.get("ticket_actions") or {}
            so = m.get("status_overrides") or {}
            for t in pv["tickets"]:
                k = t["key"]
                if t.get("jira_status") in so and k not in ts:
                    t["ado_state"] = so[t["jira_status"]]
                    t["state_match_reason"] = "override"
                if k in ts:
                    t["ado_state"] = ts[k]
                    t["state_match_reason"] = "override"
                if k in tt:
                    t["ado_type"] = tt[k]
                if k in ta:
                    t["action"] = ta[k]
            with open(pv_path, "w", encoding="utf-8") as fh:
                json.dump(pv, fh, indent=1)
    return raw


def import_job(params):
    cfg_path = params.get("config") or ""
    full_cfg = inside_runs(cfg_path)
    cfg = config.load(full_cfg)
    project = cfg["ado"]["project"]
    want = project if cfg["ado"].get("confirm_with_project_name") else "yes"
    if (params.get("confirm") or "").strip() != want:
        raise ApiError(f"Type {project} exactly to confirm." if want == project else "Confirm the import first.", 400)
    choice = params.get("possible_duplicates") or "skip"
    if choice not in ("skip", "create"):
        raise ApiError("possible_duplicates must be skip or create.")
    apply_overrides_to_config(cfg_path, params)
    job = Job("import", ["Apply status & duplicate overrides", "Create & sync work items in Azure DevOps", "Verify migrated items"])

    def work(job):
        code, _ = run_step(job, 0, ["-c", cfg_path, "plan"], ok=(0,))
        if code != 0:
            job.status, job.error = "failed", last_message(job)
            return
        code, _ = run_step(job, 1, ["-c", cfg_path, "import", "--possible-duplicates", choice], ok=(0,))
        report = os.path.join(os.path.dirname(cfg_path), "verify.csv")
        if os.path.exists(os.path.join(os.path.dirname(full_cfg), "import-state.jsonl")):
            run_step(job, 2, ["-c", cfg_path, "verify", "--report", report], ok=(0, 1))
        job.result["run"] = run_data(cfg_path)
        if code != 0:
            job.status, job.error = "failed", last_message(job)
            return

    return start(job, work)


def rollback_job(params):
    cfg_path = params.get("config") or ""
    full_cfg = inside_runs(cfg_path)
    cfg = config.load(full_cfg)
    project = cfg["ado"]["project"]
    want = project if cfg["ado"].get("confirm_with_project_name") else "yes"
    if (params.get("confirm") or "").strip() != want:
        raise ApiError(f"Type {project} exactly to confirm rollback." if want == project else "Confirm the rollback first.", 400)
    job = Job("rollback", ["Delete work items created by this run in Azure DevOps"])

    def work(job):
        code, _ = run_step(job, 0, ["-c", cfg_path, "rollback"], ok=(0,))
        job.result["run"] = run_data(cfg_path)
        if code != 0:
            job.status, job.error = "failed", last_message(job)
            return

    return start(job, work)


# ---------------------------------------------------------------- run data

def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run_data(cfg_path):
    """Everything the page shows for one run: the preview, the import log and the reports."""
    full_cfg = inside_runs(cfg_path)
    cfg = config.load(full_cfg)
    folder = os.path.dirname(cfg_path)
    full_folder = os.path.dirname(full_cfg)
    journal = {}
    held = {}
    path = os.path.join(full_folder, "import-state.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t") == "rev":
                    cur = journal.setdefault(e["k"], {})
                    cur.update(id=e["id"], revs=e["n"])
                    if e.get("found"):
                        cur["found"] = True
                    held.pop(e["k"], None)
                elif e.get("t") == "held":
                    held[e["k"]] = e.get("why", "")
    verify = {}
    vpath = os.path.join(full_folder, "verify.csv")
    if os.path.exists(vpath):
        with open(vpath, newline="", encoding="utf-8") as fh:
            verify = {r["jira_key"]: r.get("problems", "") for r in csv.DictReader(fh)}
    org = cfg["ado"]["org_url"].rstrip("/")
    project = cfg["ado"]["project"]
    return {"config": cfg_path, "folder": folder,
            "preview": read_json_file(os.path.join(full_folder, "preview.json")),
            "result": read_json_file(os.path.join(full_folder, "import-result.json")),
            "journal": journal, "held": held, "verify": verify,
            "item_url": f"{org}/{quote(project)}/_workitems/edit/",
            "jira_browse": cfg["jira"]["base_url"].rstrip("/") + "/browse/",
            "files": {name: os.path.join(folder, name) for name in ("duplicates.csv", "verify.csv")
                      if os.path.exists(os.path.join(full_folder, name))}}


# ---------------------------------------------------------------- API

def api(method, path, query, body):
    if path == "/api/state":
        s = load_settings()
        b = base_cfg()
        return {
            "jira": S.names.get("jira"),
            "ado": S.names.get("ado"),
            "jira_url": S.tokens.get("jira_url") or s.get("jira_url") or b["jira"].get("base_url") or "",
            "ado_url": S.tokens.get("ado_url") or s.get("ado_url") or b["ado"].get("org_url") or "",
            "settings": s,
            "running": S.current if S.current and S.jobs[S.current].status == "running" else None,
        }

    if path == "/api/connect" and method == "POST":
        email = (body.get("jira_email") or "").strip()
        jt, at = (body.get("jira_token") or "").strip(), (body.get("ado_token") or "").strip()
        if not (email and jt and at):
            raise ApiError("Fill in the Jira email and both tokens.")
        b = base_cfg()
        s = load_settings()
        j_url = _norm_url(body.get("jira_url") or s.get("jira_url") or b["jira"]["base_url"])
        a_url = _norm_url(body.get("ado_url") or s.get("ado_url") or b["ado"]["org_url"])
        if not j_url or not a_url:
            raise ApiError("Provide both the Atlassian Jira URL and Azure DevOps Organization URL.")
        out, problems = {}, {}
        if _is_double_paste(jt):
            problems["jira"] = "That looks like the Jira API token pasted twice. Paste it once."
        else:
            try:
                out["jira"] = JiraClient(j_url, email, jt).myself()
            except Exception as e:
                problems["jira"] = ("Jira did not accept the email and API token. Check both and paste the token once."
                                    if " 401 " in str(e) or " 403 " in str(e) else f"Could not reach Jira at {j_url}: {e}")
        if _is_double_paste(at):
            problems["ado"] = "That looks like the Azure DevOps token pasted twice. Paste it once."
        else:
            try:
                out["ado"] = AdoClient(a_url, "", at).whoami() or "your account"
            except AdoError as e:
                problems["ado"] = str(e)
            except Exception as e:
                problems["ado"] = f"Could not reach Azure DevOps at {a_url}: {e}"
        if "jira" in out:
            S.tokens.update(jira_url=j_url, jira_email=email, jira_token=jt)
            S.names["jira"] = out["jira"]
        if "ado" in out:
            S.tokens.update(ado_url=a_url, ado_token=at)
            S.names["ado"] = out["ado"]
        save_settings(jira_email=email, jira_url=j_url, ado_url=a_url)
        return {"jira": S.names.get("jira"), "ado": S.names.get("ado"),
                "jira_url": j_url, "ado_url": a_url, "problems": problems}

    if path == "/api/disconnect" and method == "POST":
        S.tokens.clear()
        S.names.clear()
        return {"ok": True}

    if path == "/api/jira/projects":
        return {"projects": jira().projects()}

    if path == "/api/jira/boards":
        key = (query.get("project") or [""])[0].strip().upper()
        if not key:
            raise ApiError("Choose a Jira project.")
        j = jira()
        boards = j.boards(key) if getattr(j, "boards", None) else []
        return {"boards": boards}

    if path == "/api/jira/sprints":
        key = (query.get("project") or [""])[0].strip().upper()
        board = (query.get("board") or [""])[0].strip()
        if not key:
            raise ApiError("Choose a Jira project.")
        try:
            j = jira()
            try:
                sprints = j.project_sprints(key, board_id=board or None)
            except TypeError:
                sprints = j.project_sprints(key)
        except RuntimeError as e:
            raise ApiError(f"Could not read the boards of {key}: {e}")
        # Active first, then the coming sprints in date order, then past sprints newest first.
        by_state = lambda st: [s for s in sprints if s.get("state") == st]
        sprints = (by_state("active")
                   + sorted(by_state("future"), key=lambda s: s.get("startDate") or "9999")
                   + sorted(by_state("closed"), key=lambda s: s.get("startDate") or "", reverse=True)
                   + [s for s in sprints if s.get("state") not in ("active", "future", "closed")])
        return {"sprints": [{"id": s["id"], "name": s["name"], "state": s.get("state"),
                             "goal": s.get("goal", ""), "boards": s.get("boards", []),
                             "start": (s.get("startDate") or "")[:10], "end": (s.get("endDate") or "")[:10]}
                            for s in sprints]}

    if path == "/api/targets":
        targets = {}
        t_path = _cfg_path(TARGETS)
        if os.path.exists(t_path):
            with open(t_path, encoding="utf-8") as fh:
                targets = yaml.safe_load(fh) or {}
        local_t_path = _cfg_path(os.path.join("configs", "local", "targets.yaml"))
        if os.path.exists(local_t_path):
            with open(local_t_path, encoding="utf-8") as fh:
                targets.update(yaml.safe_load(fh) or {})
        by_name = {
            name: {
                "name": name,
                "confirm_with_project_name": ((spec or {}).get("ado") or {}).get("confirm_with_project_name", True),
                "configured": True,
            }
            for name, spec in targets.items()
        }
        # Dynamically discover projects in the connected ADO organization; all unconfigured
        # projects require typing the project name before importing (S7).
        if S.tokens.get("ado_token"):
            try:
                client = ado()
                if getattr(client, "projects", None):
                    for p in client.projects():
                        pname = p["name"]
                        if pname not in by_name:
                            by_name[pname] = {
                                "name": pname,
                                "description": p.get("description", ""),
                                "confirm_with_project_name": True,
                                "configured": False,
                            }
            except Exception:
                pass
        return {"targets": list(by_name.values())}

    if path == "/api/ado/iterations":
        name = (query.get("project") or [""])[0].strip()
        if not name:
            raise ApiError("Choose an Azure DevOps project.")
        canonical = ado(name).project_name()
        c = ado(canonical)
        areas = c.area_paths() if getattr(c, "area_paths", None) else []
        return {"project": canonical, "iterations": c.iteration_paths(), "areas": areas}

    if path == "/api/check" and method == "POST":
        if not (S.tokens.get("jira_token") and S.tokens.get("ado_token")):
            raise ApiError("Connect to Jira and Azure DevOps first.", 401)
        source, target = (body.get("source") or "").strip(), (body.get("target") or "").strip()
        board = (body.get("board") or "").strip() or None
        sprints = [str(x) for x in body.get("sprints") or [] if str(x).strip()]
        if not (source and target and sprints):
            raise ApiError("Choose a Jira project, at least one sprint and an Azure DevOps project.")
        save_settings(source=source, board=board, target=target)
        return check_job({
            "source": source,
            "board": board,
            "target": target,
            "sprints": sprints,
            "iteration": (body.get("iteration") or "").strip() or None,
            "area": (body.get("area") or "").strip() or None,
            "include_done": bool(body.get("include_done")),
            "include_hierarchy": bool(body.get("include_hierarchy")),
            "history": body.get("history", True),
        }).public()

    if path == "/api/override" and method == "POST":
        cfg_path = body.get("config") or ""
        if not cfg_path:
            raise ApiError("Missing run config path.")
        apply_overrides_to_config(cfg_path, body)
        return {"ok": True, "run": run_data(cfg_path)}

    if path == "/api/users/update" and method == "POST":
        updates = body.get("users") or {}
        cfg_path = body.get("config") or ""
        users_path = "users.csv"
        if cfg_path:
            inside_runs(cfg_path)
            users_path = config.load(cfg_path)["paths"].get("users_file") or "users.csv"
        rows = []
        if os.path.exists(users_path):
            with open(users_path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        for r in rows:
            if r["account_id"] in updates:
                r["ado_identity"] = (updates[r["account_id"]] or "").strip()
        cols = ["account_id", "display_name", "jira_email", "active", "ado_identity"]
        with open(users_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return {"ok": True}

    if path == "/api/import" and method == "POST":
        if not (S.tokens.get("jira_token") and S.tokens.get("ado_token")):
            raise ApiError("Connect to Jira and Azure DevOps first.", 401)
        return import_job(body).public()

    if path == "/api/rollback" and method == "POST":
        if not S.tokens.get("ado_token"):
            raise ApiError("Connect to Azure DevOps first.", 401)
        return rollback_job(body).public()

    if path.startswith("/api/jobs/"):
        parts = path.split("/")
        job = S.jobs.get(parts[3]) if len(parts) > 3 else None
        if not job:
            raise ApiError("That run is not known. The browser view may have been restarted.", 404)
        if len(parts) > 4 and parts[4] == "stop" and method == "POST":
            job.stopped = True
            if job.proc:
                job.proc.terminate()
            return job.public()
        return job.public(int((query.get("since") or ["0"])[0]))

    if path == "/api/run":
        return run_data((query.get("config") or [""])[0])

    if path == "/api/open" and method == "POST":
        full = inside_runs(body.get("path") or "")
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", full] if body.get("reveal") else ["open", full], check=False)
            return {"ok": True}
        elif sys.platform == "win32":
            os.startfile(os.path.dirname(full) if body.get("reveal") else full)
            return {"ok": True}
        return {"ok": False, "path": full}

    if path == "/api/quit" and method == "POST":
        S.tokens.clear()
        S.names.clear()
        threading.Thread(target=S.server.shutdown, daemon=True).start()
        return {"ok": True}

    raise ApiError("Not found.", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "jira2ado"

    def log_message(self, fmt, *args):  # keep the terminal quiet; nothing here should reach a log
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra_headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
        for hk, hv in (extra_headers or {}).items():
            self.send_header(hk, hv)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        extra_host = os.environ.get("JIRA2ADO_UI_HOST") or "127.0.0.1"
        return self.headers.get("Host", "") in (
            f"127.0.0.1:{S.port}",
            f"localhost:{S.port}",
            f"{extra_host}:{S.port}",
        )

    def _handle(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "This view only answers to this computer."})
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if method != "GET" or url.path not in FILES:
                return self._send(404, {"error": "Not found."})
            name, ctype = FILES[url.path]
            with open(os.path.join(STATIC, name), "rb") as fh:
                return self._send(200, fh.read(), ctype)
        qs = parse_qs(url.query)
        req_key = self.headers.get("X-Jira2ado-Key", "")
        if not hmac.compare_digest(req_key, S.key):
            return self._send(403, {"error": "This page has an old key. Open the address shown in the Terminal window."})
        if url.path == "/api/download" and method == "GET":
            try:
                full = inside_runs((qs.get("path") or [""])[0])
                with open(full, "rb") as fh:
                    fname = os.path.basename(full)
                    return self._send(200, fh.read(), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{fname}"'})
            except ApiError as e:
                return self._send(e.status, {"error": str(e)})
            except OSError:
                return self._send(404, {"error": "File not found."})
        body = {}
        if method == "POST":
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self._send(415, {"error": "Send JSON."})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "The request was not valid JSON."})
        try:
            return self._send(200, api(method, url.path, qs, body))
        except ApiError as e:
            return self._send(e.status, {"error": str(e)})
        except AdoError as e:
            return self._send(502, {"error": str(e)})
        except RuntimeError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:  # always answer, so the page can show what went wrong
            return self._send(500, {"error": f"Something went wrong on this computer: {type(e).__name__}: {e}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def serve(port=0, open_browser=True, host=None):
    os.makedirs(KIT, exist_ok=True)
    os.chdir(KIT)
    bind_host = host or os.environ.get("JIRA2ADO_UI_HOST") or "127.0.0.1"
    S.server = ThreadingHTTPServer((bind_host, port), Handler)
    S.port = S.server.server_address[1]
    url = f"http://127.0.0.1:{S.port}/#key={S.key}"
    print(f"jira2ado is open in your browser at {url}")
    print("Keep this window open while you use it. Close it, or press Ctrl+C, to stop.")
    prev_term = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        def _on_term(_signum, _frame):
            if S.server:
                threading.Thread(target=S.server.shutdown, daemon=True).start()
        prev_term = signal.signal(signal.SIGTERM, _on_term)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        S.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if prev_term is not None and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, prev_term)
        S.tokens.clear()
        S.server.server_close()
        print("jira2ado has stopped.")
    return url


def main():
    port = int(os.environ.get("JIRA2ADO_UI_PORT") or 0)
    host = os.environ.get("JIRA2ADO_UI_HOST") or "127.0.0.1"
    serve(port, open_browser=os.environ.get("JIRA2ADO_UI_NO_BROWSER") != "1", host=host)


if __name__ == "__main__":
    main()
