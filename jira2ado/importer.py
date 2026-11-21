"""Phase 2: replay the exported issues into Azure DevOps, plus delta sync and rollback."""
import os
import shutil
from datetime import datetime, timezone

from .adf import esc, rewrite_html
from .ado_client import AdoError
from .duplicates import SAME_KEY, describe
from .exporter import load_issues
from .planner import Planner, override_node, sprint_info, sprints_in
from .util import Journal, bump, iso, name_key, node_name, parse_dt, read_json

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
        if isinstance(val, str) and "<" in val:
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
            add("/relations/-", {"rel": "AttachedFile", "url": att_urls[a["id"]],
                                 "attributes": {"comment": "Imported from Jira", "name": a["filename"]}})
    for rel in rev.get("relations", []):
        add("/relations/-", rel)
    return ops


def create_iterations(ado, cfg, meta, journal, log=print):
    m = cfg["mapping"]
    if not m["sprints_to_iterations"]:
        return
    sub_fn = getattr(ado, "subscribe_team_iteration", None) if cfg["ado"].get("subscribe_team_iterations", True) else None
    team = cfg["ado"].get("team")
    sprints, every = {}, {}
    for sp in (cfg.get("run") or {}).get("sprints") or []:
        if isinstance(sp, dict) and sp.get("name"):
            every.setdefault(sp["name"], (sp.get("start") or sp.get("startDate"), sp.get("end") or sp.get("endDate"), sp.get("state")))
    for issue in load_issues(cfg["paths"]["export_dir"]):
        found = sprints_in(issue, meta, cfg)
        for k, v in found.items():
            if k in every and not v[0] and every[k][0]:
                found[k] = every[k]
        every.update({k: v for k, v in found.items() if k not in every or v[0]})
        if not m["history"]:  # only the sprint each item ends up in is needed
            current = sprint_info(issue, meta, m.get("only_sprints"))[0]
            found = {current: found[current]} if current in found else {}
        for name, info in found.items():
            if name not in sprints or info[0]:
                sprints[name] = info
    node = override_node(cfg)
    if node is not None:  # every item goes to one chosen iteration
        if node and node not in journal.iterations:
            leaf = node.split("\\")[-1]
            start, end, _ = next((v for k, v in every.items() if name_key(k) == name_key(leaf)), (None, None, None))
            made = ado.create_iteration(leaf, iso(parse_dt(start)) if start and end else None,
                                        iso(parse_dt(end)) if start and end else None,
                                        parent="\\".join(node.split("\\")[:-1]) or None)
            if sub_fn:
                sub_fn(node, team=team)
            journal.log(t="iter", n=node)
            log(f"Iteration {'created' if made is not False else 'already there'}: {node}")
        return
    made = 0
    for name, (start, end, state) in sorted(sprints.items()):
        if m.get("skip_future_sprints") and state == "future":
            continue
        node = node_name(name)
        if m.get("iteration_parent"):
            node = f'{m["iteration_parent"]}\\{node}'
        if node in journal.iterations:
            continue
        s = iso(parse_dt(start)) if start else None
        e = iso(parse_dt(end)) if end else None
        leaf = node.split("\\")[-1]
        if ado.create_iteration(leaf, s, e, parent=m.get("iteration_parent")) is not False:
            made += 1
        if sub_fn:
            sub_fn(node, team=team)
        journal.log(t="iter", n=node)
    log(f"Iterations created this run: {made}")


def create_area(ado, cfg, journal, log=print):
    path = cfg["ado"].get("area_path")
    if not (cfg["ado"].get("create_area") and path) or path in journal.areas:
        return
    root = cfg["ado"]["project"]
    rel = path[len(root) + 1:] if path.startswith(root + "\\") else path
    if rel and rel != root:
        ado.create_area(rel)
        log(f"Area ready: {path}")
    journal.log(t="area", n=path)


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


