"""Phase 2: replay the exported issues into Azure DevOps, plus delta sync and rollback."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import html
import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone

from .adf import esc, rewrite_html
from .ado_client import AdoClient, AdoError
from .duplicates import SAME_KEY, describe
from .exporter import load_issues
from .planner import (
    Planner, discover_sprint_collisions, override_node, sprint_board_for,
    sprint_info, sprint_label, sprint_path_label, sprints_in, ticket_board_for,
)
from .util import Journal, bump, iso, iteration_node, name_key, node_name, parse_dt, read_json, write_json
from .zephyr import is_test_case_issue, is_test_link_type, step_attachment_key, steps_to_ado_xml

MAX_ATTACHMENT = 60 * 1024 * 1024  # Azure DevOps Services limit per file


def state_dir(cfg):
    """Folder for what belongs to one target project: import log, plans, reports."""
    return cfg["paths"].get("state_dir") or cfg["paths"]["export_dir"]


def load_meta(cfg):
    """Load Jira export metadata and overlay destination-specific ADO metadata from state_dir."""
    out = cfg["paths"]["export_dir"]
    meta = dict(read_json(os.path.join(out, "meta.json"), {}) or {})
    sdir = state_dir(cfg)
    if sdir:
        ado_meta = read_json(os.path.join(sdir, "ado-meta.json"), {}) or {}
        meta.update(ado_meta)
        jpath = os.path.join(sdir, "import-state.jsonl")
        if os.path.exists(jpath):
            j = Journal(jpath, readonly=True)
            if j.coalesced_noops is False:
                meta["_preserve_uncoalesced_revs"] = True
            elif j.coalesced_noops is None and j.items:
                meta["_preserve_uncoalesced_revs"] = True
            elif j.coalesced_noops is None:
                pvpath = os.path.join(sdir, "preview.json")
                if os.path.exists(pvpath) and not (read_json(pvpath, {}) or {}).get("coalesced_noops"):
                    meta["_preserve_uncoalesced_revs"] = True
    return meta


def journal_path(cfg):
    return os.path.join(state_dir(cfg), "import-state.jsonl")


def pause_path(cfg):
    return os.path.join(state_dir(cfg), ".pause")


def build_ops(rev, users, att_urls, jira_base, when, issue_url=None, key_to_ado=None, ado_edit_base=""):
    ops = []
    add = lambda path, value: ops.append({"op": "add", "path": path, "value": value})
    ident = users["ids"].get(rev["author"]) if rev["author"] else None
    for ref, val in rev["fields"].items():
        if ref == "Microsoft.VSTS.TCM.Steps" and rev.get("zephyr_steps"):
            val = steps_to_ado_xml(rev["zephyr_steps"], att_urls=att_urls) or val
        elif isinstance(val, str) and "<" in val:
            val = rewrite_html(val, att_urls, jira_base, key_to_ado=key_to_ado, ado_edit_base=ado_edit_base)
        add(f"/fields/{ref}", val)
    comment = rev["comment"]
    if comment:
        comment = rewrite_html(comment, att_urls, jira_base, key_to_ado=key_to_ado, ado_edit_base=ado_edit_base)
        if rev["author"] and not ident:
            comment = f"<div><i>Jira author: {esc(rev['author_name'] or rev['author'])}</i></div>" + comment
        add("/fields/System.History", comment)
    add("/fields/System.ChangedDate", iso(when))
    if ident:
        add("/fields/System.ChangedBy", ident)
    if rev.get("create"):
        add("/fields/System.CreatedDate", iso(when))
        if ident:
            add("/fields/System.CreatedBy", ident)
        if issue_url:
            add("/relations/-", {"rel": "Hyperlink", "url": issue_url, "attributes": {"comment": "Original Jira issue"}})
    for a in rev["attachments"]:
        if a["id"] in att_urls:
            cmt = (f"Zephyr step {a['zephyr_step_order']} attachment {a['id']}"
                   if a.get("zephyr_step_order") is not None else f"Jira attachment {a['id']}")
            add("/relations/-", {"rel": "AttachedFile", "url": att_urls[a["id"]],
                                 "attributes": {"comment": cmt, "name": a["filename"]}})
    for rel in rev.get("relations", []):
        add("/relations/-", rel)
    return ops


def _norm_node_path(path):
    return "\\".join(name_key(p) for p in (path or "").replace("/", "\\").split("\\") if p)


def _team_name(board_area):
    return re.sub(r"[;@]+", "-", (board_area or "")).strip(" .")[:255] or "Unnamed"


def create_board_teams(ado, cfg, meta, journal, board_nodes, log=print, planner=None):
    """Auto-create an ADO Team per Jira board (setting its Area Path and Backlog Iteration).
    Returns {name_key(board_node): actual_team_name} for teams ready to receive sprint subscriptions."""
    m = cfg["mapping"]
    if not (m.get("boards_to_areas") and cfg["ado"].get("create_board_teams", True)):
        return {}
    if cfg["ado"].get("area_path") or not board_nodes or not getattr(ado, "create_team", None):
        return {}
    existing_teams = {}
    if getattr(ado, "teams", None):
        try:
            existing_teams = {t["name"].casefold(): t["name"] for t in (ado.teams() or []) if t.get("name")}
        except Exception:
            existing_teams = {}
    orig_by_area = {
        name_key(b["area"]): b["name"]
        for b in (meta or {}).get("boards") or []
        if isinstance(b, dict) and b.get("area")
    }
    root = cfg["ado"]["project"]
    iter_parent = iteration_node(m["iteration_parent"]) if m.get("iteration_parent") else None
    use_backlog_iter = bool(m.get("sprints_to_iterations", True)) and override_node(cfg) is None
    ready = {}
    for b_node in sorted(set(board_nodes)):
        bkey = name_key(b_node)
        tname = _team_name(b_node)
        existing_logged = next((jt for jt in journal.teams if jt.casefold() == tname.casefold()), None)
        if existing_logged:
            ready[bkey] = existing_logged
            continue
        area_full = f"{root}\\{b_node}"
        rel_iter = f"{iter_parent}\\{b_node}" if iter_parent else b_node
        orig_name = orig_by_area.get(bkey, b_node)
        try:
            if tname.casefold() in existing_teams:
                actual_name = existing_teams[tname.casefold()]
                was_created = False
            else:
                res = ado.create_team(tname, description=f"Migrated from Jira board: {orig_name}")
                actual_name = (res or {}).get("name") or tname
                was_created = True
                existing_teams[actual_name.casefold()] = actual_name
            if getattr(ado, "configure_team_area", None):
                ado.configure_team_area(actual_name, area_full, include_children=True, replace=was_created)
            if use_backlog_iter and getattr(ado, "configure_team_backlog_iteration", None):
                ado.configure_team_backlog_iteration(actual_name, rel_iter)
            journal.log(t="team", n=actual_name)
            ready[bkey] = actual_name
            log(f"Team {'created' if was_created else 'ready'}: {actual_name} (Area: {area_full})")
        except Exception as e:
            msg = (f"could not create/configure ADO team '{tname}' ({e}). "
                   "Check that your PAT has 'Project and Team (Read & write)' scope; continuing with Area/Iteration paths.")
            log(f"  Warning: {msg}")
            if planner:
                planner.warn(f"[Team: {tname}]", msg)
            if getattr(e, "status", None) in (401, 403):
                break
    return ready


def create_iterations(ado, cfg, meta, journal, log=print, issues=None, colliding=None, plans=None, planner=None):
    m = cfg["mapping"]
    boards_to_areas = bool(m.get("boards_to_areas"))
    if not m["sprints_to_iterations"]:
        if boards_to_areas and plans:
            board_nodes = {node_name(p["board"]) for p in plans if p.get("board")}
            create_board_teams(ado, cfg, meta, journal, board_nodes, log=log, planner=planner)
        return
    sub_fn = getattr(ado, "subscribe_team_iteration", None) if cfg["ado"].get("subscribe_team_iterations", True) else None
    team = cfg["ado"].get("team")
    if colliding is None:
        colliding = meta.get("_colliding_sprints") if "_colliding_sprints" in meta else discover_sprint_collisions(cfg, meta, issues=issues)

    def _sub(node_path, target_team=None):
        if sub_fn and sub_fn(node_path, team=target_team if target_team is not None else team) is False:
            log(f"  Warning: could not subscribe team sprint board to iteration '{node_path}' (check Project & Team scope)")

    sprints, every = {}, {}
    for sp in list((cfg.get("run") or {}).get("sprints") or []) + list((meta or {}).get("sprints") or []):
        if isinstance(sp, dict) and sp.get("name"):
            sb = sprint_board_for(sp, meta) if boards_to_areas else None
            lbl = sprint_path_label(sp, colliding=colliding, board=sb, boards_to_areas=boards_to_areas)
            every.setdefault(lbl, (sp.get("start") or sp.get("startDate"), sp.get("end") or sp.get("endDate"), sp.get("state")))
    issue_iter = issues if issues is not None else load_issues(cfg["paths"]["export_dir"])
    for issue in issue_iter:
        found = sprints_in(issue, meta, cfg, colliding=colliding)
        for k, v in found.items():
            if k in every and not v[0] and every[k][0]:
                found[k] = every[k]
        every.update({k: v for k, v in found.items() if k not in every or v[0]})
        if not m["history"]:  # only the sprint each item ends up in is needed
            t_board = ticket_board_for(issue, meta, only=m.get("only_sprints"), colliding=colliding, boards_to_areas=boards_to_areas)
            current = sprint_info(
                issue, meta, m.get("only_sprints"), colliding=colliding,
                boards_to_areas=boards_to_areas, ticket_board=t_board,
            )[0]
            found = {current: found[current]} if current in found else {}
        for name, info in found.items():
            if name not in sprints or info[0]:
                sprints[name] = info
    existing_iters = set()
    if getattr(ado, "iteration_paths", None):
        try:
            existing_iters = {_norm_node_path(path) for path in (ado.iteration_paths() or [])}
        except Exception:
            existing_iters = set()
    node = override_node(cfg)
    if node is not None:  # every item goes to one chosen iteration
        if boards_to_areas and plans:
            board_nodes = {node_name(p["board"]) for p in plans if p.get("board")}
            create_board_teams(ado, cfg, meta, journal, board_nodes, log=log, planner=planner)
        if node and node not in journal.iterations:
            norm_node = _norm_node_path(node)
            if norm_node in existing_iters:
                made = False
            else:
                leaf = node.split("\\")[-1]
                start, end, _ = next((v for k, v in every.items() if name_key(k.split("\\")[-1]) == name_key(leaf)), (None, None, None))
                made = ado.create_iteration(leaf, iso(parse_dt(start)) if start and end else None,
                                            iso(parse_dt(end)) if start and end else None,
                                            parent="\\".join(node.split("\\")[:-1]) or None)
            _sub(node)
            journal.log(t="iter", n=node)
            log(f"Iteration {'created' if made is not False else 'already there'}: {node}")
        return

    iter_parent = iteration_node(m["iteration_parent"]) if m.get("iteration_parent") else None
    made = 0
    board_teams = {}
    if boards_to_areas:
        plan_board_nodes = {node_name(p["board"]) for p in (plans or []) if p.get("board")}
        iter_board_nodes = set(plan_board_nodes)
        for sp_name in sprints:
            if "\\" in sp_name:
                iter_board_nodes.add(node_name(sp_name.split("\\", 1)[0]))
        for b_node in sorted(iter_board_nodes):
            rel_b = f"{iter_parent}\\{b_node}" if iter_parent else b_node
            if rel_b not in journal.iterations:
                norm_b = _norm_node_path(rel_b)
                if norm_b not in existing_iters:
                    if ado.create_iteration(b_node, None, None, parent=iter_parent) is not False:
                        made += 1
                    existing_iters.add(norm_b)
                journal.log(t="iter", n=rel_b)
        board_teams = create_board_teams(ado, cfg, meta, journal, plan_board_nodes, log=log, planner=planner)

    for name, (start, end, state) in sorted(sprints.items()):
        if m.get("skip_future_sprints") and state == "future":
            continue
        sp_board = None
        if boards_to_areas and "\\" in name:
            parts = [node_name(p) for p in name.split("\\") if p.strip()]
            sp_board = parts[0] if len(parts) >= 2 else None
            rel_sp = "\\".join(parts)
        else:
            rel_sp = node_name(name)
        node = f"{iter_parent}\\{rel_sp}" if iter_parent else rel_sp
        target_team = board_teams.get(name_key(sp_board)) if (sp_board and board_teams) else team
        if node in journal.iterations:
            continue
        norm_node = _norm_node_path(node)
        if norm_node in existing_iters:
            _sub(node, target_team)
            journal.log(t="iter", n=node)
            continue
        s = iso(parse_dt(start)) if start else None
        e = iso(parse_dt(end)) if end else None
        node_parts = node.split("\\")
        leaf = node_parts[-1]
        parent_path = "\\".join(node_parts[:-1]) or None
        if ado.create_iteration(leaf, s, e, parent=parent_path) is not False:
            made += 1
        existing_iters.add(norm_node)
        _sub(node, target_team)
        journal.log(t="iter", n=node)
    log(f"Iterations created this run: {made}")


def create_area(ado, cfg, journal, log=print, plans=None):
    root = cfg["ado"]["project"]
    existing_areas = None

    def _get_existing():
        nonlocal existing_areas
        if existing_areas is None:
            existing_areas = set()
            if getattr(ado, "area_paths", None):
                try:
                    existing_areas = {_norm_node_path(p) for p in (ado.area_paths() or [])}
                except Exception:
                    existing_areas = set()
        return existing_areas

    path = cfg["ado"].get("area_path")
    if cfg["ado"].get("create_area") and path and path not in journal.areas:
        rel = path[len(root) + 1:] if path.startswith(root + "\\") else path
        if rel and rel != root:
            ex = _get_existing()
            nrel = _norm_node_path(rel)
            if nrel not in ex:
                ado.create_area(rel)
                ex.add(nrel)
            log(f"Area ready: {path}")
        journal.log(t="area", n=path)

    if cfg["mapping"].get("boards_to_areas") and not path and plans:
        needed = sorted({
            rev["fields"]["System.AreaPath"]
            for p in plans
            for rev in p.get("revisions") or []
            if (rev.get("fields") or {}).get("System.AreaPath", "").startswith(root + "\\")
        })
        if needed:
            ex = _get_existing()
            for area_full in needed:
                if area_full in journal.areas:
                    continue
                rel = area_full[len(root) + 1:]
                nrel = _norm_node_path(rel)
                if nrel not in ex:
                    ado.create_area(rel)
                    ex.add(nrel)
                    log(f"Area ready: {area_full}")
                journal.log(t="area", n=area_full)


LABELS = {"Microsoft.VSTS.Scheduling.StoryPoints": "Story Points", "Microsoft.VSTS.Scheduling.Effort": "Effort",
          "Microsoft.VSTS.Common.AcceptanceCriteria": "Acceptance Criteria", "Microsoft.VSTS.Common.Priority": "Priority",
          "Microsoft.VSTS.TCM.ReproSteps": "Repro Steps",
          "Microsoft.VSTS.Scheduling.OriginalEstimate": "Original Estimate (h)",
          "Microsoft.VSTS.Scheduling.CompletedWork": "Completed Work (h)",
          "Microsoft.VSTS.Scheduling.RemainingWork": "Remaining Work (h)"}

SILENT_OPTIONAL_FIELDS = {"Microsoft.VSTS.Common.StackRank"}


def field_label(ref, cfg):
    for spec in cfg["mapping"]["custom_fields"]:
        if spec["ado"] == ref and spec.get("label"):
            return spec["label"]
    return LABELS.get(ref, ref.split(".")[-1])


def adapt_to_type(rev, supported, key, wit, cfg, planner):
    """Leave out fields the target type does not have. On the first revision their
    values are written into a note on the item, so nothing is lost. If the Jira Key
    field is missing on this type, the key is added as a tag instead."""
    if supported is None:
        return rev
    key_ref = cfg["mapping"].get("jira_key_field")
    fields, kept_aside = {}, []
    for ref, val in rev["fields"].items():
        if ref.startswith("System.") or ref in supported:
            fields[ref] = val
        elif ref in SILENT_OPTIONAL_FIELDS:
            continue
        elif ref != key_ref and val not in ("", None):
            planner.warn(f"[{wit}]", f"{ref} is not a field on this type; values go into the 'From Jira' note")
            if rev.get("create"):
                shown = val if isinstance(val, str) and "<" in val else esc(str(val))
                kept_aside.append(f"<b>{esc(field_label(ref, cfg))}:</b> {shown}")
    if key_ref and key_ref not in supported and (rev.get("create") or "System.Tags" in fields):
        tags = fields.get("System.Tags") or ""
        if key not in [t.strip() for t in tags.split(";")]:
            fields["System.Tags"] = f"{tags}; {key}" if tags else key
    comment = rev["comment"]
    if kept_aside:
        block = "<div><i>Jira fields that this Azure DevOps project does not have</i><br>" + "<br>".join(kept_aside) + "</div>"
        comment = (comment or "") + block
    return {**rev, "fields": fields, "comment": comment}


def preflight_states(ado, plans):
    """Stop before writing anything if a mapped state does not exist on the target type."""
    getter = getattr(ado, "states", None)
    if not getter:
        return
    used = {}
    for plan in plans:
        for rev in plan["revisions"]:
            st = rev["fields"].get("System.State")
            if st:
                used.setdefault(plan["type"], set()).add(st)
    bad = []
    for wit, states in used.items():
        valid = set(getter(wit))
        bad += [f"{wit}: '{s}'" for s in sorted(states - valid)]
    if bad:
        raise SystemExit("Stopped before writing anything. These states do not exist in the target project: "
                         + "; ".join(bad) + ". Fix mapping.states in the config.")


def plan_title(plan):
    """The item's final title, which is the Jira summary."""
    title = ""
    for rev in plan["revisions"]:
        title = rev["fields"].get("System.Title", title)
    return title


