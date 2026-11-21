"""Turn one exported Jira issue into an ordered list of ADO revisions.

Two modes, set by mapping.history:
- true:  replay every field change, comment, attachment, and worklog in time order,
         starting from the values the issue had when it was created.
- false: create the item with its current values, then add comments, attachments,
         and worklogs in time order with their original authors and dates.

Pure logic: no network calls, so it is testable and can be run as a dry run.
"""
from difflib import SequenceMatcher
import re

from .adf import adf_to_html, esc, plain_to_html
from .util import iso, iteration_node, name_key, node_name, parse_dt
from .wiki import wiki_to_html

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
    if rendered:
        return rendered
    return adf_to_html(issue["fields"].get("description"), _att_names(issue), user_names)


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


def sprint_info(issue, meta, only=None):
    """(the issue's current sprint, {sprint name: state}).

    Jira does not keep the sprint list in date order, so the current sprint is
    the active one, else the next future one, else the most recent closed one.
    With only (sprint names), the choice is limited to those sprints."""
    sprints, states = [], {}
    for fid in meta.get("sprint_fields", []):
        val = issue["fields"].get(fid)
        for s in val if isinstance(val, list) else []:
            if isinstance(s, dict) and s.get("name"):
                states[s["name"]] = s.get("state")
                sprints.append(s)
    if only:
        wanted = {name_key(n) for n in only}
        sprints = [s for s in sprints if name_key(s["name"]) in wanted] or sprints
    if not sprints:
        return None, states
    if not any(s.get("state") for s in sprints):
        return sprints[-1]["name"], states  # no state information: keep Jira's order
    active = [s for s in sprints if s.get("state") == "active"]
    future = sorted((s for s in sprints if s.get("state") == "future"), key=lambda s: (s.get("startDate") or "9", s.get("id", 0)))
    closed = [s for _, s in sorted(((s.get("endDate") or s.get("startDate") or "", i), s)
                                   for i, s in enumerate(sprints) if s.get("state") not in ("active", "future"))]
    pick = active[0] if active else future[0] if future else closed[-1]
    return pick["name"], states


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


def current_state(issue, meta, only=None, user_names=None):
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
        "sprint": sprint_info(issue, meta, only)[0],
        "components": [c["name"] for c in f.get("components") or []],
        "versions": [v["name"] for v in f.get("fixVersions") or []],
    }


def classify(item, cfg, att_by_name=None, user_names=None):
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
        last = lambda s: [p.strip() for p in s.split(",") if p.strip()][-1] if s and s.strip() else None
        return "sprint", last(frm), last(to)
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