def check_duplicates(ado, cfg, plans, journal):
    """Look in the target project for items that may already hold these tickets. Tickets this
    import log already knows are not checked. Returns (already there {key: id}, possible {key: [match]})."""
    finder = getattr(ado, "find_duplicates", None)
    todo = [p for p in plans if p["key"] not in journal.items]
    if not finder or not todo:
        return {}, {}
    key_ref = cfg["mapping"].get("jira_key_field")
    getter = getattr(ado, "type_fields", None)
    has_field = bool(key_ref and getter and any(key_ref in (getter(t) or set()) for t in {p["type"] for p in todo}))
    # For sub-tasks, use only the Jira ID/Key (never summary title) because sub-tasks across stories share identical names
    entries = [(p["key"], "" if p.get("subtask") else plan_title(p)) for p in todo]
    found = finder(entries, key_ref if has_field else None,
                   cfg["mapping"].get("duplicate_check_titles", True))
    there, possible = {}, {}
    for key, matches in found.items():
        same = [m for m in matches if m["kind"] == SAME_KEY]
        if same:
            there[key] = same[0]["id"]
        else:
            possible[key] = matches
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

    # Map existing attached filenames on the ADO item so syncing from another run folder reuses them
    existing_att_by_name = {}
    for r in live_rels:
        url = r.get("url") or ""
        if not url:
            continue
        if r.get("rel") == "AttachedFile" or "/attachments/" in url:
            attrs = r.get("attributes") or {}
            rname = attrs.get("name") or (parse_qs(urlparse(url).query).get("fileName") or [""])[0]
            if rname:
                existing_att_by_name[rname] = url

    existing_comments = []
    if getattr(ado, "comments", None):
        try:
            existing_comments = list(ado.comments(item_id) or [])
        except Exception:
            existing_comments = []
    existing_norm = {_norm_comment(c) for c in existing_comments if c}

    new_atts = []
    for rev in revs:
        for a in rev["attachments"]:
            if a["id"] not in journal.atts and a["filename"] in existing_att_by_name:
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

    # Save a pre-sync snapshot in the journal so rollback can restore the item
    if key not in journal.synced:
        tracked_keys = (set(final_fields) | set(live_fields)) - {"System.CreatedDate", "System.CreatedBy", "System.ChangedDate", "System.ChangedBy"}
        before_fields = {k: live_fields.get(k) for k in sorted(tracked_keys)}
        journal.log(t="sync", k=key, id=item_id, before_fields=before_fields, before_rel_urls=sorted(before_rel_urls))

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
        if norm_full in existing_norm or (norm_raw and any(norm_raw in ec for ec in existing_norm)):
            continue
        ado.update_item(item_id, c_ops)
        existing_norm.add(norm_full)
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
    planner = Planner(cfg, meta, users, available_wits=available_wits)
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
             "possible": {}, "held": [], "synced": [], "remaining": 0, "paused": False}
    type_fields = {}
    pfile = pause_path(cfg)
    try:
        issues_list = [i for i in load_issues(out) if not only or i["key"] in only]
        plans = [planner.plan(i) for i in issues_list]
        preflight_states(ado, plans)
        planner.warnings.clear()
        existing, possible = check_duplicates(ado, cfg, plans, journal)
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
        create_area(ado, cfg, journal, log)
        create_iterations(ado, cfg, meta, journal, log)
        n = 0
        key_ref = cfg["mapping"].get("jira_key_field")
        for issue in issues_list:
            key = issue["key"]
            if key in held:
                continue
            plan = planner.plan(issue)
            st = journal.items.get(key, {})
            done = st.get("revs", 0)
            if key not in sync_targets and done >= len(plan["revisions"]):
                stats["complete"] += 1
                continue
            if os.path.exists(pfile):
                try:
                    os.remove(pfile)
                except OSError:
                    pass
                stats["paused"] = True
                log("  Migration paused cleanly by user request. Click Resume to continue.")
            if stats["paused"] or (limit and n >= limit):
                stats["remaining"] += 1
                continue
            n += 1
            if plan["type"] not in type_fields:
                getter = getattr(ado, "type_fields", None)
                type_fields[plan["type"]] = getter(plan["type"]) if getter else None
            supported = type_fields[plan["type"]]
            has_key_field = bool(key_ref and supported and key_ref in supported)
            key_to_ado = {k: v["id"] for k, v in journal.items.items() if v.get("id")} if ado_edit_base else None
            if key in sync_targets:
                try:
                    sync_existing_item(ado, sync_targets[key], issue, plan, supported, cfg, users, journal, base, planner,
                                       key_to_ado=key_to_ado, ado_edit_base=ado_edit_base, log=log)
                    stats["synced"].append(key)
                    stats["revisions"] += 1
                except (AdoError, OSError) as e:
                    stats["failed"].append(key)
                    log(f"  {key} SYNC FAILED: {e}")
                continue
            try:
                for rev in plan["revisions"][done:]:
                    for a in rev["attachments"]:
                        if a["id"] in journal.atts:
                            continue
                        path = os.path.join(out, a["local_path"])
                        if os.path.getsize(path) > MAX_ATTACHMENT:
                            planner.warn(key, f"attachment '{a['filename']}' is over 60 MB and was skipped")
                            continue
                        journal.log(t="att", a=a["id"], u=ado.upload_attachment(path, a["filename"]))
                # Inline images can point at any attachment, so all uploads happen before the first write.
                last = parse_dt(st["last"]) if st.get("last") else None
                item_id = st.get("id")
                # Self-healing resume: reconcile ChangedDate against live ADO item if resuming a partial import
                # (Never infer `done` from live["rev"], because link additions or edits increment `rev` in ADO)
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
                for i in range(done, len(plan["revisions"])):
                    rev = adapt_to_type(plan["revisions"][i], supported, key, plan["type"], cfg, planner)
                    if key in possible and (rev.get("create") or "System.Tags" in rev["fields"]):
                        rev = mark_possible_duplicate(rev, possible[key], ado)
                    when = min(bump(parse_dt(rev["date"]), last), datetime.now(timezone.utc))
                    when = bump(when, last)
                    ops = build_ops(rev, users, journal.atts, base, when, f"{base}/browse/{key}",
                                    key_to_ado=key_to_ado, ado_edit_base=ado_edit_base)
                    if item_id is None:
                        item_id = _create_item_idempotent(ado, plan["type"], ops, key, plan_title(plan),
                                                          key_ref if has_key_field else None)
                        stats["created"] += 1
                    else:
                        ado.update_item(item_id, ops)
                    last = when
                    journal.log(t="rev", k=key, id=item_id, n=i + 1, d=iso(when))
                    stats["revisions"] += 1
                if done:
                    stats["resumed"] += 1
                    log(f"  {key} finished on #{item_id}")
                else:
                    log(f"  {key} created as #{item_id}")
            except (AdoError, OSError) as e:
                stats["failed"].append(key)
                log(f"  {key} FAILED: {e}")
                continue
            if n % 25 == 0:
                log(f"  processed {n} issues")
    finally:
        journal.close()
    stats["warnings"] = planner.warnings
    return stats