def check_duplicates(ado, cfg, plans, journal, use_cache=False, save_cache=False):
    """Look in the target project for items that may already hold these tickets. Tickets this
    import log already knows are not checked. Returns (already there {key: id}, possible {key: [match]})."""
    todo = [p for p in plans if p["key"] not in journal.items]
    if not todo:
        return {}, {}
    want_titles = bool(cfg["mapping"].get("duplicate_check_titles", True))
    dup_path = os.path.join(state_dir(cfg), "duplicates.json")
    if use_cache and os.path.exists(dup_path):
        cached = read_json(dup_path, {}) or {}
        if cached.get("checked_online") is True and cached.get("duplicate_check_titles", True) == want_titles:
            todo_keys = {p["key"] for p in todo}
            there = {k: v for k, v in (cached.get("already_there") or {}).items() if k in todo_keys}
            possible = {k: v for k, v in (cached.get("possible") or {}).items() if k in todo_keys}
            return there, possible
    finder = getattr(ado, "find_duplicates", None)
    if not finder:
        return {}, {}
    key_ref = cfg["mapping"].get("jira_key_field")
    getter = getattr(ado, "type_fields", None)
    has_field = bool(key_ref and (not getter or any((getter(t) is None or key_ref in (getter(t) or set())) for t in {p["type"] for p in todo})))
    # For sub-tasks, use only the Jira ID/Key (never summary title) because sub-tasks across stories share identical names
    entries = [(p["key"], "" if p.get("subtask") else plan_title(p)) for p in todo]
    found = finder(entries, key_ref if has_field else None, want_titles)
    there, possible = {}, {}
    for key, matches in found.items():
        same = [m for m in matches if m["kind"] == SAME_KEY]
        if same:
            there[key] = same[0]["id"]
        else:
            possible[key] = matches
    if save_cache:
        write_json(dup_path, {
            "checked_online": True,
            "duplicate_check_titles": want_titles,
            "already_there": there,
            "possible": possible,
        })
    return there, possible


