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
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import yaml

from .. import config
from ..ado_client import AdoClient, AdoError
from ..jira_client import JiraClient
from ..zephyr_client import DEFAULT_SQUAD_URL, ZephyrError, ZephyrSquadClient

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
         "/sprints": ("sprints.html", "text/html; charset=utf-8"),
         "/board": ("board.html", "text/html; charset=utf-8"),
         "/project": ("project.html", "text/html; charset=utf-8"),
         "/jql": ("jql.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/app.css": ("app.css", "text/css; charset=utf-8"),
         "/logo.svg": ("logo.svg", "image/svg+xml")}


class State:
    def __init__(self):
        self.key = secrets.token_urlsafe(24)
        self.port = None
        self.tokens = {}     # jira_url, jira_email, jira_token, ado_url, ado_token, zephyr_*
        self.names = {}      # jira, ado, zephyr: signed-in names
        self.connected_from_env = False
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


def active_zephyr_url():
    return _norm_url(
        S.tokens.get("zephyr_base_url")
        or load_settings().get("zephyr_base_url")
        or (base_cfg().get("zephyr") or {}).get("base_url")
        or DEFAULT_SQUAD_URL
    )


_PLACEHOLDER_PREFIXES = ("your_", "https://your-org.", "you@company.com")


def _is_placeholder(val):
    v = (val or "").strip().lower()
    return (not v) or any(v.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def _find_local_env_file():
    for cand in (
        (os.environ.get("JIRA2ADO_SECRETS_FILE") or "").strip(),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(KIT, ".env"),
        os.path.join(os.getcwd(), ".secrets.env"),
        os.path.join(KIT, ".secrets.env"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _connect_credentials(j_url, email, jt, a_url, at, z_ak="", z_sk="", z_url="", from_env=False):
    out, problems = {}, {}
    account_id = ""
    if _is_double_paste(jt):
        problems["jira"] = "That looks like the Jira API token pasted twice. Paste it once."
    else:
        try:
            jc = JiraClient(j_url, email, jt)
            if getattr(jc, "myself_details", None):
                me = jc.myself_details() or {}
                out["jira"] = me.get("displayName") or me.get("emailAddress") or me.get("accountId") or email
                account_id = (me.get("accountId") or "").strip()
            else:
                out["jira"] = jc.myself()
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
    z_ak = (z_ak or "").strip()
    z_sk = (z_sk or "").strip()
    z_url = _norm_url(z_url or active_zephyr_url())
    if z_ak or z_sk:
        if not (z_ak and z_sk):
            problems["zephyr"] = "Provide both the Zephyr Squad Access Key and Secret Key."
        elif _is_double_paste(z_ak) or _is_double_paste(z_sk):
            problems["zephyr"] = "That looks like a Zephyr key pasted twice. Paste each key once."
        else:
            try:
                ZephyrSquadClient(z_ak, z_sk, account_id, base_url=z_url).check_access()
                out["zephyr"] = "Zephyr Squad Cloud"
            except ZephyrError as e:
                problems["zephyr"] = str(e)
            except Exception as e:
                problems["zephyr"] = f"Could not reach Zephyr Squad at {z_url}: {e}"
    if "jira" in out:
        S.tokens.update(jira_url=j_url, jira_email=email, jira_token=jt)
        if account_id:
            S.tokens["zephyr_account_id"] = account_id
        S.names["jira"] = out["jira"]
    if "ado" in out:
        S.tokens.update(ado_url=a_url, ado_token=at)
        S.names["ado"] = out["ado"]
    if "zephyr" in out:
        S.tokens.update(zephyr_access_key=z_ak, zephyr_secret_key=z_sk, zephyr_base_url=z_url)
        S.names["zephyr"] = out["zephyr"]
    elif not (z_ak or z_sk):
        for k in ("zephyr_access_key", "zephyr_secret_key", "zephyr_base_url"):
            S.tokens.pop(k, None)
        S.names.pop("zephyr", None)
    S.connected_from_env = bool(from_env and "jira" in out and "ado" in out)
    save_settings(jira_email=email, jira_url=j_url, ado_url=a_url,
                  zephyr_base_url=z_url if (z_ak and z_sk) else None)
    return {
        "jira": S.names.get("jira"),
        "ado": S.names.get("ado"),
        "zephyr": S.names.get("zephyr"),
        "jira_url": j_url,
        "ado_url": a_url,
        "zephyr_url": z_url,
        "connected_from_env": S.connected_from_env,
        "problems": problems,
    }


def auto_connect_from_env(path=None):
    """Read Jira, ADO, and optional Zephyr keys from the git-ignored local .env file and connect in memory."""
    env_path = path or _find_local_env_file()
    if not env_path:
        return None
    vals = config.read_dotenv(env_path)
    b = base_cfg()
    s = load_settings()
    j_url = _norm_url(vals.get("JIRA_BASE_URL") or s.get("jira_url") or b["jira"]["base_url"])
    email = (vals.get("JIRA_EMAIL") or s.get("jira_email") or "").strip()
    jt = (vals.get("JIRA_API_TOKEN") or "").strip()
    a_url = _norm_url(vals.get("ADO_ORG_URL") or s.get("ado_url") or b["ado"]["org_url"])
    at = (vals.get("ADO_PAT") or "").strip()
    z_ak = "" if _is_placeholder(vals.get("ZEPHYR_ACCESS_KEY")) else (vals.get("ZEPHYR_ACCESS_KEY") or "").strip()
    z_sk = "" if _is_placeholder(vals.get("ZEPHYR_SECRET_KEY")) else (vals.get("ZEPHYR_SECRET_KEY") or "").strip()
    z_url = (vals.get("ZEPHYR_BASE_URL") or "").strip()
    if any(_is_placeholder(x) for x in (j_url, email, jt, a_url, at)):
        return None
    return _connect_credentials(j_url, email, jt, a_url, at, z_ak=z_ak, z_sk=z_sk, z_url=z_url, from_env=True)


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
        self.paused = False
        self.config_path = None
        self.started_at = time.time()
        self.finished_at = None

    def public(self, since=0):
        end_ts = self.finished_at if self.finished_at is not None else time.time()
        return {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
                "steps": self.steps, "log": self.log[since:], "next": len(self.log), "result": self.result,
                "started_at": self.started_at, "elapsed_seconds": round(max(0.0, end_ts - self.started_at), 1)}


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
    if S.tokens.get("zephyr_access_key") and S.tokens.get("zephyr_secret_key"):
        env["ZEPHYR_ACCESS_KEY"] = S.tokens["zephyr_access_key"]
        env["ZEPHYR_SECRET_KEY"] = S.tokens["zephyr_secret_key"]
        env["ZEPHYR_BASE_URL"] = active_zephyr_url()
        if S.tokens.get("zephyr_account_id"):
            env["ZEPHYR_ACCOUNT_ID"] = S.tokens["zephyr_account_id"]
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
        proc.stdout.close()
        proc.wait()
    job.proc = None
    if job.stopped:
        step["status"] = "stopped"
        raise StopRun()
    if proc.returncode == 2:
        step["status"] = "paused"
        return 2, out
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
        finally:
            job.finished_at = time.time()

    threading.Thread(target=body, daemon=True).start()
    return job


def _clear_pause_file(cfg_path):
    try:
        full_cfg = inside_runs(cfg_path)
        pfile = os.path.join(os.path.dirname(full_cfg), ".pause")
        if os.path.exists(pfile):
            os.remove(pfile)
    except Exception:
        pass


def check_job(params, existing_cfg=None):
    """Prepare the run, check the tokens and the query, export, and preview. Writes nothing to Azure DevOps."""
    job = Job("check", ["Look up the project, board and scope", "Check access", "Export from Jira",
                        "Resolve user identities", "Compare with Azure DevOps & map statuses", "Plan the work items"])

    def work(job):
        if existing_cfg:
            cfg = existing_cfg
            job.steps[0]["status"] = "done"
        else:
            mode = (params.get("mode") or "sprints").strip().lower()
            args = ["prepare", "--mode", mode, "--from", params["source"], "--to", params["target"]]
            if params.get("board"):
                args += ["--board", str(params["board"])]
            for sp in (params.get("sprints") or []):
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
            if params.get("boards_to_areas") is True:
                args += ["--boards-to-areas"]
            elif params.get("boards_to_areas") is False:
                args += ["--no-boards-to-areas"]
            if params.get("create_board_teams") is True:
                args += ["--create-board-teams"]
            elif params.get("create_board_teams") is False:
                args += ["--no-create-board-teams"]
            if params.get("include_zephyr") is True:
                args += ["--include-zephyr"]
            elif params.get("include_zephyr") is False:
                args += ["--no-include-zephyr"]
            if params.get("include_test_plans") is True:
                args += ["--include-test-plans"]
            elif params.get("include_test_plans") is False:
                args += ["--no-include-test-plans"]
            code, out = run_step(job, 0, args)
            if code != 0:
                job.status, job.error = "failed", last_message(job)
                return
            cfg = out.strip().splitlines()[-1]
        _clear_pause_file(cfg)
        job.config_path = cfg
        job.result["config"] = cfg
        exp_step = ["export", "--force"] if params.get("force_export") else ["export"]
        steps_list = [["access"], exp_step, ["users"], ["check", "--online"], ["plan"]]
        for i, step in enumerate(steps_list, 1):
            code, _ = run_step(job, i, ["-c", cfg] + step)
            if code == 2:
                job.status = "paused"
                return
            if code != 0:
                job.status, job.error = "failed", last_message(job)
                return
            if job.paused and i < len(steps_list):
                _clear_pause_file(cfg)
                job.status = "paused"
                return
        _clear_pause_file(cfg)
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
        pv_path = os.path.join(os.path.dirname(full_cfg), "preview.json")
        pv = read_json_file(pv_path)
        if pv and isinstance(pv.get("tickets"), list):
            ts = m.get("ticket_states") or {}
            tt = m.get("ticket_types") or {}
            ta = m.get("ticket_actions") or {}
            so = m.get("status_overrides") or {}
            for t in pv["tickets"]:
                k = t["key"]
                if k in tt:
                    t["ado_type"] = tt[k]
                valid = t.get("valid_states") or (pv.get("valid_states") or {}).get(t.get("ado_type")) or []
                if t.get("jira_status") in so and k not in ts:
                    cand = so[t["jira_status"]]
                    if not valid or cand in valid:
                        t["ado_state"] = cand
                        t["state_match_reason"] = "override"
                if k in ts:
                    cand = ts[k]
                    if not valid or cand in valid:
                        t["ado_state"] = cand
                        t["state_match_reason"] = "override"
                    else:
                        ts.pop(k, None)
                if k in ta:
                    t["action"] = ta[k]
            with open(pv_path, "w", encoding="utf-8") as fh:
                json.dump(pv, fh, indent=1)
        with open(full_cfg, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True, width=120)
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
    batch_size = 0
    if params.get("batch_size") not in (None, "", 0, "0", "all"):
        try:
            batch_size = max(0, int(params["batch_size"]))
        except (TypeError, ValueError):
            raise ApiError("batch_size must be a positive integer.")
    only_keys = [str(k).strip() for k in (params.get("only_keys") or []) if str(k).strip()]
    for k in only_keys:
        if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", k, re.I):
            raise ApiError(f"Invalid Jira ticket key '{k}'.", 400)
    apply_overrides_to_config(cfg_path, params)
    _clear_pause_file(cfg_path)
    job = Job("import", ["Create & sync work items in Azure DevOps", "Verify migrated items"])
    job.config_path = cfg_path

    def work(job):
        imp_args = ["-c", cfg_path, "import", "--possible-duplicates", choice]
        if batch_size > 0:
            imp_args += ["--limit", str(batch_size)]
        if only_keys:
            imp_args += only_keys
        code, _ = run_step(job, 0, imp_args, ok=(0, 2))
        report = os.path.join(os.path.dirname(cfg_path), "verify.csv")
        res_pre = read_json_file(os.path.join(os.path.dirname(full_cfg), "import-result.json")) or {}
        batch_keys = res_pre.get("batch_keys") or []
        if os.path.exists(os.path.join(os.path.dirname(full_cfg), "import-state.jsonl")):
            if batch_size > 0:
                if batch_keys:
                    run_step(job, 1, ["-c", cfg_path, "verify", "--report", report] + list(batch_keys), ok=(0, 1))
                else:
                    job.steps[1]["status"] = "done"
            else:
                run_step(job, 1, ["-c", cfg_path, "verify", "--report", report], ok=(0, 1))
        r_data = run_data(cfg_path)
        job.result["run"] = r_data
        res_info = r_data.get("result") or {}
        failed_list = res_info.get("failed") or []
        if failed_list or code not in (0, 2):
            job.status = "failed"
            job.error = (f"Failed on {', '.join(failed_list)}: {last_message(job)}"
                         if failed_list else last_message(job))
            return
        if code == 2 or res_info.get("paused") or job.paused:
            _clear_pause_file(cfg_path)
            job.status = "paused"
            return
        if batch_size > 0 and (res_info.get("remaining") or 0) > 0:
            job.status = "batch_done"

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


def sync_zephyr_job(params):
    cfg_path = params.get("config") or ""
    inside_runs(cfg_path)
    if not (S.tokens.get("zephyr_access_key") and S.tokens.get("zephyr_secret_key")):
        raise ApiError("Connect Zephyr Squad API keys first (or add them to your local .env file).", 400)
    include_plans = params.get("include_test_plans", True)
    job = Job("sync_zephyr", ["Fetch Zephyr Squad steps/cycles & sync to Azure DevOps"])
    job.config_path = cfg_path

    def work(job):
        args = ["-c", cfg_path, "sync-zephyr"]
        if include_plans is False:
            args += ["--no-test-plans"]
        code, _ = run_step(job, 0, args, ok=(0,))
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


def run_data(cfg_path, include_preview=True):
    """Everything the page shows for one run: the preview (when include_preview=True), the import log and the reports."""
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
    out = {"config": cfg_path, "folder": folder,
           "run_info": cfg.get("run") or {},
           "result": read_json_file(os.path.join(full_folder, "import-result.json")),
           "journal": journal, "held": held, "verify": verify,
           "item_url": f"{org}/{quote(project)}/_workitems/edit/",
           "jira_browse": cfg["jira"]["base_url"].rstrip("/") + "/browse/",
           "files": {name: os.path.join(folder, name) for name in ("duplicates.csv", "verify.csv")
                     if os.path.exists(os.path.join(full_folder, name))}}
    if include_preview:
        out["preview"] = read_json_file(os.path.join(full_folder, "preview.json"))
    else:
        export_dir = os.path.join(KIT, (cfg.get("paths") or {}).get("export_dir", ""))
        keys_list = read_json_file(os.path.join(export_dir, "keys.json"))
        if isinstance(keys_list, list) and keys_list:
            issues_dir = os.path.join(export_dir, "issues")
            try:
                exp_count = sum(1 for fn in os.listdir(issues_dir) if fn.endswith(".json")) if os.path.isdir(issues_dir) else 0
            except OSError:
                exp_count = 0
            out["export_progress"] = {"exported": min(exp_count, len(keys_list)), "total": len(keys_list)}
    return out


def ticket_detail(cfg_path, key):
    """Return full description_html for a single ticket when the Inspector drawer opens."""
    from ..exporter import issue_path, load_users_file
    from ..planner import _description_html
    if not key or not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key, re.I):
        raise ApiError("Invalid Jira ticket key.", 400)
    full_cfg = inside_runs(cfg_path)
    cfg = config.load(full_cfg)
    export_dir = os.path.join(KIT, (cfg.get("paths") or {}).get("export_dir", ""))
    iss = read_json_file(issue_path(export_dir, key))
    if not iss:
        raise ApiError(f"Ticket {key} not found in export.", 404)
    users_path = os.path.join(KIT, (cfg.get("paths") or {}).get("users_file") or "users.csv")
    users = load_users_file(users_path) or {"ids": {}, "names": {}}
    return {
        "key": key,
        "description_html": _description_html(iss, users.get("names") or "") or "",
        "zephyr_steps": iss.get("zephyr_steps") or [],
    }


def list_saved_runs():
    """Scan runs/ for existing run folders so interrupted, paused, or failed runs can be recovered."""
    runs_root = os.path.join(KIT, "runs")
    if not os.path.isdir(runs_root):
        return []
    out = []
    for root, _, files in os.walk(runs_root):
        if "config.yaml" not in files:
            continue
        full_cfg = os.path.join(root, "config.yaml")
        rel_cfg = os.path.relpath(full_cfg, KIT)
        try:
            cfg = config.load(full_cfg)
        except Exception:
            continue
        run_info = cfg.get("run") or {}
        mode = run_info.get("mode") or ("sprints" if run_info.get("sprints") else "project")
        source = run_info.get("from") or ""
        target = run_info.get("to") or (cfg.get("ado") or {}).get("project") or ""
        board_name = run_info.get("board_name") or run_info.get("board_id") or ""
        sprints = [s.get("name") for s in (run_info.get("sprints") or []) if isinstance(s, dict) and s.get("name")]
        if mode == "board":
            scope_label = f"Board: {board_name or 'All board tickets'}"
        elif mode == "project":
            scope_label = f"Entire Project ({source})"
        else:
            scope_label = ", ".join(sprints[:3]) + (f" (+{len(sprints) - 3} more)" if len(sprints) > 3 else "") or "Sprint run"
        pv = read_json_file(os.path.join(root, "preview.json"))
        res = read_json_file(os.path.join(root, "import-result.json")) or {}
        exp_keys = read_json_file(os.path.join(KIT, (cfg.get("paths") or {}).get("export_dir", ""), "keys.json")) or []
        data = run_data(rel_cfg, include_preview=False)
        tickets = (pv or {}).get("tickets") or []
        total = len(tickets) or len(exp_keys)
        completed = 0
        partial = 0
        skipped = len(data.get("held") or {})
        for t in tickets:
            j = data["journal"].get(t["key"])
            if j:
                if j.get("found") or j.get("revs", 0) >= t.get("revisions", 1):
                    completed += 1
                elif j.get("revs", 0) > 0:
                    partial += 1
        if not tickets and data["journal"]:
            completed = len(data["journal"])
        failed_list = res.get("failed") or []
        failed = len(failed_list)
        remaining = res.get("remaining") if "remaining" in res else max(0, total - completed - skipped - failed)
        if pv is None:
            state = "export_incomplete"
        elif failed > 0 or partial > 0:
            state = "failed"
        elif res.get("paused"):
            state = "paused"
        elif (completed > 0 or skipped > 0) and remaining > 0:
            state = "batch_done"
        elif (completed > 0 or skipped > 0) and remaining == 0 and res:
            state = "completed"
        else:
            state = "validated"
        mtime = os.path.getmtime(full_cfg)
        for fn in ("import-state.jsonl", "import-result.json", "preview.json"):
            fp = os.path.join(root, fn)
            if os.path.exists(fp):
                mtime = max(mtime, os.path.getmtime(fp))
        out.append({
            "config": rel_cfg,
            "mode": mode,
            "source": source,
            "target": target,
            "board_id": run_info.get("board_id"),
            "board_name": board_name,
            "scope_label": scope_label,
            "has_preview": pv is not None,
            "total": total,
            "completed": completed,
            "partial": partial,
            "failed": failed,
            "failed_keys": failed_list,
            "skipped": skipped,
            "remaining": remaining,
            "state": state,
            "updated_ts": mtime,
        })
    out.sort(key=lambda r: r["updated_ts"], reverse=True)
    return out


# ---------------------------------------------------------------- API

def api(method, path, query, body):
    if path == "/api/state":
        s = load_settings()
        b = base_cfg()
        env_file = _find_local_env_file()
        return {
            "jira": S.names.get("jira"),
            "ado": S.names.get("ado"),
            "zephyr": S.names.get("zephyr"),
            "jira_url": S.tokens.get("jira_url") or s.get("jira_url") or b["jira"].get("base_url") or "",
            "ado_url": S.tokens.get("ado_url") or s.get("ado_url") or b["ado"].get("org_url") or "",
            "zephyr_url": S.tokens.get("zephyr_base_url") or s.get("zephyr_base_url") or (b.get("zephyr") or {}).get("base_url") or DEFAULT_SQUAD_URL,
            "has_env_file": bool(env_file),
            "env_file_name": os.path.basename(env_file) if env_file else "",
            "connected_from_env": S.connected_from_env,
            "settings": s,
            "running": S.current if S.current and S.jobs[S.current].status == "running" else None,
        }

    if path == "/api/connect-env" and method == "POST":
        env_file = _find_local_env_file()
        if not env_file:
            raise ApiError("No local .env credentials file found. Copy .env.example to .env and fill in your keys.", 404)
        res = auto_connect_from_env(env_file)
        if res is None:
            raise ApiError(f"{os.path.basename(env_file)} is missing Jira or Azure DevOps credentials (or still has placeholder values).", 400)
        return res

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
        return _connect_credentials(
            j_url, email, jt, a_url, at,
            z_ak=body.get("zephyr_access_key") or "",
            z_sk=body.get("zephyr_secret_key") or "",
            z_url=body.get("zephyr_base_url") or "",
            from_env=False,
        )

    if path == "/api/disconnect" and method == "POST":
        S.tokens.clear()
        S.names.clear()
        S.connected_from_env = False
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
        mode = (body.get("mode") or "sprints").strip().lower()
        if mode not in ("sprints", "board", "project"):
            raise ApiError(f"Unsupported migration mode '{mode}'.")
        source, target = (body.get("source") or "").strip(), (body.get("target") or "").strip()
        board = (body.get("board") or "").strip() or None
        sprints = [str(x) for x in body.get("sprints") or [] if str(x).strip()]
        if mode == "sprints" and not (source and target and sprints):
            raise ApiError("Choose a Jira project, at least one sprint and an Azure DevOps project.")
        if mode == "board" and not (source and board and target):
            raise ApiError("Choose a Jira project, a Jira board and an Azure DevOps project.")
        if mode == "project" and not (source and target):
            raise ApiError("Choose a Jira project and an Azure DevOps project.")
        save_settings(source=source, board=board, target=target, mode=mode)
        return check_job({
            "mode": mode,
            "source": source,
            "board": board,
            "target": target,
            "sprints": sprints,
            "iteration": (body.get("iteration") or "").strip() or None,
            "area": (body.get("area") or "").strip() or None,
            "include_done": bool(body.get("include_done")),
            "include_hierarchy": bool(body.get("include_hierarchy")),
            "history": body.get("history", True),
            "boards_to_areas": body.get("boards_to_areas") if "boards_to_areas" in body else None,
            "create_board_teams": body.get("create_board_teams") if "create_board_teams" in body else None,
            "include_zephyr": body.get("include_zephyr") if "include_zephyr" in body else None,
            "include_test_plans": body.get("include_test_plans") if "include_test_plans" in body else None,
            "force_export": bool(body.get("force_export")),
        }).public()

    if path == "/api/runs" and method == "GET":
        return {"runs": list_saved_runs()}

    if path == "/api/runs/recover" and method == "POST":
        cfg_path = (body.get("config") or "").strip()
        if not cfg_path:
            raise ApiError("Missing run config path.")
        full_cfg = inside_runs(cfg_path)
        if not os.path.exists(full_cfg):
            raise ApiError("Run config file not found.", 404)
        pv_path = os.path.join(os.path.dirname(full_cfg), "preview.json")
        if os.path.exists(pv_path):
            return {"ok": True, "recovered": "loaded", "run": run_data(cfg_path)}
        if not (S.tokens.get("jira_token") and S.tokens.get("ado_token")):
            raise ApiError("Connect to Jira and Azure DevOps first to resume an incomplete export/validation.", 401)
        job = check_job({}, existing_cfg=cfg_path)
        return {"ok": True, "recovered": "resuming_check", "job": job.public()}

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

    if path == "/api/sync-zephyr" and method == "POST":
        if not (S.tokens.get("jira_token") and S.tokens.get("ado_token")):
            raise ApiError("Connect to Jira and Azure DevOps first.", 401)
        return sync_zephyr_job(body).public()

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
        if len(parts) > 4 and parts[4] == "pause" and method == "POST":
            job.paused = True
            if job.config_path:
                try:
                    full_cfg = inside_runs(job.config_path)
                    with open(os.path.join(os.path.dirname(full_cfg), ".pause"), "w", encoding="utf-8") as fh:
                        fh.write("pause\n")
                except Exception:
                    pass
            job.log.append("Pause requested — finishing in-flight tickets cleanly...")
            return job.public()
        return job.public(int((query.get("since") or ["0"])[0]))

    if path == "/api/run/progress":
        return run_data((query.get("config") or [""])[0], include_preview=False)

    if path == "/api/run":
        return run_data((query.get("config") or [""])[0], include_preview=True)

    if path == "/api/ticket":
        return ticket_detail((query.get("config") or [""])[0], (query.get("key") or [""])[0].strip().upper())

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
    try:
        env_res = auto_connect_from_env()
        if env_res and env_res.get("jira") and env_res.get("ado"):
            z_msg = f", Zephyr: {env_res['zephyr']}" if env_res.get("zephyr") else ""
            print(f"Loaded credentials from local .env (Jira: {env_res['jira']}, ADO: {env_res['ado']}{z_msg})")
    except Exception:
        pass
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
