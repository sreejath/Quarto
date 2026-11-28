"""Turn one exported Jira issue into an ordered list of ADO revisions.

Two modes, set by mapping.history:
- true:  replay every field change, comment, attachment, and worklog in time order,
         starting from the values the issue had when it was created.
- false: create the item with its current values, then add comments, attachments,
         and worklogs in time order with their original authors and dates.

Pure logic: no network calls, so it is testable and can be run as a dry run.
"""
from difflib import SequenceMatcher
import os
import re
import threading

from .adf import adf_to_html, esc, plain_to_html
from .util import iso, iteration_node, name_key, node_name, parse_dt
from .wiki import wiki_to_html
from .zephyr import step_attachment_key, steps_to_ado_xml

SCALARS = ("title", "description", "status", "resolution", "priority", "assignee", "labels", "points", "sprint")
SETS = ("components", "versions")

# Synonym buckets for intelligent closest-state matching across Scrum, Agile, Basic, CMMI, and custom processes
_STATE_SYNONYMS = [
    {"new", "to do", "todo", "open", "backlog", "proposed", "design", "created", "requested", "triage"},
    {"ready", "approved", "selected for development", "ready for dev", "ready for development", "groomed", "refined"},
    {"in progress", "development", "in dev", "active", "committed", "doing", "working", "blocked", "on hold", "coding"},
    {"in review", "pr review", "ready for pr review", "code review", "peer review", "review"},
    {"qa", "in qa", "ready for qa", "testing", "in test", "test", "verification", "sit"},
    {"uat", "acceptance", "user acceptance", "product review", "business review", "sign off", "ready for release",
     "release", "staging", "pre prod", "promotion"},
    {"done", "closed", "resolved", "completed", "complete", "released", "shipped", "verified"},
    {"removed", "cancelled", "canceled", "won't do", "wont do", "rejected", "duplicate", "cannot reproduce", "abandoned"},
]