def mark_possible_duplicate(rev, matches, ado):
    """Tag the item 'Possible duplicate' and, on its first revision, link it to each match."""
    fields = dict(rev["fields"])
    tags = [t.strip() for t in (fields.get("System.Tags") or "").split(";") if t.strip()]
    if "Possible duplicate" not in tags:
        fields["System.Tags"] = "; ".join(tags + ["Possible duplicate"])
    out = {**rev, "fields": fields}
    if rev.get("create"):
        out["relations"] = list(rev.get("relations", [])) + [
            {"rel": "System.LinkTypes.Related", "url": ado.item_url(m["id"]),
             "attributes": {"comment": f"Possible duplicate: {m['kind']}"}} for m in matches]
    return out


def _create_item_idempotent(ado, wit, ops, key, title, key_ref):
    """Create a work item in ADO, checking by exact Jira key if a transient server error occurred
    after the item was already created on the server so retries never produce duplicates."""
    finder = getattr(ado, "find_duplicates", None)
    try:
        return ado.create_item(wit, ops)["id"]
    except AdoError:
        if finder:
            try:
                found = finder([(key, title)], key_ref, titles=False)
                same = [m for m in (found.get(key) or []) if m["kind"] == SAME_KEY]
                if same:
                    return same[0]["id"]
            except Exception:
                pass
        raise


def _norm_comment(s):
    return " ".join((s or "").split())


def _norm_comment_text(s):
    """Strip HTML tags and unescape entities so comments reformatted by Azure DevOps still match."""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return " ".join(plain.split()).casefold()