def link_target(issue, cfg):
    """Yield (relation type, other issue key) for links owned by this issue."""
    f, m, mode = issue["fields"], cfg["mapping"]["links"], cfg["mapping"].get("link_mode", "all")
    if mode == "none":
        return
    if (f.get("parent") or {}).get("key"):
        yield "System.LinkTypes.Hierarchy-Reverse", f["parent"]["key"]
    if mode == "parent":
        return
    for l in f.get("issuelinks") or []:
        if l.get("outwardIssue"):  # each link is stored on both issues; the outward side owns it
            yield m.get((l.get("type") or {}).get("name"), m["default"]), l["outwardIssue"]["key"]


def import_links(ado, cfg, log=print, skip_keys=None):
    out = cfg["paths"]["export_dir"]
    journal = Journal(journal_path(cfg))
    skip_keys = set(skip_keys or ())
    stats = {"added": 0, "skipped_outside_scope": [], "failed": []}
    # Also resolve external links if the target key already exists in ADO!
    finder = getattr(ado, "find_duplicates", None)
    key_ref = cfg["mapping"].get("jira_key_field")
    external_cache = {}
    try:
        missing_targets = set()
        for issue in load_issues(out):
            key = issue["key"]
            if key in skip_keys:
                continue
            st = journal.items.get(key)
            if not st or not st.get("id") or st.get("links"):
                continue
            for _, other in link_target(issue, cfg):
                if not journal.items.get(other, {}).get("id"):
                    missing_targets.add(other)
        if missing_targets and finder and cfg["mapping"].get("include_hierarchy"):
            try:
                found_ext = finder([(k, "") for k in sorted(missing_targets)], key_ref, titles=False)
                for k, matches in found_ext.items():
                    same = [m for m in matches if m["kind"] == SAME_KEY]
                    if same:
                        external_cache[k] = same[0]["id"]
            except Exception:
                pass

        for issue in load_issues(out):
            key = issue["key"]
            if key in skip_keys:
                continue
            st = journal.items.get(key)
            if not st or not st.get("id") or st.get("links"):
                continue
            ok = True
            for rel, other in link_target(issue, cfg):
                target = journal.items.get(other, {}).get("id") or external_cache.get(other)
                if not target:
                    stats["skipped_outside_scope"].append(f"{key} -> {other}")
                    continue
                try:
                    ado.update_item(st["id"], [{"op": "add", "path": "/relations/-",
                                                "value": {"rel": rel, "url": ado.item_url(target)}}])
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
            try:
                live = ado.get_item(item_id) if getattr(ado, "get_item", None) else {"fields": {}, "relations": []}
                cur_fields = live.get("fields") or {}
                live_last = parse_dt(cur_fields.get("System.ChangedDate")) if cur_fields.get("System.ChangedDate") else None
                when = bump(datetime.now(timezone.utc), live_last)
                ops = [{"op": "add", "path": "/fields/System.ChangedDate", "value": iso(when)}]
                for ref, val in b_fields.items():
                    if val is None:
                        if ref in cur_fields:
                            ops.append({"op": "remove", "path": f"/fields/{ref}"})
                    else:
                        ops.append({"op": "add", "path": f"/fields/{ref}", "value": val})
                before_urls = set(sync_info.get("before_rel_urls") or [])
                rels = live.get("relations") or []
                for idx in range(len(rels) - 1, -1, -1):
                    if rels[idx].get("url") and rels[idx]["url"] not in before_urls:
                        ops.append({"op": "remove", "path": f"/relations/{idx}"})
                ado.update_item(item_id, ops)
                restored.append({"key": key, "id": item_id})
                kept.append({"key": key, "id": item_id})
                log(f"  Restored {key} (#{item_id}) to its pre-sync state")
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
