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


def journal_path(cfg):
    return os.path.join(state_dir(cfg), "import-state.jsonl")


def build_ops(rev, users, att_urls, jira_base, when, issue_url=None):
    ops = []
    add = lambda path, value: ops.append({"op": "add", "path": path, "value": value})
    ident = users["ids"].get(rev["author"]) if rev["author"] else None
    for ref, val in rev["fields"].items():
        if isinstance(val, str) and "<" in val:
            val = rewrite_html(val, att_urls, jira_base)
        add(f"/fields/{ref}", val)
    comment = rev["comment"]
    if comment:
        comment = rewrite_html(comment, att_urls, jira_base)
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
    for issue in load_issues(cfg["paths"]["export_dir"]):
        found = sprints_in(issue, meta, cfg)
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
    found = finder([(p["key"], plan_title(p)) for p in todo], key_ref if has_field else None,
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


def sync_existing_item(ado, item_id, plan, supported, cfg, users, journal, base, planner, log=print):
    """Delta Sync (Golden Quadrant #4): update an existing ADO work item with the latest Jira state,
    new attachments, and a delta sync note."""
    key = plan["key"]
    out = cfg["paths"]["export_dir"]
    revs = plan["revisions"]
    for rev in revs:
        for a in rev["attachments"]:
            if a["id"] in journal.atts:
                continue
            path = os.path.join(out, a["local_path"])
            if os.path.exists(path) and os.path.getsize(path) <= MAX_ATTACHMENT:
                journal.log(t="att", a=a["id"], u=ado.upload_attachment(path, a["filename"]))
    final_fields = {}
    for rev in revs:
        adapted = adapt_to_type(rev, supported, key, plan["type"], cfg, planner)
        final_fields.update(adapted["fields"])
    final_fields.pop("System.AreaPath", None)
    when = datetime.now(timezone.utc)
    sync_rev = {
        "date": iso(when),
        "author": None,
        "author_name": None,
        "fields": final_fields,
        "comment": f"<div><i>Delta synced from Jira issue {esc(key)} on {iso(when)[:10]}.</i></div>",
        "attachments": [],
    }
    ops = build_ops(sync_rev, users, journal.atts, base, when)
    ado.update_item(item_id, ops)
    journal.log(t="rev", k=key, id=item_id, n=len(revs), d=iso(when), found=True)
    log(f"  {key} delta-synced onto existing #{item_id}")


def import_items(ado, cfg, users, only=None, limit=None, log=print, possible_duplicates=None, jira=None):
    """possible_duplicates: 'skip' leaves out tickets that may already be in the target (the default),
    'create' creates them tagged 'Possible duplicate' with a Related link to each match,
    'sync' updates the matched ADO item in place. Per-ticket overrides in cfg['mapping']['ticket_actions']
    take precedence for individual keys."""
    out = cfg["paths"]["export_dir"]
    meta = read_json(os.path.join(out, "meta.json"), {})
    journal = Journal(journal_path(cfg))
    planner = Planner(cfg, meta, users)
    base = meta.get("base_url") or cfg["jira"]["base_url"]
    policy = possible_duplicates or cfg["mapping"].get("possible_duplicates") or "skip"
    existing_policy = cfg["mapping"].get("existing_duplicates") or "skip"
    ticket_actions = cfg["mapping"].get("ticket_actions") or {}
    stats = {"created": 0, "resumed": 0, "complete": 0, "revisions": 0, "failed": [], "existing": {},
             "possible": {}, "held": [], "synced": []}
    type_fields = {}
    try:
        plans = [planner.plan(i) for i in load_issues(out) if not only or i["key"] in only]
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
                elif eff == "sync" and possible[key]:
                    sync_targets[key] = possible[key][0]["id"]
                else:
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
        for issue in load_issues(out):
            key = issue["key"]
            if only and key not in only:
                continue
            if limit and n >= limit:
                break
            if key in held:
                continue
            n += 1
            plan = planner.plan(issue)
            if plan["type"] not in type_fields:
                getter = getattr(ado, "type_fields", None)
                type_fields[plan["type"]] = getter(plan["type"]) if getter else None
            supported = type_fields[plan["type"]]
            if key in sync_targets:
                try:
                    sync_existing_item(ado, sync_targets[key], plan, supported, cfg, users, journal, base, planner, log)
                    stats["synced"].append(key)
                    stats["revisions"] += 1
                except (AdoError, OSError) as e:
                    stats["failed"].append(key)
                    log(f"  {key} SYNC FAILED: {e}")
                continue
            st = journal.items.get(key, {})
            done = st.get("revs", 0)
            if done >= len(plan["revisions"]):
                stats["complete"] += 1
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
                for i in range(done, len(plan["revisions"])):
                    rev = adapt_to_type(plan["revisions"][i], supported, key, plan["type"], cfg, planner)
                    if key in possible and (rev.get("create") or "System.Tags" in rev["fields"]):
                        rev = mark_possible_duplicate(rev, possible[key], ado)
                    when = min(bump(parse_dt(rev["date"]), last), datetime.now(timezone.utc))
                    when = bump(when, last)
                    ops = build_ops(rev, users, journal.atts, base, when, f"{base}/browse/{key}")
                    if item_id is None:
                        item_id = ado.create_item(plan["type"], ops)["id"]
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
                if cfg["mapping"].get("stamp_jira_migrated") and jira and item_id:
                    org = cfg["ado"]["org_url"].rstrip("/")
                    proj = cfg["ado"]["project"]
                    jira.stamp_migrated(key, f"{org}/{proj}/_workitems/edit/{item_id}", item_id)
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


def import_links(ado, cfg, log=print):
    out = cfg["paths"]["export_dir"]
    journal = Journal(journal_path(cfg))
    stats = {"added": 0, "skipped_outside_scope": [], "failed": []}
    # Also resolve external links if the target key already exists in ADO!
    finder = getattr(ado, "find_duplicates", None)
    key_ref = cfg["mapping"].get("jira_key_field")
    external_cache = {}
    try:
        missing_targets = set()
        for issue in load_issues(out):
            key = issue["key"]
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
    this run) and reset the run's journal so the migration can be re-run cleanly."""
    jpath = journal_path(cfg)
    if not os.path.exists(jpath):
        log("Nothing to roll back: no import journal found for this run.")
        return {"deleted": [], "failed": [], "kept_preexisting": []}
    journal = Journal(jpath, readonly=True)
    deleted, failed, kept = [], [], []
    for key, st in sorted(journal.items.items()):
        item_id = st.get("id")
        if not item_id:
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
        log(f"Rollback complete. {len(deleted)} work item(s) moved to ADO Recycle Bin.")
    return {"deleted": deleted, "failed": failed, "kept_preexisting": kept}