def sync_existing_item(ado, item_id, issue, plan, supported, cfg, users, journal, base, planner,
                       key_to_ado=None, ado_edit_base="", log=print):
    """Delta Sync (for exact Jira key matches only): update an existing ADO work item with the latest
    Jira state, non-blank fields, new attachments, and any missing comments without clearing existing
    ADO assignees or overwriting title-only matches."""
    from urllib.parse import parse_qs, urlparse
    key = plan["key"]
    out = cfg["paths"]["export_dir"]
    revs = plan["revisions"]
    live = ado.get_item(item_id) if getattr(ado, "get_item", None) else {"fields": {}, "relations": []}
    live_fields = dict(live.get("fields") or {})
    live_rels = list(live.get("relations") or [])
    live_rel_urls = {r.get("url") for r in live_rels if r.get("url")}
    before_rel_urls = set(live_rel_urls)

    # Count attachment filenames on the Jira issue so duplicate filenames are only matched by Jira attachment ID
    jira_name_counts = Counter(a["filename"] for rev in revs for a in rev["attachments"])
    existing_att_by_id = {}
    existing_att_by_name = {}
    for r in live_rels:
        url = r.get("url") or ""
        if not url:
            continue
        if r.get("rel") == "AttachedFile" or "/attachments/" in url:
            attrs = r.get("attributes") or {}
            rcmt = (attrs.get("comment") or "").strip()
            if rcmt.startswith("Jira attachment "):
                att_id = rcmt[len("Jira attachment "):].strip()
                if att_id:
                    existing_att_by_id[att_id] = url
                    continue
            rname = attrs.get("name") or (parse_qs(urlparse(url).query).get("fileName") or [""])[0]
            if rname and jira_name_counts.get(rname, 0) == 1:
                existing_att_by_name[rname] = url

    existing_comments = []
    if getattr(ado, "comments", None):
        try:
            existing_comments = list(ado.comments(item_id) or [])
        except Exception:
            existing_comments = []
    existing_norm = {_norm_comment(c) for c in existing_comments if c}
    existing_text_norm = {_norm_comment_text(c) for c in existing_comments if c}

    new_atts = []
    for rev in revs:
        for a in rev["attachments"]:
            if a["id"] not in journal.atts:
                if a["id"] in existing_att_by_id:
                    journal.log(t="att", a=a["id"], u=existing_att_by_id[a["id"]])
                elif a["filename"] in existing_att_by_name:
                    journal.log(t="att", a=a["id"], u=existing_att_by_name[a["filename"]])
            if a["id"] not in journal.atts:
                path = os.path.join(out, a["local_path"])
                if os.path.exists(path) and os.path.getsize(path) <= MAX_ATTACHMENT:
                    journal.log(t="att", a=a["id"], u=ado.upload_attachment(path, a["filename"]))
            if a["id"] in journal.atts and journal.atts[a["id"]] not in live_rel_urls:
                new_atts.append(a)
                live_rel_urls.add(journal.atts[a["id"]])

    final_fields = {}
    for rev in revs:
        adapted = adapt_to_type(rev, supported, key, plan["type"], cfg, planner)
        final_fields.update(adapted["fields"])
    final_fields.pop("System.AreaPath", None)
    # Never wipe an existing ADO assignee simply because the Jira assignee has no ADO mapping
    if final_fields.get("System.AssignedTo") == "" and live_fields.get("System.AssignedTo") and (issue.get("fields") or {}).get("assignee"):
        final_fields.pop("System.AssignedTo", None)
    # Merge tags added in Azure DevOps with the planned Jira tags
    if "System.Tags" in final_fields and live_fields.get("System.Tags"):
        existing_tags = [t.strip() for t in str(live_fields["System.Tags"]).split(";") if t.strip()]
        planned_tags = [t.strip() for t in str(final_fields["System.Tags"]).split(";") if t.strip()]
        merged_tags = list(existing_tags)
        seen_tags = {t.casefold() for t in existing_tags}
        for t in planned_tags:
            if t.casefold() not in seen_tags:
                merged_tags.append(t)
                seen_tags.add(t.casefold())
        final_fields["System.Tags"] = "; ".join(merged_tags)

    # Save a pre-sync snapshot of only the fields actually modified by this sync (never read-only System.* fields)
    # along with after_fields so rollback only reverts fields that were not subsequently edited in ADO.
    readonly_sys = {"System.CreatedDate", "System.CreatedBy", "System.ChangedDate", "System.ChangedBy",
                    "System.Id", "System.Rev", "System.WorkItemType", "System.TeamProject", "System.Watermark"}
    added_rel_urls = sorted({journal.atts[a["id"]] for a in new_atts if a["id"] in journal.atts})
    if key not in journal.synced:
        changed_keys = sorted(k for k in final_fields if k not in readonly_sys and live_fields.get(k) != final_fields[k])
        before_fields = {k: live_fields.get(k) for k in changed_keys}
        after_fields = {k: final_fields[k] for k in changed_keys}
        journal.log(t="sync", k=key, id=item_id, before_fields=before_fields, after_fields=after_fields,
                    before_rel_urls=sorted(before_rel_urls), added_rel_urls=added_rel_urls)
    elif added_rel_urls:
        journal.log(t="sync", k=key, id=item_id, added_rel_urls=added_rel_urls)

    live_last = parse_dt(live_fields.get("System.ChangedDate")) if live_fields.get("System.ChangedDate") else None
    when = bump(datetime.now(timezone.utc), live_last)
    sync_rev = {
        "date": iso(when),
        "author": None,
        "author_name": None,
        "fields": final_fields,
        "comment": f"<div><i>Delta synced from Jira issue {esc(key)} on {iso(when)[:10]}.</i></div>",
        "attachments": new_atts,
    }
    ops = build_ops(sync_rev, users, journal.atts, base, when, key_to_ado=key_to_ado, ado_edit_base=ado_edit_base)
    ado.update_item(item_id, ops)
    last = when

    # Append any Jira comments that are not already present on the ADO item
    comment_revs = [r for r in revs[1:] if r.get("comment")]
    for crev in comment_revs:
        when = bump(datetime.now(timezone.utc), last)
        c_ops = build_ops(
            {"date": iso(when), "author": crev.get("author"), "author_name": crev.get("author_name"),
             "fields": {}, "comment": crev["comment"], "attachments": []},
            users, journal.atts, base, when, key_to_ado=key_to_ado, ado_edit_base=ado_edit_base,
        )
        c_html = next((o["value"] for o in c_ops if o["path"] == "/fields/System.History"), "")
        raw_html = rewrite_html(crev["comment"], journal.atts, base, key_to_ado=key_to_ado, ado_edit_base=ado_edit_base)
        norm_full = _norm_comment(c_html)
        norm_raw = _norm_comment(raw_html)
        text_full = _norm_comment_text(c_html)
        text_raw = _norm_comment_text(raw_html)
        if (norm_full in existing_norm
                or (norm_raw and any(norm_raw in ec for ec in existing_norm))
                or (text_full and text_full in existing_text_norm)
                or (text_raw and any(text_raw in et for et in existing_text_norm))):
            continue
        ado.update_item(item_id, c_ops)
        existing_norm.add(norm_full)
        if text_full:
            existing_text_norm.add(text_full)
        last = when
    journal.log(t="rev", k=key, id=item_id, n=len(revs), d=iso(last), found=True)
    log(f"  {key} delta-synced onto existing #{item_id}")


