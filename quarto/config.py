import copy
import os
import sys

import yaml

BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def frozen_workspace_dir():
    """Return a writable workspace directory for standalone packaged builds so
    read-only macOS .app / App Translocation or Program Files never fail."""
    custom = (os.environ.get("QUARTO_HOME") or "").strip()
    if custom:
        path = os.path.abspath(os.path.expanduser(custom))
        os.makedirs(path, exist_ok=True)
        return path
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    base = docs if os.path.isdir(docs) and os.access(docs, os.W_OK) else home
    path = os.path.join(base, "Quarto")
    os.makedirs(path, exist_ok=True)
    return path


def resolve_config_path(path):
    """Return path if it exists in the working directory, else fall back to the bundled copy."""
    if not path:
        return path
    if os.path.exists(path):
        return path
    bundled = os.path.join(BUNDLE_DIR, path)
    return bundled if os.path.exists(bundled) else path

DEFAULTS = {
    "jira": {"base_url": "", "jql": "", "board_id": None, "board_name": None},
    "zephyr": {
        "enabled": False,
        "provider": "squad",
        "base_url": "https://prod-api.zephyr4jiracloud.com/connect",
        "include_step_attachments": True,
        "link_as_tested_by": True,
        "include_test_plans": True,
        "test_issue_types": ["Test", "Test Case"],
    },
    "ado": {
        "org_url": "",
        "project": "",
        "team": None,
        "area_path": None,
        "create_area": False,
        "subscribe_team_iterations": True,
        "create_board_teams": True,
        "api_version": "7.1",
        # Ask for the project's name instead of "yes" before writing. Keep true for production projects.
        "confirm_with_project_name": True,
    },
    # state_dir holds what belongs to one target project: the import log, plans and reports.
    # Unset means the export folder, as in older configs.
    "paths": {"export_dir": "./export", "users_file": "./users.csv", "state_dir": None},
    "mapping": {
        # true: replay every Jira field change, comment, attachment, and worklog as an ADO revision.
        # false: current values only, then comments and attachments with original authors and dates.
        "history": True,
        # Jira issue type -> ADO work item type (Scrum process by default; auto-adapts to Agile/Basic/CMMI)
        "types": {
            "Epic": "Epic",
            "Story": "Product Backlog Item",
            "Task": "Product Backlog Item",
            "Bug": "Bug",
            "Sub-task": "Task",
            "Subtask": "Task",
        },
        "default_type": "Product Backlog Item",
        # Explicit Jira status -> ADO state, per ADO type. "default" applies to all types.
        "states": {"default": {}},
        # Used when a status has no explicit mapping. Keys are Jira status categories.
        "state_by_category": {
            "default": {"new": "New", "indeterminate": "Committed", "done": "Done"},
            "Product Backlog Item": {"new": "New", "indeterminate": "Committed", "done": "Done"},
            "User Story": {"new": "New", "indeterminate": "Active", "done": "Closed"},
            "Bug": {"new": "New", "indeterminate": "Committed", "done": "Done"},
            "Task": {"new": "To Do", "indeterminate": "In Progress", "done": "Done"},
            "Issue": {"new": "To Do", "indeterminate": "Doing", "done": "Done"},
            "Requirement": {"new": "Proposed", "indeterminate": "Active", "done": "Closed"},
            "Epic": {"new": "New", "indeterminate": "In Progress", "done": "Done"},
            "Feature": {"new": "New", "indeterminate": "In Progress", "done": "Done"},
            "Test Case": {"new": "Design", "indeterminate": "Design", "done": "Ready"},
        },
        "done_states": ["Done", "Closed", "Resolved", "Completed", "Ready"],
        # Jira resolution -> ADO state, used when the status maps to a done state.
        "resolution_states": {"default": {}},
        # Per-ticket user overrides from the interactive validation UI:
        # ticket_states: {jira_key: ado_state}
        # ticket_types:  {jira_key: ado_work_item_type}
        # ticket_actions: {jira_key: "create" | "skip" | "duplicate" | "sync"}
        "ticket_states": {},
        "ticket_types": {},
        "ticket_actions": {},
        # Global status overrides from the UI dropdowns: {jira_status: ado_state}
        "status_overrides": {},
        # Extra fields: {ado: reference name, jira: [field names or ids], kind: text|html|number|user|users}
        # "users" takes the first person; the others are listed in a note on the item.
        "custom_fields": [],
        # Tag added for a Jira issue type, for example {"Prod Bug": "Prod Bug"}.
        "type_tags": {},
        # Iteration node that sprints are created under.
        "iteration_parent": None,
        # Place each item by these Jira sprints only (set by run.sh --sprint).
        "only_sprints": None,
        # Put every item in this iteration instead of one named after its sprint (run.sh --iteration).
        "iteration_override": None,
        # Duplicate detection & policy:
        # possible_duplicates: "skip" | "create"
        # existing_duplicates: "skip" | "create" | "sync"
        "duplicate_check_titles": True,
        "possible_duplicates": "skip",
        "existing_duplicates": "skip",
        # Leave items in future sprints on the backlog instead of creating those sprints.
        "skip_future_sprints": False,
        # Which links to migrate: all, parent (parent/child only) or none.
        "link_mode": "all",
        # Golden Quadrant & 100% Fidelity options:
        "include_worklogs": True,
        "include_remote_links": True,
        "include_dev_links": True,
        "include_hierarchy": False,
        "preserve_rank": True,
        "rewrite_cross_references": True,
        # Rewrite Jira email domains when building users.csv, for example {"company.com": "company.org"}.
        "identity_domains": {},
        # Add "Jira status: ..." to the note on each new item, useful when the target has fewer states.
        "note_jira_status": False,
        "priority": {"Urgent": 1, "Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 4},
        # Set on every new item. Required fields are not defaulted when rules are bypassed.
        # Use null under a type to leave a field off that type.
        "create_defaults": {
            "default": {"Microsoft.VSTS.Common.ValueArea": "Business"},
            "Task": {"Microsoft.VSTS.Common.ValueArea": None},
            "Test Case": {"Microsoft.VSTS.Common.ValueArea": None},
            "Issue": {"Microsoft.VSTS.Common.ValueArea": None},
        },
        "description_field": {
            "default": "System.Description",
            "Bug": "Microsoft.VSTS.TCM.ReproSteps",
        },
        "points_field": {"default": "Microsoft.VSTS.Scheduling.Effort", "Task": None},
        "points_jira_names": ["Story Points", "Story point estimate"],
        "sprint_jira_name": "Sprint",
        "sprints_to_iterations": True,
        "boards_to_areas": False,
        "tags": {"labels": True, "components": "component:", "versions": "version:", "jira_key": True},
        "jira_key_field": None,  # e.g. Custom.JiraKey, if you add that field to the process
        "links": {
            "default": "System.LinkTypes.Related",
            "Blocks": "System.LinkTypes.Dependency-Forward",
            "Duplicate": "System.LinkTypes.Duplicate-Reverse",
            "Cloners": "System.LinkTypes.Related",
            "Relates": "System.LinkTypes.Related",
        },
        # Jira field changes with no ADO target are written into the history as notes.
        "history_notes": True,
        "history_ignore": [
            "Rank", "Link", "RemoteIssueLink", "Attachment", "WorklogId", "timespent",
            "timeestimate", "Epic Child", "IssueParentAssociation", "Parent",
            "Flagged", "development",
        ],
    },
}


def _dotenv_candidates(path=None):
    """Return candidate secret file paths in priority order (never committed to git)."""
    if path:
        return [path]
    cands = []
    env_override = (os.environ.get("QUARTO_SECRETS_FILE") or "").strip()
    if env_override:
        cands.append(os.path.expanduser(env_override))
    for name in (".env", ".secrets.env"):
        cands.append(name)
        bundled = os.path.join(BUNDLE_DIR, name)
        if os.path.abspath(bundled) != os.path.abspath(name):
            cands.append(bundled)
    return cands


def find_dotenv(path=None):
    """Return the first existing local credentials file path, or None."""
    for cand in _dotenv_candidates(path):
        if cand and os.path.isfile(cand):
            return cand
    return None


def read_dotenv(path=None):
    """Parse key=value pairs from the local credentials file without mutating os.environ."""
    found = find_dotenv(path)
    if not found:
        return {}
    out = {}
    try:
        with open(found, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    out[k] = v
    except OSError:
        return {}
    return out


def load_dotenv(path=None):
    """Load key=value pairs from a local .env / secrets file if present, without overwriting existing env vars."""
    for k, v in read_dotenv(path).items():
        if k and k not in os.environ:
            os.environ[k] = v


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _read(path):
    """Read a config file. A file can start from another one with  extends: other.yaml"""
    real_path = resolve_config_path(path)
    with open(real_path, encoding="utf-8") as fh:
        user = yaml.safe_load(fh) or {}
    base = user.pop("extends", None)
    if base:
        parent = _read(os.path.join(os.path.dirname(os.path.abspath(real_path)), base))
        user = _merge(parent, user)
    return user


def load(path):
    load_dotenv()
    real_path = resolve_config_path(path)
    user = _read(real_path) if real_path and os.path.exists(real_path) else {}
    cfg = _merge(copy.deepcopy(DEFAULTS), user)
    if os.environ.get("JIRA_BASE_URL") and not user.get("jira", {}).get("base_url"):
        cfg["jira"]["base_url"] = os.environ["JIRA_BASE_URL"]
    if os.environ.get("ADO_ORG_URL") and not user.get("ado", {}).get("org_url"):
        cfg["ado"]["org_url"] = os.environ["ADO_ORG_URL"]
    if os.environ.get("ZEPHYR_BASE_URL") and not user.get("zephyr", {}).get("base_url"):
        cfg["zephyr"]["base_url"] = os.environ["ZEPHYR_BASE_URL"]
    cfg["jira"]["base_url"] = (cfg["jira"]["base_url"] or "").rstrip("/")
    cfg["ado"]["org_url"] = (cfg["ado"]["org_url"] or "").rstrip("/")
    cfg["zephyr"]["base_url"] = (cfg["zephyr"].get("base_url") or "https://prod-api.zephyr4jiracloud.com/connect").rstrip("/")
    return cfg


def secret(name):
    load_dotenv()
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Set the {name} environment variable first.")
    return v


def secret_optional(name):
    load_dotenv()
    return (os.environ.get(name) or "").strip()