def extract_remote_relations(issue):
    """Build ADO Hyperlink relations from exported Jira remote links and Git development links."""
    rels = []
    seen = set()
    for rl in (issue.get("remotelinks") or issue.get("remote_links") or []):
        obj = rl.get("object") or {}
        url = obj.get("url")
        if url and url not in seen:
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
    def __init__(self, cfg, meta, users=None, valid_states_by_type=None, available_wits=None):
        self.cfg, self.meta = cfg, meta or {}
        self.users = users or {"ids": {}, "names": {}}
        self.valid_states_by_type = valid_states_by_type if valid_states_by_type is not None else (self.meta.get("valid_states_by_type") or {})
        self.available_wits = available_wits if available_wits is not None else self.meta.get("available_wits")
        self.warnings = []

    def warn(self, key, msg):
        w = f"{key}: {msg}"
        if w not in self.warnings:
            self.warnings.append(w)

    def identity(self, key, acc, what):
        if not acc:
            return ""
        ident = self.users["ids"].get(acc)
        if not ident:
            self.warn(key, f"{what} {self.users['names'].get(acc, acc)} is not in the user map, left empty")
        return ident or ""

    def iteration(self, sprint, sprint_states):
        m, root = self.cfg["mapping"], self.cfg["ado"]["project"]
        node = override_node(self.cfg)
        if node is not None:
            return f"{root}\\{node}" if node else root
        parent = f'{root}\\{m["iteration_parent"]}' if m.get("iteration_parent") else root
        if not sprint or (m.get("skip_future_sprints") and sprint_states.get(sprint) == "future"):
            return parent
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
            new, ok = map_state(state["status"], wit, self.cfg, self.meta, state.get("resolution"), valid_states)
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
            out["System.IterationPath"] = self.iteration(state["sprint"], ctx["sprint_states"])
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
            for fid in field_ids(self.meta, spec["jira"]):
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
        cur = current_state(issue, self.meta, m.get("only_sprints"), user_names)
        names = _att_names(issue)
        closed = f.get("resolutiondate") or f.get("updated")
        ctx = {"jira_type": f["issuetype"]["name"], "sprint_states": sprint_info(issue, self.meta)[1],
               "closed_date": iso(parse_dt(closed)) if closed and not m["history"] else None}
        hist = sorted(issue.get("changelog", []), key=lambda h: parse_dt(h["created"])) if m["history"] else []

        # In history mode, work out the starting values by reading the log backwards.
        init = {k: (list(v) if isinstance(v, list) else v) for k, v in cur.items()}
        seen, last_desc = set(), None
        for hi, h in enumerate(hist):
            for ii, it in enumerate(h.get("items", [])):
                c = classify(it, cfg, names, user_names)
                if not c:
                    continue
                if c[0] == "description":
                    last_desc = (hi, ii)
                if c[0] in SCALARS and c[0] not in seen:
                    seen.add(c[0])
                    init[c[0]] = c[1]
        for name in SETS:
            vals = list(cur[name])
            for h in reversed(hist):
                for it in reversed(h.get("items", [])):
                    c = classify(it, cfg, names, user_names)
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
        first["fields"]["System.AreaPath"] = cfg["ado"].get("area_path") or cfg["ado"]["project"]
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
        remote_rels = extract_remote_relations(issue)
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
        for when, _, kind, payload in events:
            date = iso(when)
            if kind == "change":
                hi, h = payload
                changed, notes = set(), []
                for ii, it in enumerate(h.get("items", [])):
                    c = classify(it, cfg, names, user_names)
                    if c:
                        name, old, new = c
                        if name in SETS:
                            if old and old in state[name]:
                                state[name].remove(old)
                            if new and new not in state[name]:
                                state[name].append(new)
                        else:
                            if name == "description" and (hi, ii) == last_desc:
                                new = cur["description"]  # the final text keeps its formatting
                            state[name] = new
                        changed.add(name)
                    elif (it.get("field") or "").lower() == "issuetype":
                        notes.append(f"Issue type changed from {it.get('fromString')} to {it.get('toString')}")
                    elif m["history_notes"] and it.get("field") not in m["history_ignore"]:
                        notes.append(f"{it.get('field')}: {it.get('fromString') or '(empty)'} → {it.get('toString') or '(empty)'}")
                fields = self.ado_fields(changed, state, key, wit, date, flags, ctx)
                if not fields and not notes:
                    continue
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
            self.warn(key, f"history did not add up for {sorted(diff)}; final values were set from current Jira state")
            revs.append({"date": date, "author": None, "author_name": None,
                         "fields": self.ado_fields(diff, state, key, wit, date, flags, ctx),
                         "comment": "<div><i>Migration note: " + ", ".join(sorted(diff)) +
                                    " set from the final Jira values because the change log was incomplete.</i></div>",
                         "attachments": []})

        # Apply per-ticket status override from the interactive validation UI if present
        ticket_state_ov = (m.get("ticket_states") or {}).get(key)
        if ticket_state_ov:
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

        return {"key": key, "type": wit, "subtask": is_subtask_issue(issue), "revisions": revs}


def sprints_in(issue, meta, cfg):
    """All sprints an issue was ever in: {name: (start, end, state)}."""
    out = {}
    for fid in meta.get("sprint_fields", []):
        for s in issue["fields"].get(fid) or []:
            if isinstance(s, dict) and s.get("name"):
                out[s["name"]] = (s.get("startDate"), s.get("endDate"), s.get("state"))
    if cfg["mapping"]["history"]:
        for h in issue.get("changelog", []):
            for it in h.get("items", []):
                if (it.get("field") or "").lower() == cfg["mapping"]["sprint_jira_name"].lower():
                    for side in ("fromString", "toString"):
                        for n in (it.get(side) or "").split(","):
                            if n.strip():
                                out.setdefault(n.strip(), (None, None, None))
    return out