def import_items(ado, cfg, users, only=None, limit=None, log=print, possible_duplicates=None, jira=None):
    """possible_duplicates: 'skip' leaves out tickets that may already be in the target (the default),
    'create' creates them tagged 'Possible duplicate' with a Related link to each match.
    Per-ticket overrides in cfg['mapping']['ticket_actions'] take precedence for individual keys.
    Note: 'sync' is only permitted on exact Jira key matches (existing), never on title-only matches."""
    from urllib.parse import quote
    out = cfg["paths"]["export_dir"]
    meta = load_meta(cfg)
    journal = Journal(journal_path(cfg))
    available_wits = meta.get("available_wits") or (ado.work_item_types() if getattr(ado, "work_item_types", None) else None)
    all_exported = list(load_issues(out))
    planner = Planner(cfg, meta, users, available_wits=available_wits, issues=all_exported)
    if journal.coalesced_noops is None:
        journal.log(t="mode", coalesced_noops=bool(planner.coalesce_noops))
    base = meta.get("base_url") or cfg["jira"]["base_url"]
    org_url = (cfg["ado"].get("org_url") or "").rstrip("/")
    proj_name = cfg["ado"].get("project") or ""
    ado_edit_base = f"{org_url}/{quote(proj_name)}/_workitems/edit/" if (org_url and proj_name and cfg["mapping"].get("rewrite_cross_references", True)) else ""
    policy = possible_duplicates or cfg["mapping"].get("possible_duplicates") or "skip"
    if policy == "sync":
        policy = "skip"  # never bulk-sync title-only possible duplicates
    existing_policy = cfg["mapping"].get("existing_duplicates") or "skip"
    ticket_actions = cfg["mapping"].get("ticket_actions") or {}
    stats = {"created": 0, "resumed": 0, "complete": 0, "revisions": 0, "failed": [], "existing": {},
             "possible": {}, "held": [], "synced": [], "batch_keys": [], "remaining": 0, "paused": False}
    type_fields = {}
    pfile = pause_path(cfg)
    try:
        issues_list = [i for i in all_exported if not only or i["key"] in only]
        plans = [planner.plan(i) for i in issues_list]
        plans_by_key = {p["key"]: p for p in plans}
        preflight_states(ado, plans)
        planner.warnings.clear()
        existing, possible = check_duplicates(
            ado, cfg, plans, journal, use_cache=True, save_cache=bool(limit)
        )
        force_duplicate = {}
        sync_targets = {}
        for p in plans:
            key = p["key"]
            act = ticket_actions.get(key)
            if key in existing:
                eff = act or existing_policy
                if eff in ("create", "duplicate"):
                    old_id = existing.pop(key)
                    force_duplicate[key] = [{"id": old_id, "kind": SAME_KEY}]
                    possible[key] = force_duplicate[key]
                    log(f"  {key} is already in Azure DevOps as #{old_id}; creating duplicate entry per user selection")
                elif eff == "sync":
                    sync_targets[key] = existing.pop(key)
                else:
                    journal.log(t="rev", k=key, id=existing[key], n=len(p["revisions"]),
                                d=iso(datetime.now(timezone.utc)), found=True)
                    log(f"  {p['key']} is already in Azure DevOps as #{existing[p['key']]}; skipped")
            elif key in possible:
                eff = act or policy
                why = "; ".join(describe(m) for m in possible[key])
                if eff in ("create", "duplicate"):
                    log(f"  {key} may be a duplicate ({why}); creating it tagged 'Possible duplicate'")
                else:
                    # Possible duplicates (title/key-in-title) are never overwritten via sync
                    journal.log(t="held", k=key, why=why)
                    stats["held"].append(key)
                    log(f"  {key} left out, possible duplicate: {why}")
            elif act == "skip":
                journal.log(t="held", k=key, why="skipped by user selection")
                stats["held"].append(key)
                log(f"  {key} skipped by user selection")
        stats["existing"], stats["possible"] = existing, possible
        held = set(stats["held"])
        create_area(ado, cfg, journal, log, plans=plans)
        create_iterations(
            ado, cfg, meta, journal, log, issues=issues_list,
            colliding=planner.colliding_sprints, plans=plans, planner=planner,
        )
        key_ref = cfg["mapping"].get("jira_key_field")
        key_to_ado = {k: v["id"] for k, v in journal.items.items() if v.get("id")} if ado_edit_base else None
        stats_lock = threading.Lock()
        pause_event = threading.Event()
        processed_counter = [0]

        def safe_log(msg):
            with stats_lock:
                log(msg)

        todo_issues = []
        for issue in issues_list:
            key = issue["key"]
            if key in held:
                continue
            plan = plans_by_key[key]
            st = journal.items.get(key, {})
            done = st.get("revs", 0)
            if key not in sync_targets and done >= len(plan["revisions"]):
                stats["complete"] += 1
                continue
            todo_issues.append(issue)

        if limit and limit > 0 and len(todo_issues) > limit:
            stats["remaining"] += len(todo_issues) - limit
            todo_issues = todo_issues[:limit]

        # Pre-populate supported fields per work item type once before running concurrent workers
        getter = getattr(ado, "type_fields", None)
        for wit_name in sorted({plans_by_key[i["key"]]["type"] for i in todo_issues}):
            if wit_name not in type_fields:
                type_fields[wit_name] = getter(wit_name) if getter else None

        def _process_one(issue):
            key = issue["key"]
            if pause_event.is_set() or os.path.exists(pfile):
                pause_event.set()
                with stats_lock:
                    stats["remaining"] += 1
                return
            plan = plans_by_key[key]
            with journal._lock:
                st = dict(journal.items.get(key, {}))
            done = st.get("revs", 0)
            supported = type_fields.get(plan["type"])
            has_key_field = bool(key_ref and (supported is None or key_ref in supported))
            if key in sync_targets:
                try:
                    with stats_lock:
                        kta_snap = dict(key_to_ado) if key_to_ado is not None else None
                    sync_existing_item(ado, sync_targets[key], issue, plan, supported, cfg, users, journal, base, planner,
                                       key_to_ado=kta_snap, ado_edit_base=ado_edit_base, log=safe_log)
                    with stats_lock:
                        if key_to_ado is not None:
                            key_to_ado[key] = sync_targets[key]
                        stats["synced"].append(key)
                        stats["batch_keys"].append(key)
                        stats["revisions"] += 1
                        processed_counter[0] += 1
                        if processed_counter[0] % 25 == 0:
                            log(f"  processed {processed_counter[0]} issues")
                except (AdoError, OSError) as e:
                    with stats_lock:
                        stats["failed"].append(key)
                        log(f"  {key} SYNC FAILED: {e}")
                return

            try:
                skipped_notes_by_rev = {}
                for rev_idx in range(done, len(plan["revisions"])):
                    rev = plan["revisions"][rev_idx]
                    for a in rev["attachments"]:
                        with journal._lock:
                            already_up = a["id"] in journal.atts
                        if already_up:
                            continue
                        path = os.path.join(out, a["local_path"])
                        fsize = os.path.getsize(path)
                        if fsize > MAX_ATTACHMENT:
                            mb = round(fsize / (1024 * 1024), 1)
                            planner.warn(key, f"skipped (> 60 MB ADO limit): {a['filename']} ({mb} MB)")
                            skipped_notes_by_rev.setdefault(rev_idx, []).append(
                                f"Skipped attachment (&gt; 60 MB Azure DevOps limit): <b>{esc(a['filename'])}</b> ({mb} MB)"
                            )
                            continue
                        uploaded_url = ado.upload_attachment(path, a["filename"])
                        journal.log(t="att", a=a["id"], u=uploaded_url)

                # Inline images can point at any attachment, so all uploads happen before the first write.
                last = parse_dt(st["last"]) if st.get("last") else None
                item_id = st.get("id")
                # Self-healing resume: reconcile ChangedDate against live ADO item if resuming a partial import
                if item_id is not None and done > 0 and getattr(ado, "get_item", None):
                    try:
                        live = ado.get_item(item_id)
                        live_changed = (live.get("fields") or {}).get("System.ChangedDate")
                        if live_changed:
                            live_dt = parse_dt(live_changed)
                            if live_dt and (not last or live_dt > last):
                                last = live_dt
                    except Exception:
                        pass

                with journal._lock:
                    att_urls = dict(journal.atts)
                with stats_lock:
                    kta_snap = dict(key_to_ado) if key_to_ado is not None else None

                for i in range(done, len(plan["revisions"])):
                    rev = adapt_to_type(plan["revisions"][i], supported, key, plan["type"], cfg, planner)
                    if i in skipped_notes_by_rev:
                        skip_html = "<div><i>" + "<br>".join(skipped_notes_by_rev[i]) + "</i></div>"
                        rev = {**rev, "comment": (rev["comment"] or "") + skip_html}
                    if key in possible and (rev.get("create") or "System.Tags" in rev["fields"]):
                        rev = mark_possible_duplicate(rev, possible[key], ado)
                    when = min(bump(parse_dt(rev["date"]), last), datetime.now(timezone.utc))
                    when = bump(when, last)
                    ops = build_ops(rev, users, att_urls, base, when, f"{base}/browse/{key}",
                                    key_to_ado=kta_snap, ado_edit_base=ado_edit_base)
                    if item_id is None:
                        item_id = _create_item_idempotent(ado, plan["type"], ops, key, plan_title(plan),
                                                          key_ref if has_key_field else None)
                        with stats_lock:
                            stats["created"] += 1
                    else:
                        try:
                            ado.update_item(item_id, ops)
                        except AdoError as ue:
                            msg_low = str(ue).lower()
                            if getattr(ado, "get_item", None) and any(
                                tok in msg_low for tok in ("changeddate", "tf400073", "vs402625", "date", "revision")
                            ):
                                live = ado.get_item(item_id)
                                live_changed = (live.get("fields") or {}).get("System.ChangedDate")
                                live_dt = parse_dt(live_changed) if live_changed else None
                                base_dt = max(d for d in (last, live_dt, when) if d is not None)
                                when = min(base_dt + timedelta(milliseconds=20), datetime.now(timezone.utc))
                                if live_dt and when <= live_dt:
                                    when = live_dt + timedelta(milliseconds=20)
                                ops = build_ops(rev, users, att_urls, base, when, f"{base}/browse/{key}",
                                                key_to_ado=kta_snap, ado_edit_base=ado_edit_base)
                                ado.update_item(item_id, ops)
                            else:
                                raise
                    if key_to_ado is not None and item_id is not None:
                        with stats_lock:
                            key_to_ado[key] = item_id
                    last = when
                    journal.log(t="rev", k=key, id=item_id, n=i + 1, d=iso(when))
                    with stats_lock:
                        stats["revisions"] += 1

                with stats_lock:
                    stats["batch_keys"].append(key)
                    processed_counter[0] += 1
                    cur_n = processed_counter[0]
                    if done:
                        stats["resumed"] += 1
                        log(f"  {key} finished on #{item_id}")
                    else:
                        log(f"  {key} created as #{item_id}")
                    if cur_n % 25 == 0:
                        log(f"  processed {cur_n} issues")
            except (AdoError, OSError) as e:
                with stats_lock:
                    stats["failed"].append(key)
                    log(f"  {key} FAILED: {e}")

        cfg_workers = cfg["mapping"].get("import_workers")
        if cfg_workers is not None:
            workers = max(1, int(cfg_workers))
        else:
            workers = 6 if isinstance(ado, AdoClient) else 1
        workers = min(workers, max(1, len(todo_issues)))

        if workers <= 1 or len(todo_issues) <= 1:
            for issue in todo_issues:
                _process_one(issue)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_process_one, issue) for issue in todo_issues]
                for fut in futures:
                    fut.result()

        if pause_event.is_set() or os.path.exists(pfile):
            try:
                if os.path.exists(pfile):
                    os.remove(pfile)
            except OSError:
                pass
            stats["paused"] = True
            log("  Migration paused cleanly after finishing in-flight tickets. Click Resume to continue.")
    finally:
        journal.close()
    stats["warnings"] = planner.warnings
    return stats