def _norm_state(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def closest_ado_state(status, wit, valid_states=None, cfg=None, meta=None, resolution=None):
    """Find the closest matching ADO state for a Jira status.
    Returns (ado_state, is_confident_match, match_reason).
    Order of precedence:
    1. User status override in cfg['mapping']['status_overrides']
    2. Exact case/punctuation-insensitive match in valid_states (when ADO has identically named state)
    3. Explicit config mapping in cfg['mapping']['states'] (if valid in valid_states or offline)
    4. Fuzzy / token-overlap / synonym match against valid_states
    5. Jira statusCategory fallback (new / indeterminate / done) mapped into valid_states
    """
    m = (cfg or {}).get("mapping") or {}
    meta = meta or {}
    valid = list(valid_states) if valid_states else []
    valid_by_norm = {_norm_state(v): v for v in valid}

    # Check resolution override for done items first if applicable
    def apply_resolution(chosen_state, ok_flag, reason):
        if chosen_state in m.get("done_states", ["Done"]) and resolution:
            for scope in (wit, "default"):
                hit = ((m.get("resolution_states") or {}).get(scope) or {}).get(resolution)
                if hit and (not valid or hit in valid):
                    return hit, ok_flag, "resolution"
        return chosen_state, ok_flag, reason

    # 1. Explicit UI status override
    ov = (m.get("status_overrides") or {}).get(status)
    if ov and (not valid or ov in valid):
        return apply_resolution(ov, True, "override")

    # 2. Explicit config mapping (if it exists in valid_states or when offline)
    cfg_hit = None
    for scope in (wit, "default"):
        hit = ((m.get("states") or {}).get(scope) or {}).get(status)
        if hit:
            cfg_hit = hit
            break
    if cfg_hit and (not valid or cfg_hit in valid):
        return apply_resolution(cfg_hit, True, "configured")

    # 3. Exact case/punctuation-insensitive match in valid_states (when ADO has an identically named state)
    norm_st = _norm_state(status)
    if valid and norm_st in valid_by_norm:
        return apply_resolution(valid_by_norm[norm_st], True, "exact")

    cat = (meta.get("statuses") or {}).get(status)
    ado_cats = ((meta.get("state_categories_by_type") or {}).get(wit)) or {}
    want_ado_cats = {"new": {"Proposed"}, "indeterminate": {"InProgress", "Resolved"}, "done": {"Completed"}}.get(cat, set())

    # 4. Fuzzy & synonym match against valid_states
    if valid and status:
        best_state, best_score = None, 0.0
        st_tokens = set(norm_st.split())
        st_buckets = [i for i, b in enumerate(_STATE_SYNONYMS) if norm_st in b or any(t in b for t in st_tokens)]
        for cand in valid:
            norm_c = _norm_state(cand)
            c_tokens = set(norm_c.split())
            # Substring containment (e.g. "Product Review" in "Ready for Product Review" or "In QA" -> "QA")
            sub_bonus = 0.25 if (norm_st and norm_c and (norm_st in norm_c or norm_c in norm_st)) else 0.0
            # Token overlap
            tok_overlap = (len(st_tokens & c_tokens) / max(len(st_tokens | c_tokens), 1)) * 0.45 if (st_tokens and c_tokens) else 0.0
            # Synonym bucket match (exact phrase in same bucket gets 0.60; token overlap in bucket gets 0.50)
            c_buckets = [i for i, b in enumerate(_STATE_SYNONYMS) if norm_c in b or any(t in b for t in c_tokens)]
            exact_syn = any(norm_st in b and norm_c in b for b in _STATE_SYNONYMS)
            syn_bonus = 0.60 if exact_syn else (0.50 if (st_buckets and c_buckets and set(st_buckets) & set(c_buckets)) else 0.0)
            # Category alignment bonus/penalty when ADO state categories and Jira statusCategory are known
            cat_adj = 0.0
            if want_ado_cats and cand in ado_cats:
                cat_adj = 0.15 if ado_cats[cand] in want_ado_cats else -0.25
            # Character sequence similarity
            seq_score = SequenceMatcher(None, norm_st, norm_c).ratio() * 0.5
            score = seq_score + sub_bonus + tok_overlap + syn_bonus + cat_adj
            if score > best_score:
                best_score, best_state = score, cand
        if best_state and best_score >= 0.52:
            return apply_resolution(best_state, True, "fuzzy")

    # 5. Category fallback (using live ADO state categories first, then synonym buckets)
    by_cat_map = m.get("state_by_category") or {}
    by_cat = by_cat_map.get(wit) or by_cat_map.get("default") or {"new": "New", "indeterminate": "Committed", "done": "Done"}
    cat_choice, ok = (by_cat[cat], True) if cat in by_cat else (by_cat.get("new", "New"), False)
    if valid and cat_choice not in valid:
        # First check live ADO state_categories_by_type if available
        matched_by_ado_cat = next((cand for cand in valid if want_ado_cats and ado_cats.get(cand) in want_ado_cats), None)
        if matched_by_ado_cat:
            cat_choice = matched_by_ado_cat
        else:
            # Pick the closest valid state in the same semantic category bucket
            cat_bucket_idx = {"new": 0, "indeterminate": 2, "done": 6}.get(cat, 0)
            bucket = _STATE_SYNONYMS[cat_bucket_idx]
            for cand in valid:
                if _norm_state(cand) in bucket:
                    cat_choice = cand
                    break
            else:
                cat_choice = valid[0]
    if cfg_hit and not valid:
        return apply_resolution(cfg_hit, True, "configured")
    return apply_resolution(cat_choice, ok, "category" if ok else "fallback")


def is_subtask_issue(issue):
    """Return True if the Jira issue is a Sub-task (by issuetype.subtask flag or type name)."""
    it = ((issue or {}).get("fields") or {}).get("issuetype") or {}
    if it.get("subtask") is True:
        return True
    return (it.get("name") or "").strip().lower() in ("sub-task", "subtask")


def type_for(issue, cfg, available_wits=None):
    key = issue.get("key")
    m = cfg["mapping"]
    if key and key in (m.get("ticket_types") or {}):
        return m["ticket_types"][key]
    name = issue["fields"]["issuetype"]["name"]
    chosen = m["types"].get(name, m["default_type"])
    if available_wits and chosen not in available_wits:
        # Auto-adapt across Scrum (Product Backlog Item), Agile (User Story), Basic (Issue), CMMI (Requirement)
        fallbacks = {
            "Product Backlog Item": ["User Story", "Issue", "Requirement", "Task"],
            "User Story": ["Product Backlog Item", "Issue", "Requirement", "Task"],
            "Feature": ["Epic", "Product Backlog Item", "User Story", "Issue"],
            "Epic": ["Feature", "Issue"],
            "Bug": ["Issue", "Defect", "Product Backlog Item", "User Story", "Task"],
            "Task": ["Issue", "Product Backlog Item", "User Story"],
            "Test Case": ["Task", "Issue", "Product Backlog Item"],
        }
        for alt in fallbacks.get(chosen, []) + list(available_wits):
            if alt in available_wits:
                return alt
    return chosen


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _att_names(issue):
    return {a["filename"]: a["id"] for a in issue["fields"].get("attachment") or []}


def _description_html(issue, user_names=None):
    rendered = (issue.get("renderedFields") or {}).get("description")
    if rendered and "<!-- ADF macro (type = 'table') -->" not in rendered:
        return rendered
    return adf_to_html(issue["fields"].get("description"), _att_names(issue), user_names) or (rendered or "")


def comment_html(comment, issue, user_names=None):
    if comment.get("renderedBody"):
        return comment["renderedBody"]
    return adf_to_html(comment.get("body"), _att_names(issue), user_names) or "<i>(empty comment)</i>"


def worklog_html(wl, issue, user_names=None):
    spent = wl.get("timeSpent") or (f"{round(wl['timeSpentSeconds'] / 3600.0, 2)}h" if wl.get("timeSpentSeconds") else "time")
    body = ""
    if wl.get("renderedBody"):
        body = wl["renderedBody"]
    elif wl.get("comment"):
        body = adf_to_html(wl.get("comment"), _att_names(issue), user_names) if isinstance(wl.get("comment"), dict) else wiki_to_html(str(wl.get("comment")), _att_names(issue), user_names)
    return f"<div><i>Jira worklog: logged <b>{esc(spent)}</b></i>" + (f"<br>{body}" if body else "") + "</div>"


def field_ids(meta, names):
    """Jira field ids for a list of field names or ids, in the order given."""
    by_name = {}
    for fid, name in (meta.get("field_names") or {}).items():
        by_name.setdefault(name.lower(), []).append(fid)
    out = []
    for n in names if isinstance(names, list) else [names]:
        if n.startswith("customfield_") or n in (meta.get("field_names") or {}):
            out.append(n)
        else:
            out += by_name.get(n.lower(), [])
    return out


def custom_value(issue, fid, kind, names, user_names=None):
    """Read one Jira custom field as (value, extra people) for the given kind."""
    f, rendered = issue["fields"], issue.get("renderedFields") or {}
    v = f.get(fid)
    if v in (None, "", []):
        return None, []
    if kind == "html":
        if rendered.get(fid):
            return rendered[fid], []
        return (adf_to_html(v, names, user_names) if isinstance(v, dict) else wiki_to_html(str(v), names, user_names)), []
    if kind == "number":
        return _num(v), []
    if kind == "user":
        return (v.get("accountId") if isinstance(v, dict) else None), []
    if kind == "users":
        people = [u for u in (v if isinstance(v, list) else [v]) if isinstance(u, dict) and u.get("accountId")]
        if not people:
            return None, []
        return people[0]["accountId"], [u.get("displayName") or u["accountId"] for u in people[1:]]
    # text
    if isinstance(v, list):
        return "; ".join(str(x.get("value", x.get("name", x))) if isinstance(x, dict) else str(x) for x in v), []
    if isinstance(v, dict):
        return str(v.get("value") or v.get("name") or v.get("displayName") or ""), []
    return str(v), []


def _is_issue_done(issue, meta):
    """Return True if the Jira issue is currently in a Done/completed status or resolved."""
    f = (issue or {}).get("fields") or {}
    status_name = (f.get("status") or {}).get("name") or ""
    cat = ((meta or {}).get("statuses") or {}).get(status_name)
    if cat == "done":
        return True
    if cat in ("new", "indeterminate"):
        return False
    if (f.get("resolution") or {}).get("name"):
        return True
    return status_name.strip().lower() in ("done", "closed", "resolved", "completed", "complete")


def sprint_board_for(s, meta, ticket_board=None):
    """Return the board area name that owns sprint dict `s`, falling back to `ticket_board`."""
    if not isinstance(s, dict):
        return ticket_board
    sb = (meta or {}).get("sprint_boards") or {}
    sid = s.get("id") if s.get("id") is not None else s.get("number")
    if sid is not None and str(sid) in sb:
        return sb[str(sid)]
    boards = (meta or {}).get("boards") or []
    if boards:
        by_id = {str(b["id"]): b["area"] for b in boards if isinstance(b, dict) and b.get("id") is not None and b.get("area")}
        for field in ("originBoardId", "boardId"):
            bid = s.get(field)
            if bid is not None and str(bid) in by_id:
                return by_id[str(bid)]
        for bid in s.get("board_ids") or []:
            if bid is not None and str(bid) in by_id:
                return by_id[str(bid)]
    return ticket_board


def sprint_label(s, colliding=None, board=None):
    """Return the sprint's iteration label, disambiguated with its Jira Sprint ID if another sprint shares its name."""
    if not isinstance(s, dict):
        return str(s or "")
    name = s.get("name") or ""
    sid = s.get("id") if s.get("id") is not None else s.get("number")
    if colliding and sid is not None:
        nk = name_key(name)
        bkey = name_key(board) if board else ""
        if nk in colliding or (bkey, nk) in colliding:
            return f"{name} ({sid})"
    return name


def sprint_path_label(s, colliding=None, board=None, boards_to_areas=False):
    """Return the sprint label, prefixed with `<Board>\\` when `boards_to_areas` is active and `board` is known."""
    lbl = sprint_label(s, colliding=colliding, board=board if boards_to_areas else None)
    if boards_to_areas and board:
        return f"{node_name(board)}\\{node_name(lbl)}"
    return lbl


def discover_sprint_collisions(cfg, meta, issues=None):
    """Return normalized sprint names (or `(board_key, sprint_key)` tuples when `boards_to_areas` is enabled)
    that belong to 2+ distinct Jira Sprint IDs within the same iteration parent."""
    boards_to_areas = bool(((cfg or {}).get("mapping") or {}).get("boards_to_areas"))
    ticket_boards = (meta or {}).get("ticket_boards") or {}
    ids_by_key = {}

    def _add(sp, t_board=None):
        if not isinstance(sp, dict):
            return
        sid = sp.get("id") if sp.get("id") is not None else sp.get("number")
        sname = sp.get("name")
        if sid is None or not sname:
            return
        nk = name_key(sname)
        if boards_to_areas:
            b = sprint_board_for(sp, meta, ticket_board=t_board)
            key = (name_key(b) if b else "", nk)
        else:
            key = nk
        ids_by_key.setdefault(key, set()).add(str(sid))

    for sp in ((cfg or {}).get("run") or {}).get("sprints") or []:
        _add(sp)
    for sp in (meta or {}).get("sprints") or []:
        _add(sp)

    sprint_fids = (meta or {}).get("sprint_fields") or []
    if issues is None and cfg and sprint_fids:
        export_dir = ((cfg.get("paths") or {}).get("export_dir")) or ""
        if export_dir and os.path.isdir(os.path.join(export_dir, "issues")):
            from .exporter import load_issues
            try:
                issues = list(load_issues(export_dir))
            except Exception:
                issues = []
    for iss in (issues or []):
        f = (iss or {}).get("fields") or {}
        t_board = ticket_boards.get((iss or {}).get("key")) if boards_to_areas else None
        for fid in sprint_fids:
            for s in f.get(fid) or []:
                _add(s, t_board=t_board)
    return {k for k, sids in ids_by_key.items() if len(sids) > 1}


def _issue_sprint_lookup(issue, meta, colliding, boards_to_areas=False, ticket_board=None):
    """Build {sprint_id: label}, {norm_name: label} from global meta sprints and the issue's sprint fields."""
    by_id, by_norm = {}, {}
    preferred_norm = set()

    def _record(s, prefer=False):
        if not isinstance(s, dict) or not s.get("name"):
            return
        b = sprint_board_for(s, meta, ticket_board=ticket_board) if boards_to_areas else None
        lbl = sprint_path_label(s, colliding=colliding, board=b, boards_to_areas=boards_to_areas)
        sid = s.get("id") if s.get("id") is not None else s.get("number")
        if sid is not None:
            by_id[str(sid)] = lbl
        nk = name_key(s["name"])
        same_board = boards_to_areas and ticket_board and b and name_key(b) == name_key(ticket_board)
        if prefer or same_board or nk not in preferred_norm:
            by_norm[nk] = lbl
            if prefer or same_board:
                preferred_norm.add(nk)

    for s in (meta or {}).get("sprints") or []:
        _record(s, prefer=False)
    f = (issue or {}).get("fields") or {}
    for fid in (meta or {}).get("sprint_fields") or []:
        for s in f.get(fid) or []:
            _record(s, prefer=True)
    return by_id, by_norm


def _pick_current_sprint(issue, meta, only=None, colliding=None, boards_to_areas=False, ticket_board=None):
    """Pick the issue's current sprint dict (or None) and build {label_or_name: state}."""
    sprints, states = [], {}
    for fid in (meta or {}).get("sprint_fields", []):
        val = issue["fields"].get(fid)
        for s in val if isinstance(val, list) else []:
            if isinstance(s, dict) and s.get("name"):
                b = sprint_board_for(s, meta, ticket_board=ticket_board) if boards_to_areas else None
                lbl = sprint_path_label(s, colliding=colliding, board=b, boards_to_areas=boards_to_areas)
                states[lbl] = s.get("state")
                states[s["name"]] = s.get("state")
                sprints.append(s)
    if only:
        wanted_names = {name_key(str(n)) for n in only}
        wanted_ids = {str(n) for n in only}
        sprints = [
            s for s in sprints
            if str(s.get("id")) in wanted_ids
            or name_key(s["name"]) in wanted_names
            or name_key(sprint_label(s, colliding)) in wanted_names
        ] or sprints
    if not sprints:
        return None, states
    if not any(s.get("state") for s in sprints):
        return sprints[-1], states  # no state information: keep Jira's order
    active = [s for s in sprints if s.get("state") == "active"]
    future = sorted((s for s in sprints if s.get("state") == "future"), key=lambda s: (s.get("startDate") or "9", s.get("id", 0)))
    closed = [s for _, s in sorted(((s.get("endDate") or s.get("startDate") or "", i), s)
                                   for i, s in enumerate(sprints) if s.get("state") not in ("active", "future"))]
    if active:
        pick = active[0]
    elif future:
        pick = future[0]
    else:
        # Backlog rule: open/unfinished tickets whose only sprints are closed returned to the backlog
        if not only and not _is_issue_done(issue, meta):
            return None, states
        pick = closed[-1]
    return pick, states


def ticket_board_for(issue, meta, only=None, colliding=None, boards_to_areas=False):
    """Resolve the owning board area name for an issue when `boards_to_areas` is enabled, or None."""
    if not boards_to_areas:
        return None
    pick, _ = _pick_current_sprint(issue, meta, only=only, colliding=colliding, boards_to_areas=False)
    if pick is not None:
        sb = sprint_board_for(pick, meta)
        if sb:
            return sb
    return ((meta or {}).get("ticket_boards") or {}).get((issue or {}).get("key"))


def sprint_info(issue, meta, only=None, colliding=None, boards_to_areas=False, ticket_board=None):
    """(the issue's current sprint, {sprint label: state}).

    Jira does not keep the sprint list in date order, so the current sprint is
    the active one, else the next future one, else (for Done tickets or explicit
    only_sprints) the most recent closed one. Under the Backlog Rule, an open
    ticket whose only attached sprints are closed belongs on the Backlog (None)."""
    pick, states = _pick_current_sprint(
        issue, meta, only=only, colliding=colliding, boards_to_areas=boards_to_areas, ticket_board=ticket_board
    )
    if pick is None:
        return None, states
    b = sprint_board_for(pick, meta, ticket_board=ticket_board) if boards_to_areas else None
    return sprint_path_label(pick, colliding=colliding, board=b, boards_to_areas=boards_to_areas), states


def override_node(cfg):
    """mapping.iteration_override as a path below the project root, or None when it is not set.
    A value that starts with the project name is a full path; anything else goes under iteration_parent."""
    m, root = cfg["mapping"], cfg["ado"]["project"]
    ov = (m.get("iteration_override") or "").replace("/", "\\").strip()
    if not ov:
        return None
    if name_key(ov) == name_key(root):
        return ""
    if ov.lower().startswith(root.lower() + "\\"):
        return iteration_node(ov[len(root) + 1:])
    node = iteration_node(ov)
    return f'{iteration_node(m["iteration_parent"])}\\{node}' if m.get("iteration_parent") else node


def current_state(issue, meta, only=None, user_names=None, colliding=None, boards_to_areas=False, ticket_board=None):
    f = issue["fields"]
    points = None
    for fid in meta.get("points_fields", []):  # first filled field wins, in the configured order
        if f.get(fid) is not None:
            points = _num(f[fid])
            break
    return {
        "title": f.get("summary") or "",
        "description": _description_html(issue, user_names),
        "status": (f.get("status") or {}).get("name"),
        "resolution": (f.get("resolution") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("accountId"),
        "labels": list(f.get("labels") or []),
        "points": points,
        "sprint": sprint_info(
            issue, meta, only, colliding=colliding, boards_to_areas=boards_to_areas, ticket_board=ticket_board
        )[0],
        "components": [c["name"] for c in f.get("components") or []],
        "versions": [v["name"] for v in f.get("fixVersions") or []],
    }


def classify(item, cfg, att_by_name=None, user_names=None, sprint_lookup=None):
    """Map one changelog item to (logical field, old value, new value), or None."""
    name = (item.get("field") or "").lower()
    frm, to = item.get("fromString"), item.get("toString")
    m = cfg["mapping"]
    if name == "summary":
        return "title", frm or "", to or ""
    if name == "description":
        return "description", wiki_to_html(frm, att_by_name, user_names), wiki_to_html(to, att_by_name, user_names)
    if name == "status":
        return "status", frm, to
    if name == "resolution":
        return "resolution", frm, to
    if name == "priority":
        return "priority", frm, to
    if name == "assignee":
        return "assignee", item.get("from"), item.get("to")
    if name == "labels":
        return "labels", (frm or "").split(), (to or "").split()
    if name in [n.lower() for n in m["points_jira_names"]]:
        return "points", _num(frm), _num(to)
    if name == m["sprint_jira_name"].lower():
        by_id, by_norm = sprint_lookup or ({}, {})

        def _resolve_last(names_str, ids_str):
            raw_ids = [p.strip() for p in (ids_str or "").split(",") if p.strip()]
            if raw_ids and raw_ids[-1] in by_id:
                return by_id[raw_ids[-1]]
            raw_names = [p.strip() for p in (names_str or "").split(",") if p.strip()]
            if not raw_names:
                return None
            last_name = raw_names[-1]
            return by_norm.get(name_key(last_name), last_name)

        return "sprint", _resolve_last(frm, item.get("from")), _resolve_last(to, item.get("to"))
    if name == "component":
        return "components", frm, to
    if name in ("fix version", "fixversion", "fix versions"):
        return "versions", frm, to
    return None


def map_state(status, wit, cfg, meta, resolution=None, valid_states=None):
    new, ok, _ = closest_ado_state(status, wit, valid_states, cfg, meta, resolution)
    return new, ok


def make_tags(state, key, cfg, jira_type=None):
    m = cfg["mapping"]
    t = m["tags"]
    tags = []
    if jira_type and m["type_tags"].get(jira_type):
        tags.append(m["type_tags"][jira_type])
    if t.get("labels"):
        tags += state["labels"]
    if t.get("components") is not None and t.get("components") is not False:
        tags += [f'{t["components"]}{c}' for c in state["components"]]
    if t.get("versions") is not None and t.get("versions") is not False:
        tags += [f'{t["versions"]}{v}' for v in state["versions"]]
    if t.get("jira_key"):
        tags.append(key)
    clean = []
    for tag in tags:
        tag = str(tag).replace(";", " ").replace(",", " ").strip()
        if tag and tag not in clean:
            clean.append(tag)
    return "; ".join(clean)


def extract_remote_relations(issue, cfg=None):
    """Build ADO Hyperlink relations from exported Jira remote links and Git development links."""
    rels = []
    seen = set()
    skip_zephyr_servlet = bool(((cfg or {}).get("zephyr") or {}).get("enabled"))
    for rl in (issue.get("remotelinks") or issue.get("remote_links") or []):
        obj = rl.get("object") or {}
        url = obj.get("url")
        if not url or url in seen:
            continue
        if skip_zephyr_servlet and "/plugins/servlet/ac/com.thed.zephyr.je/" in url:
            continue
        seen.add(url)
        title = obj.get("title") or rl.get("relationship") or "Jira Remote Link"
        rels.append({
            "rel": "Hyperlink",
            "url": url,
            "attributes": {"comment": f"Jira link: {title}"[:255]},
        })
    for dl in (issue.get("devlinks") or issue.get("dev_links") or []):
        url = dl.get("url")
        if url and url not in seen:
            seen.add(url)
            title = dl.get("title") or dl.get("name") or dl.get("kind") or "Development Link"
            rels.append({
                "rel": "Hyperlink",
                "url": url,
                "attributes": {"comment": title[:255]},
            })
    return rels


class Planner:
    def __init__(self, cfg, meta, users=None, valid_states_by_type=None, available_wits=None, colliding_sprints=None, issues=None):
        self.cfg, self.meta = cfg, meta or {}
        self.users = users or {"ids": {}, "names": {}}
        self.valid_states_by_type = valid_states_by_type if valid_states_by_type is not None else (self.meta.get("valid_states_by_type") or {})
        self.available_wits = available_wits if available_wits is not None else self.meta.get("available_wits")
        if colliding_sprints is not None:
            self.colliding_sprints = colliding_sprints
        else:
            self.colliding_sprints = discover_sprint_collisions(cfg, self.meta, issues=issues)
        self.coalesce_noops = bool(self.cfg["mapping"].get("coalesce_noop_revisions", True)) and not bool(
            self.meta.get("_preserve_uncoalesced_revs")
        )
        self.boards_to_areas = bool(self.cfg["mapping"].get("boards_to_areas"))
        self._by_name_cache = {}
        for fid, fname in (self.meta.get("field_names") or {}).items():
            self._by_name_cache.setdefault(fname.lower(), []).append(fid)
        self._field_ids_cache = {}
        self._state_cache = {}
        self._warn_lock = threading.Lock()
        self.warnings = []

    def warn(self, key, msg):
        w = f"{key}: {msg}"
        with self._warn_lock:
            if w not in self.warnings:
                self.warnings.append(w)

    def _cached_field_ids(self, names):
        cache_key = tuple(names) if isinstance(names, list) else (names,)
        hit = self._field_ids_cache.get(cache_key)
        if hit is not None:
            return hit
        fn_map = self.meta.get("field_names") or {}
        out = []
        for n in cache_key:
            if n.startswith("customfield_") or n in fn_map:
                out.append(n)
            else:
                out += self._by_name_cache.get(n.lower(), [])
        self._field_ids_cache[cache_key] = out
        return out

    def _cached_map_state(self, status, wit, resolution, valid_states):
        ck = (status, wit, resolution)
        hit = self._state_cache.get(ck)
        if hit is not None:
            return hit
        res = map_state(status, wit, self.cfg, self.meta, resolution, valid_states)
        self._state_cache[ck] = res
        return res

    def identity(self, key, acc, what):
        if not acc:
            return ""
        ident = self.users["ids"].get(acc)
        if not ident:
            self.warn(key, f"{what} {self.users['names'].get(acc, acc)} is not in the user map, left empty")
        return ident or ""

    def iteration(self, sprint, sprint_states, board=None):
        m, root = self.cfg["mapping"], self.cfg["ado"]["project"]
        node = override_node(self.cfg)
        if node is not None:
            return f"{root}\\{node}" if node else root
        parent = f'{root}\\{iteration_node(m["iteration_parent"])}' if m.get("iteration_parent") else root
        if not sprint or (m.get("skip_future_sprints") and sprint_states.get(sprint) == "future"):
            if self.boards_to_areas and board:
                return f"{parent}\\{node_name(board)}"
            return parent
        if self.boards_to_areas:
            if "\\" in sprint:
                return f"{parent}\\{iteration_node(sprint)}"
            if board:
                return f"{parent}\\{node_name(board)}\\{node_name(sprint)}"
        return f"{parent}\\{node_name(sprint)}"

    def ado_fields(self, changed, state, key, wit, date, flags, ctx):
        m = self.cfg["mapping"]
        out = {}
        valid_states = self.valid_states_by_type.get(wit)
        if "title" in changed:
            out["System.Title"] = (state["title"] or key)[:255]
        if "description" in changed:
            ref = m["description_field"].get(wit, m["description_field"]["default"])
            out[ref] = state["description"] or ""
        if changed & {"status", "resolution"}:
            new, ok = self._cached_map_state(state["status"], wit, state.get("resolution"), valid_states)
            if not ok:
                self.warn(key, f"status '{state['status']}' has no mapping, used '{new}'")
            out["System.State"] = new
            if new in m["done_states"]:
                if not flags.get("closed"):
                    out["Microsoft.VSTS.Common.ClosedDate"] = ctx.get("closed_date") or date
                flags["closed"] = True
            elif flags.get("closed"):
                out["Microsoft.VSTS.Common.ClosedDate"] = ""
                flags["closed"] = False
        if "priority" in changed and state["priority"] in m["priority"]:
            out["Microsoft.VSTS.Common.Priority"] = m["priority"][state["priority"]]
        if "assignee" in changed:
            out["System.AssignedTo"] = self.identity(key, state["assignee"], "assignee")
        if "points" in changed:
            ref = m["points_field"].get(wit, m["points_field"]["default"])
            if ref:
                out[ref] = state["points"] if state["points"] is not None else ""
        if "sprint" in changed and m["sprints_to_iterations"]:
            out["System.IterationPath"] = self.iteration(state["sprint"], ctx["sprint_states"], board=ctx.get("board"))
        if changed & {"labels", "components", "versions"}:
            out["System.Tags"] = make_tags(state, key, self.cfg, ctx["jira_type"])
        return out

    def custom_fields(self, issue, key, wit):
        """Current values of the configured custom fields, plus a note for extra people."""
        out, extras, names = {}, [], _att_names(issue)
        user_names = self.users.get("names") or {}
        mapped_fids = set()
        for spec in self.cfg["mapping"]["custom_fields"]:
            types = spec.get("types")
            if types and wit not in types:
                continue
            kind = spec.get("kind", "text")
            for fid in self._cached_field_ids(spec["jira"]):
                mapped_fids.add(fid)
                value, more = custom_value(issue, fid, kind, names, user_names)
                if value is None or value == "":
                    continue
                if kind in ("user", "users"):
                    value = self.identity(key, value, spec.get("label") or spec["ado"].split(".")[-1])
                    if not value:
                        continue
                out[spec["ado"]] = value
                if more:
                    extras.append(f'{spec.get("label") or spec["ado"].split(".")[-1]} also: {", ".join(more)}')
                break
        # Time-tracking / Worklog totals (seconds -> hours)
        f = issue["fields"]
        if self.cfg["mapping"].get("include_worklogs", True):
            for jira_f, ado_f in (
                ("timeoriginalestimate", "Microsoft.VSTS.Scheduling.OriginalEstimate"),
                ("timespent", "Microsoft.VSTS.Scheduling.CompletedWork"),
                ("timeestimate", "Microsoft.VSTS.Scheduling.RemainingWork"),
            ):
                sec = f.get(jira_f)
                if isinstance(sec, (int, float)) and sec > 0:
                    out.setdefault(ado_f, round(sec / 3600.0, 2))
        # Board rank preservation
        rank_map = (self.meta or {}).get("rank_order") or {}
        if self.cfg["mapping"].get("preserve_rank", True) and key in rank_map:
            out.setdefault("Microsoft.VSTS.Common.StackRank", float(rank_map[key] * 1000))
        return out, extras

    def plan(self, issue):
        cfg, m = self.cfg, self.cfg["mapping"]
        f, key = issue["fields"], issue["key"]
        user_names = self.users.get("names") or {}
        wit = type_for(issue, cfg, self.available_wits)
        if f["issuetype"]["name"] not in m["types"] and key not in (m.get("ticket_types") or {}):
            self.warn(key, f"issue type '{f['issuetype']['name']}' has no mapping, used '{wit}'")
        t_board = ticket_board_for(
            issue, self.meta, only=m.get("only_sprints"), colliding=self.colliding_sprints,
            boards_to_areas=self.boards_to_areas,
        )
        cur = current_state(
            issue, self.meta, m.get("only_sprints"), user_names, colliding=self.colliding_sprints,
            boards_to_areas=self.boards_to_areas, ticket_board=t_board,
        )
        names = _att_names(issue)
        closed = f.get("resolutiondate") or f.get("updated")
        ctx = {
            "jira_type": f["issuetype"]["name"],
            "board": t_board,
            "sprint_states": sprint_info(
                issue, self.meta, colliding=self.colliding_sprints,
                boards_to_areas=self.boards_to_areas, ticket_board=t_board,
            )[1],
            "closed_date": iso(parse_dt(closed)) if closed and not m["history"] else None,
        }
        hist = sorted(issue.get("changelog", []), key=lambda h: parse_dt(h["created"])) if m["history"] else []
        sp_lookup = _issue_sprint_lookup(
            issue, self.meta, self.colliding_sprints, boards_to_areas=self.boards_to_areas, ticket_board=t_board
        )

        # In history mode, work out the starting values by reading the log backwards.
        init = {k: (list(v) if isinstance(v, list) else v) for k, v in cur.items()}
        seen, last_desc = set(), None
        for hi, h in enumerate(hist):
            for ii, it in enumerate(h.get("items", [])):
                fname_low = (it.get("field") or "").lower()
                if fname_low == "description":
                    last_desc = (hi, ii)
                    if "description" not in seen:
                        seen.add("description")
                        init["description"] = wiki_to_html(it.get("fromString"), names, user_names)
                    continue
                c = classify(it, cfg, names, user_names, sprint_lookup=sp_lookup)
                if not c:
                    continue
                if c[0] in SCALARS and c[0] not in seen:
                    seen.add(c[0])
                    init[c[0]] = c[1]
        for name in SETS:
            vals = list(cur[name])
            for h in reversed(hist):
                for it in reversed(h.get("items", [])):
                    fname_low = (it.get("field") or "").lower()
                    if fname_low not in ("component", "fix version", "fixversion", "fix versions"):
                        continue
                    c = classify(it, cfg, names, user_names, sprint_lookup=sp_lookup)
                    if c and c[0] == name:
                        if c[2] and c[2] in vals:
                            vals.remove(c[2])
                        if c[1] and c[1] not in vals:
                            vals.append(c[1])
            init[name] = vals

        state = {k: (list(v) if isinstance(v, list) else v) for k, v in init.items()}
        flags = {}
        creator = f.get("creator") or f.get("reporter") or {}
        created = parse_dt(f["created"])
        first = {
            "date": iso(created), "author": creator.get("accountId"), "author_name": creator.get("displayName"),
            "fields": self.ado_fields(set(SCALARS) | set(SETS), state, key, wit, iso(created), flags, ctx),
            "comment": None, "attachments": [], "create": True,
        }
        if cfg["ado"].get("area_path"):
            first["fields"]["System.AreaPath"] = cfg["ado"]["area_path"]
        elif self.boards_to_areas and t_board:
            first["fields"]["System.AreaPath"] = f"{cfg['ado']['project']}\\{node_name(t_board)}"
        else:
            first["fields"]["System.AreaPath"] = cfg["ado"]["project"]
        excluded = set()
        for scope in (wit, "default"):  # a type-specific value wins over the default
            for ref, val in (m["create_defaults"].get(scope) or {}).items():
                if val is None:
                    excluded.add(ref)
                elif ref not in excluded:
                    first["fields"].setdefault(ref, val)
        custom, extras = self.custom_fields(issue, key, wit)
        if m.get("note_jira_status"):
            extras.insert(0, f"Jira status: {cur['status']}" + (f" ({cur['resolution']})" if cur.get("resolution") else ""))
        first["fields"].update(custom)
        if extras:
            first["comment"] = "<div><i>From Jira</i><br>" + "<br>".join(esc(x) for x in extras) + "</div>"
        if m.get("jira_key_field"):
            first["fields"][m["jira_key_field"]] = key
        zephyr_steps = issue.get("zephyr_steps") or []
        if wit == "Test Case" and zephyr_steps:
            steps_xml = steps_to_ado_xml(zephyr_steps)
            if steps_xml:
                first["fields"]["Microsoft.VSTS.TCM.Steps"] = steps_xml
                first["zephyr_steps"] = zephyr_steps
            for st in zephyr_steps:
                for satt in st.get("attachments") or []:
                    if satt.get("local_path"):
                        first["attachments"].append({
                            "id": step_attachment_key(st.get("id"), satt.get("id")),
                            "filename": satt["filename"],
                            "local_path": satt["local_path"],
                            "zephyr_step_order": st.get("order"),
                        })
                    else:
                        self.warn(key, f"Zephyr step attachment '{satt.get('filename', '')}' was not downloaded and will be missing")
        remote_rels = extract_remote_relations(issue, cfg=cfg)
        if remote_rels:
            first["relations"] = remote_rels

        events = []  # (datetime, sequence, kind, payload)
        seq = 0
        for hi, h in enumerate(hist):
            events.append((parse_dt(h["created"]), seq, "change", (hi, h))); seq += 1
        for c in issue.get("comments", []):
            events.append((parse_dt(c["created"]), seq, "comment", c)); seq += 1
        if m.get("include_worklogs", True):
            for wl in issue.get("worklogs") or []:
                when_wl = wl.get("started") or wl.get("created") or f["created"]
                events.append((parse_dt(when_wl), seq, "worklog", wl)); seq += 1
        groups = {}
        for a in f.get("attachment") or []:
            if a.get("local_path"):
                groups.setdefault((a["created"], (a.get("author") or {}).get("accountId")), []).append(a)
            else:
                self.warn(key, f"attachment '{a['filename']}' was not downloaded and will be missing")
        for (when, _), atts in groups.items():
            events.append((parse_dt(when), seq, "attach", atts)); seq += 1
        events.sort(key=lambda e: (e[0], e[1]))

        revs = [first]
        applied_ado_fields = dict(first["fields"])
        for when, _, kind, payload in events:
            date = iso(when)
            if kind == "change":
                hi, h = payload
                changed, notes = set(), []
                for ii, it in enumerate(h.get("items", [])):
                    fname_low = (it.get("field") or "").lower()
                    if fname_low == "description":
                        state["description"] = (
                            cur["description"] if (hi, ii) == last_desc
                            else wiki_to_html(it.get("toString"), names, user_names)
                        )
                        changed.add("description")
                        continue
                    c = classify(it, cfg, names, user_names, sprint_lookup=sp_lookup)
                    if c:
                        name, old, new = c
                        if name in SETS:
                            if old and old in state[name]:
                                state[name].remove(old)
                            if new and new not in state[name]:
                                state[name].append(new)
                        else:
                            state[name] = new
                        changed.add(name)
                    elif (it.get("field") or "").lower() == "issuetype":
                        notes.append(f"Issue type changed from {it.get('fromString')} to {it.get('toString')}")
                    elif m["history_notes"] and it.get("field") not in m["history_ignore"]:
                        notes.append(f"{it.get('field')}: {it.get('fromString') or '(empty)'} → {it.get('toString') or '(empty)'}")
                fields = self.ado_fields(changed, state, key, wit, date, flags, ctx)
                if self.coalesce_noops:
                    fields = {fk: fv for fk, fv in fields.items() if applied_ado_fields.get(fk) != fv}
                if not fields and not notes:
                    continue
                applied_ado_fields.update(fields)
                note = "<div><i>Jira history</i><br>" + "<br>".join(esc(n) for n in notes) + "</div>" if notes else None
                a = h.get("author") or {}
                revs.append({"date": date, "author": a.get("accountId"), "author_name": a.get("displayName"),
                             "fields": fields, "comment": note, "attachments": []})
            elif kind == "comment":
                a = payload.get("author") or {}
                revs.append({"date": date, "author": a.get("accountId"), "author_name": a.get("displayName"),
                             "fields": {}, "comment": comment_html(payload, issue, user_names), "attachments": []})
            elif kind == "worklog":
                a = payload.get("author") or {}
                revs.append({"date": date, "author": a.get("accountId"), "author_name": a.get("displayName"),
                             "fields": {}, "comment": worklog_html(payload, issue, user_names), "attachments": []})
            else:
                a = payload[0].get("author") or {}
                revs.append({"date": date, "author": a.get("accountId"), "author_name": a.get("displayName"),
                             "fields": {}, "comment": None,
                             "attachments": [{"id": x["id"], "filename": x["filename"], "local_path": x["local_path"]}
                                             for x in payload]})

        # Safety net: the replayed values must equal what Jira shows today.
        diff = {k for k in cur if (sorted(state[k]) if isinstance(cur[k], list) else state[k])
                != (sorted(cur[k]) if isinstance(cur[k], list) else cur[k])}
        if diff:
            for k in diff:
                state[k] = cur[k]
            date = iso(parse_dt(f.get("updated") or f["created"]))
            # When an open ticket returned to the backlog after a sprint closed (cur["sprint"] is None),
            # Jira keeps the closed sprint on the issue without always adding a changelog entry; don't warn on that.
            warn_diff = diff - ({"sprint"} if cur.get("sprint") is None else set())
            if warn_diff:
                self.warn(key, f"history did not add up for {sorted(warn_diff)}; final values were set from current Jira state")
                note_html = ("<div><i>Migration note: " + ", ".join(sorted(warn_diff)) +
                             " set from the final Jira values because the change log was incomplete.</i></div>")
            else:
                note_html = None
            rec_fields = self.ado_fields(diff, state, key, wit, date, flags, ctx)
            if self.coalesce_noops:
                rec_fields = {fk: fv for fk, fv in rec_fields.items() if applied_ado_fields.get(fk) != fv}
            if rec_fields or note_html:
                applied_ado_fields.update(rec_fields)
                revs.append({"date": date, "author": None, "author_name": None,
                             "fields": rec_fields,
                             "comment": note_html,
                             "attachments": []})

        # Apply per-ticket status override from the interactive validation UI if present
        ticket_state_ov = (m.get("ticket_states") or {}).get(key)
        if ticket_state_ov:
            valid_states = self.valid_states_by_type.get(wit)
            if valid_states and ticket_state_ov not in valid_states:
                ticket_state_ov, _, _ = closest_ado_state(
                    ticket_state_ov, wit, valid_states, cfg, self.meta, cur.get("resolution")
                )
            ado_cats = ((self.meta.get("state_categories_by_type") or {}).get(wit)) or {}
            is_done = ticket_state_ov in m["done_states"] or ado_cats.get(ticket_state_ov) == "Completed"
            target_rev = revs[-1]
            for r in reversed(revs):
                if "System.State" in r["fields"]:
                    target_rev = r
                    break
            target_rev["fields"]["System.State"] = ticket_state_ov
            if is_done:
                if not any(r["fields"].get("Microsoft.VSTS.Common.ClosedDate") for r in revs):
                    target_rev["fields"]["Microsoft.VSTS.Common.ClosedDate"] = ctx.get("closed_date") or target_rev["date"]
            else:
                had_earlier_close = any(r is not target_rev and r["fields"].get("Microsoft.VSTS.Common.ClosedDate") for r in revs)
                if had_earlier_close:
                    target_rev["fields"]["Microsoft.VSTS.Common.ClosedDate"] = ""
                else:
                    target_rev["fields"].pop("Microsoft.VSTS.Common.ClosedDate", None)

        return {
            "key": key,
            "type": wit,
            "subtask": is_subtask_issue(issue),
            "board": t_board,
            "test_steps_count": len(zephyr_steps) if wit == "Test Case" else 0,
            "revisions": revs,
        }


def sprints_in(issue, meta, cfg, colliding=None):
    """All sprints an issue was ever in: {sprint_label: (start, end, state)}."""
    out = {}
    boards_to_areas = bool(((cfg or {}).get("mapping") or {}).get("boards_to_areas"))
    t_board = ticket_board_for(issue, meta, colliding=colliding, boards_to_areas=boards_to_areas)
    by_id, by_norm = _issue_sprint_lookup(
        issue, meta, colliding, boards_to_areas=boards_to_areas, ticket_board=t_board
    )
    for fid in meta.get("sprint_fields", []):
        for s in issue["fields"].get(fid) or []:
            if isinstance(s, dict) and s.get("name"):
                b = sprint_board_for(s, meta, ticket_board=t_board) if boards_to_areas else None
                lbl = sprint_path_label(s, colliding=colliding, board=b, boards_to_areas=boards_to_areas)
                out[lbl] = (s.get("startDate"), s.get("endDate"), s.get("state"))
    if cfg["mapping"]["history"]:
        for h in issue.get("changelog", []):
            for it in h.get("items", []):
                if (it.get("field") or "").lower() == cfg["mapping"]["sprint_jira_name"].lower():
                    for str_side, id_side in (("fromString", "from"), ("toString", "to")):
                        names = [p.strip() for p in (it.get(str_side) or "").split(",") if p.strip()]
                        ids = [p.strip() for p in (it.get(id_side) or "").split(",") if p.strip()]
                        for idx, n in enumerate(names):
                            sid = ids[idx] if idx < len(ids) else None
                            lbl = (by_id.get(sid) if sid else None) or by_norm.get(name_key(n), n)
                            if boards_to_areas and t_board and "\\" not in lbl:
                                lbl = f"{node_name(t_board)}\\{node_name(lbl)}"
                            out.setdefault(lbl, (None, None, None))
    return out