def link_target(issue, cfg, test_keys=None):
    """Yield (relation type, other issue key) for links owned by this issue.
    When zephyr.link_as_tested_by is enabled and test_keys is provided, general/test links between a
    Test Case and a non-Test Case are promoted to Microsoft.VSTS.Common.TestedBy-Reverse / -Forward."""
    f, m, mode = issue["fields"], cfg["mapping"]["links"], cfg["mapping"].get("link_mode", "all")
    if mode == "none":
        return
    if (f.get("parent") or {}).get("key"):
        yield "System.LinkTypes.Hierarchy-Reverse", f["parent"]["key"]
    if mode == "parent":
        return
    promote = bool(((cfg or {}).get("zephyr") or {}).get("link_as_tested_by", True)) and test_keys is not None
    me_key = issue.get("key")
    me_is_test = bool(promote and me_key in test_keys)
    for l in f.get("issuelinks") or []:
        if l.get("outwardIssue"):  # each link is stored on both issues; the outward side owns it
            ltype_name = (l.get("type") or {}).get("name")
            other = l["outwardIssue"]["key"]
            if promote and is_test_link_type(ltype_name):
                other_is_test = other in test_keys
                if me_is_test and not other_is_test:
                    yield "Microsoft.VSTS.Common.TestedBy-Reverse", other
                    continue
                if other_is_test and not me_is_test:
                    yield "Microsoft.VSTS.Common.TestedBy-Forward", other
                    continue
            yield m.get(ltype_name, m["default"]), other


def import_links(ado, cfg, log=print, skip_keys=None, force_keys=None):
    out = cfg["paths"]["export_dir"]
    journal = Journal(journal_path(cfg))
    skip_keys = set(skip_keys or ())
    force_keys = set(force_keys or ())
    stats = {"added": 0, "skipped_outside_scope": [], "failed": []}
    # Also resolve external links if the target key already exists in ADO!
    finder = getattr(ado, "find_duplicates", None)
    key_ref = cfg["mapping"].get("jira_key_field")
    external_cache = {}
    all_issues = list(load_issues(out))
    test_keys = {i["key"] for i in all_issues if is_test_case_issue(i, cfg)}
    zephyr_on = bool(((cfg or {}).get("zephyr") or {}).get("enabled"))
    try:
        missing_targets = set()
        for issue in all_issues:
            key = issue["key"]
            if key in skip_keys:
                continue
            st = journal.items.get(key)
            if not st or not st.get("id"):
                continue
            if st.get("links") and key not in force_keys:
                continue
            for _, other in link_target(issue, cfg, test_keys=test_keys):
                if not journal.items.get(other, {}).get("id"):
                    missing_targets.add(other)
        if missing_targets and finder and (
            cfg["mapping"].get("include_hierarchy") or zephyr_on or bool(test_keys)
        ):
            try:
                found_ext = finder([(k, "") for k in sorted(missing_targets)], key_ref, titles=False)
                for k, matches in found_ext.items():
                    same = [m for m in matches if m["kind"] == SAME_KEY]
                    if same:
                        external_cache[k] = same[0]["id"]
            except Exception:
                pass

        for issue in all_issues:
            key = issue["key"]
            if key in skip_keys:
                continue
            st = journal.items.get(key)
            if not st or not st.get("id"):
                continue
            if st.get("links") and key not in force_keys:
                continue
            existing_pairs = set()
            if key in force_keys and getattr(ado, "get_item", None):
                try:
                    live = ado.get_item(st["id"]) or {}
                    existing_pairs = {(r.get("rel"), r.get("url")) for r in (live.get("relations") or [])}
                except Exception:
                    existing_pairs = set()
            ok = True
            for rel, other in link_target(issue, cfg, test_keys=test_keys):
                target = journal.items.get(other, {}).get("id") or external_cache.get(other)
                if not target:
                    stats["skipped_outside_scope"].append(f"{key} -> {other}")
                    continue
                rel_url = ado.item_url(target)
                if (rel, rel_url) in existing_pairs:
                    continue
                try:
                    ado.update_item(st["id"], [{"op": "add", "path": "/relations/-",
                                                "value": {"rel": rel, "url": rel_url}}])
                    if key in journal.synced:
                        journal.log(t="sync", k=key, id=st["id"], added_rel_urls=[rel_url])
                    existing_pairs.add((rel, rel_url))
                    stats["added"] += 1
                except AdoError as e:
                    if "already exists" in str(e) or "RelationAlreadyExists" in str(e):
                        continue
                    ok = False
                    stats["failed"].append(f"{key} -> {other}: {e}")
            if ok:
                journal.log(t="links", k=key)
    finally:
        journal.close()
    log(f"Links added: {stats['added']}")
    return stats


def import_test_plans(ado, cfg, log=print):
    """Migrate exported Zephyr Squad Versions -> Test Cycles -> Folders into Azure Test Plans & Static Suites.
    Requires the ADO PAT 'Test Management (Read & write)' scope and a Basic + Test Plans license;
    degrades cleanly with a clear warning if permission is missing."""
    zcfg = (cfg or {}).get("zephyr") or {}
    stats = {"plans_created": 0, "suites_created": 0, "cases_added": 0, "warnings": []}
    if not (zcfg.get("enabled") and zcfg.get("include_test_plans", True)):
        return stats
    out = cfg["paths"]["export_dir"]
    cyc_path = os.path.join(out, "zephyr-cycles.json")
    data = read_json(cyc_path)
    versions = (data or {}).get("versions") or []
    if not versions:
        return stats
    if not getattr(ado, "create_test_plan", None) or not getattr(ado, "create_static_suite", None):
        return stats

    journal = Journal(journal_path(cfg))
    try:
        # Build Jira key -> ADO work item ID map from journal + ADO exact-key lookup for pre-existing Test Cases
        key_to_id = {k: v["id"] for k, v in journal.items.items() if v.get("id")}
        all_cycle_keys = set()
        for v in versions:
            for c in v.get("cycles") or []:
                all_cycle_keys.update(c.get("direct_keys") or [])
                for fld in c.get("folders") or []:
                    all_cycle_keys.update(fld.get("keys") or [])
        unresolved = sorted(k for k in all_cycle_keys if k not in key_to_id)
        finder = getattr(ado, "find_duplicates", None)
        if unresolved and finder:
            try:
                found = finder([(k, "") for k in unresolved], cfg["mapping"].get("jira_key_field"), titles=False)
                for k, matches in found.items():
                    same = [m for m in matches if m["kind"] == SAME_KEY]
                    if same:
                        key_to_id[k] = same[0]["id"]
            except Exception:
                pass

        try:
            existing_plans = {
                (p.get("name") or "").casefold(): p
                for p in (ado.list_test_plans() or [])
                if isinstance(p, dict) and p.get("name")
            } if getattr(ado, "list_test_plans", None) else {}
        except AdoError as e:
            if e.status in (401, 403):
                msg = (f"Could not access Azure Test Plans (HTTP {e.status}): {e}. "
                       "Add the 'Test Management (Read & write)' scope to your ADO PAT and ensure your account has a "
                       "Basic + Test Plans license. Test Cases, Steps, and Tested By links were still migrated.")
                log(f"  Warning: {msg}")
                stats["warnings"].append(msg)
                return stats
            raise

        project = cfg["ado"]["project"]
        area_default = cfg["ado"].get("area_path") or project
        source_proj = (data or {}).get("project_key") or ((cfg.get("run") or {}).get("from")) or project

        for v in versions:
            cycles = v.get("cycles") or []
            if not cycles:
                continue
            vname = v.get("name") or "Unscheduled"
            plan_name = f"{source_proj} - {vname}" if vname.lower() == "unscheduled" else vname
            plan_key = f"v:{v.get('id', vname)}"
            try:
                if plan_key in journal.test_plans:
                    plan_id = journal.test_plans[plan_key]["id"]
                    root_suite_id = journal.test_plans[plan_key].get("root_suite_id")
                elif plan_name.casefold() in existing_plans:
                    ep = existing_plans[plan_name.casefold()]
                    plan_id = ep["id"]
                    root_suite_id = (ep.get("rootSuite") or {}).get("id")
                    journal.log(t="tplan", k=plan_key, id=plan_id, root_suite_id=root_suite_id, n=plan_name)
                else:
                    created = ado.create_test_plan(
                        plan_name,
                        area_path=area_default,
                        iteration=project,
                        description=v.get("description") or f"Migrated from Zephyr Squad version: {vname}",
                        start_date=iso(parse_dt(v["startDate"])) if v.get("startDate") else None,
                        end_date=iso(parse_dt(v["releaseDate"])) if v.get("releaseDate") else None,
                    )
                    plan_id = created["id"]
                    root_suite_id = (created.get("rootSuite") or {}).get("id")
                    existing_plans[plan_name.casefold()] = created
                    journal.log(t="tplan", k=plan_key, id=plan_id, root_suite_id=root_suite_id, n=plan_name)
                    stats["plans_created"] += 1
                    log(f"Test Plan created: {plan_name} (#{plan_id})")

                existing_suites = {}
                if getattr(ado, "list_test_suites", None):
                    for s in ado.list_test_suites(plan_id) or []:
                        if not isinstance(s, dict):
                            continue
                        pid = (s.get("parentSuite") or {}).get("id")
                        if pid is None and s.get("id") and not root_suite_id:
                            root_suite_id = s["id"]
                        if s.get("name"):
                            existing_suites[(pid, s["name"].casefold())] = s
                if not root_suite_id:
                    continue

                def _ensure_suite(skey, parent_id, sname):
                    if skey in journal.test_suites:
                        return journal.test_suites[skey]["id"]
                    hit = existing_suites.get((parent_id, sname.casefold()))
                    if hit:
                        sid = hit["id"]
                        journal.log(t="tsuite", k=skey, plan_id=plan_id, id=sid, n=sname)
                        return sid
                    made = ado.create_static_suite(plan_id, parent_id, sname)
                    sid = made["id"]
                    existing_suites[(parent_id, sname.casefold())] = made
                    journal.log(t="tsuite", k=skey, plan_id=plan_id, id=sid, n=sname)
                    stats["suites_created"] += 1
                    return sid

                def _assign_keys(sid, jkeys):
                    already = journal.suite_cases.get(str(sid), set())
                    wanted_ids = []
                    for jk in jkeys or []:
                        wid = key_to_id.get(jk)
                        if wid and int(wid) not in already and int(wid) not in wanted_ids:
                            wanted_ids.append(int(wid))
                    if wanted_ids:
                        ado.add_test_cases_to_suite(plan_id, sid, wanted_ids)
                        journal.log(t="tsuite_cases", suite_id=sid, ids=wanted_ids)
                        stats["cases_added"] += len(wanted_ids)

                for c in cycles:
                    ckey = f"c:{plan_id}:{c.get('id', c['name'])}"
                    c_suite_id = _ensure_suite(ckey, root_suite_id, c["name"])
                    _assign_keys(c_suite_id, c.get("direct_keys") or [])
                    for fld in c.get("folders") or []:
                        fkey = f"f:{plan_id}:{c_suite_id}:{fld.get('id', fld['name'])}"
                        f_suite_id = _ensure_suite(fkey, c_suite_id, fld["name"])
                        _assign_keys(f_suite_id, fld.get("keys") or [])
            except AdoError as e:
                if e.status in (401, 403):
                    msg = (f"Azure Test Plans refused write (HTTP {e.status}): {e}. "
                           "Add the 'Test Management (Read & write)' scope to your ADO PAT and ensure your account has "
                           "a Basic + Test Plans license.")
                    log(f"  Warning: {msg}")
                    stats["warnings"].append(msg)
                    break
                raise
    finally:
        journal.close()
    log(f"Azure Test Plans synced: {stats['plans_created']} plan(s), {stats['suites_created']} suite(s), "
        f"{stats['cases_added']} test case assignment(s).")
    return stats


def sync_zephyr_steps(ado, cfg, users, log=print):
    """Backfill Microsoft.VSTS.TCM.Steps, step attachments, Tested By links, and Azure Test Plans
    onto already-migrated ADO Test Cases without creating duplicate work items."""
    out = cfg["paths"]["export_dir"]
    meta = load_meta(cfg)
    journal = Journal(journal_path(cfg))
    all_issues = list(load_issues(out))
    test_issues = [i for i in all_issues if is_test_case_issue(i, cfg)]
    stats = {"updated": 0, "unchanged": 0, "not_in_ado": [], "failed": [], "links_added": 0,
             "plans_created": 0, "suites_created": 0, "cases_added": 0, "warnings": []}
    if not test_issues:
        journal.close()
        log("No Zephyr Test Case issues found in export to sync.")
        return stats
    try:
        key_to_id = {k: v["id"] for k, v in journal.items.items() if v.get("id")}
        unresolved = [i["key"] for i in test_issues if i["key"] not in key_to_id]
        finder = getattr(ado, "find_duplicates", None)
        if unresolved and finder:
            found = finder([(k, "") for k in unresolved], cfg["mapping"].get("jira_key_field"), titles=False)
            for k, matches in found.items():
                same = [m for m in matches if m["kind"] == SAME_KEY]
                if same:
                    key_to_id[k] = same[0]["id"]
                    journal.log(t="rev", k=k, id=same[0]["id"], n=1, d=iso(datetime.now(timezone.utc)), found=True)

        target_ids = [key_to_id[i["key"]] for i in test_issues if i["key"] in key_to_id]
        live_by_id = {}
        if target_ids and getattr(ado, "get_items_batch", None):
            live_by_id = ado.get_items_batch(target_ids) or {}

        synced_keys = []
        for issue in test_issues:
            key = issue["key"]
            item_id = key_to_id.get(key)
            if not item_id:
                stats["not_in_ado"].append(key)
                continue
            synced_keys.append(key)
            steps = issue.get("zephyr_steps") or []
            if not steps:
                stats["unchanged"] += 1
                continue
            try:
                live = live_by_id.get(item_id) if item_id in live_by_id else (
                    ado.get_item(item_id) if getattr(ado, "get_item", None) else {"fields": {}, "relations": []}
                )
                live_fields = live.get("fields") or {}
                live_rel_urls = {r.get("url") for r in (live.get("relations") or []) if r.get("url")}
                new_step_atts = []
                for st in steps:
                    for satt in st.get("attachments") or []:
                        if not satt.get("local_path"):
                            continue
                        akey = step_attachment_key(st.get("id"), satt.get("id"))
                        if akey not in journal.atts:
                            ap = os.path.join(out, satt["local_path"])
                            if os.path.exists(ap) and os.path.getsize(ap) <= MAX_ATTACHMENT:
                                url = ado.upload_attachment(ap, satt["filename"])
                                journal.log(t="att", a=akey, u=url)
                        if akey in journal.atts and journal.atts[akey] not in live_rel_urls:
                            new_step_atts.append({
                                "id": akey, "filename": satt["filename"],
                                "zephyr_step_order": st.get("order"),
                            })
                            live_rel_urls.add(journal.atts[akey])

                desired_xml = steps_to_ado_xml(steps, att_urls=journal.atts)
                if desired_xml and live_fields.get("Microsoft.VSTS.TCM.Steps") == desired_xml and not new_step_atts:
                    stats["unchanged"] += 1
                    continue
                live_last = parse_dt(live_fields.get("System.ChangedDate")) if live_fields.get("System.ChangedDate") else None
                when = bump(datetime.now(timezone.utc), live_last)
                ops = [
                    {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": desired_xml},
                    {"op": "add", "path": "/fields/System.ChangedDate", "value": iso(when)},
                ]
                for a in new_step_atts:
                    ops.append({
                        "op": "add",
                        "path": "/relations/-",
                        "value": {
                            "rel": "AttachedFile",
                            "url": journal.atts[a["id"]],
                            "attributes": {
                                "comment": f"Zephyr step {a['zephyr_step_order']} attachment {a['id']}",
                                "name": a["filename"],
                            },
                        },
                    })
                ado.update_item(item_id, ops)
                stats["updated"] += 1
                log(f"  {key} (#{item_id}): synced {len(steps)} Zephyr step(s)")
            except (AdoError, OSError) as e:
                stats["failed"].append(key)
                log(f"  {key} (#{item_id}) ZEPHYR SYNC FAILED: {e}")
    finally:
        journal.close()

    # Reconcile TestedBy links for all Test Cases (and any Story/Bug that owns outward links to them)
    all_force = set(synced_keys)
    test_key_set = {i["key"] for i in test_issues}
    for iss in all_issues:
        for l in (iss.get("fields") or {}).get("issuelinks") or []:
            if l.get("outwardIssue") and l["outwardIssue"]["key"] in test_key_set:
                all_force.add(iss["key"])
    if all_force:
        lstats = import_links(ado, cfg, log=log, force_keys=all_force)
        stats["links_added"] = lstats["added"]

    pstats = import_test_plans(ado, cfg, log=log)
    stats["plans_created"] = pstats["plans_created"]
    stats["suites_created"] = pstats["suites_created"]
    stats["cases_added"] = pstats["cases_added"]
    stats["warnings"].extend(pstats["warnings"])
    return stats


def rollback_run(ado, cfg, log=print):
    """Golden Quadrant #3: Delete all work items created by this run (never items that existed before
    this run), restore any pre-existing items updated via Delta Sync, and reset the run's journal."""
    jpath = journal_path(cfg)
    if not os.path.exists(jpath):
        log("Nothing to roll back: no import journal found for this run.")
        return {"deleted": [], "failed": [], "kept_preexisting": [], "restored": []}
    journal = Journal(jpath, readonly=True)
    deleted, failed, kept, restored = [], [], [], []
    for key, st in sorted(journal.items.items()):
        item_id = st.get("id")
        if not item_id:
            continue
        if key in journal.synced:
            sync_info = journal.synced[key]
            b_fields = sync_info.get("before_fields") or {}
            a_fields = sync_info.get("after_fields") or {}
            readonly_sys = {"System.CreatedDate", "System.CreatedBy", "System.ChangedDate", "System.ChangedBy",
                            "System.Id", "System.Rev", "System.WorkItemType", "System.TeamProject", "System.Watermark"}
            try:
                live = ado.get_item(item_id) if getattr(ado, "get_item", None) else {"fields": {}, "relations": []}
                cur_fields = live.get("fields") or {}
                live_last = parse_dt(cur_fields.get("System.ChangedDate")) if cur_fields.get("System.ChangedDate") else None
                when = bump(datetime.now(timezone.utc), live_last)
                ops = [{"op": "add", "path": "/fields/System.ChangedDate", "value": iso(when)}]
                for ref, val in b_fields.items():
                    if ref in readonly_sys:
                        continue
                    # Only revert fields that were not subsequently edited in Azure DevOps after the sync
                    if a_fields and ref in a_fields and cur_fields.get(ref) != a_fields[ref]:
                        continue
                    if val is None:
                        if ref in cur_fields:
                            ops.append({"op": "remove", "path": f"/fields/{ref}"})
                    else:
                        ops.append({"op": "add", "path": f"/fields/{ref}", "value": val})
                added_urls = sync_info.get("added_rel_urls")
                rels = live.get("relations") or []
                if added_urls is not None:
                    added_set = set(added_urls)
                    for idx in range(len(rels) - 1, -1, -1):
                        if rels[idx].get("url") and rels[idx]["url"] in added_set:
                            ops.append({"op": "remove", "path": f"/relations/{idx}"})
                else:
                    before_urls = set(sync_info.get("before_rel_urls") or [])
                    for idx in range(len(rels) - 1, -1, -1):
                        if rels[idx].get("url") and rels[idx]["url"] not in before_urls:
                            ops.append({"op": "remove", "path": f"/relations/{idx}"})
                if len(ops) > 1:
                    ado.update_item(item_id, ops)
                restored.append({"key": key, "id": item_id})
                kept.append({"key": key, "id": item_id})
                log(f"  Restored {key} (#{item_id}) to its pre-sync state (note: System.History comments remain in ADO)")
            except AdoError as e:
                failed.append({"key": key, "id": item_id, "error": str(e)})
                log(f"  Failed to restore synced item {key} (#{item_id}): {e}")
            continue
        if st.get("found"):
            kept.append({"key": key, "id": item_id})
            log(f"  {key} (#{item_id}) existed before this run; kept untouched")
            continue
        try:
            ado.delete_item(item_id)
            deleted.append({"key": key, "id": item_id})
            log(f"  Deleted {key} (#{item_id}) from Azure DevOps")
        except AdoError as e:
            if e.status == 404 or "does not exist" in str(e):
                deleted.append({"key": key, "id": item_id})
                log(f"  {key} (#{item_id}) was already deleted")
            else:
                failed.append({"key": key, "id": item_id, "error": str(e)})
                log(f"  Failed to delete {key} (#{item_id}): {e}")
    if not failed:
        backup = jpath + ".rolled-back"
        shutil.move(jpath, backup)
        folder = state_dir(cfg)
        for fname in ("import-result.json", "verify.csv", "link-report.json"):
            fpath = os.path.join(folder, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
        log(f"Rollback complete. {len(deleted)} work item(s) moved to ADO Recycle Bin"
            + (f", {len(restored)} synced item(s) restored." if restored else "."))
    return {"deleted": deleted, "failed": failed, "kept_preexisting": kept, "restored": restored}
