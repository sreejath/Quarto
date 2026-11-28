"""Compare what was exported from Jira with what now exists in Azure DevOps."""
from concurrent.futures import ThreadPoolExecutor
import csv
import os

from .ado_client import AdoClient
from .exporter import load_issues
from .importer import MAX_ATTACHMENT, journal_path, link_target, load_meta
from .planner import Planner
from .util import Journal
from .zephyr import is_test_case_issue, step_count_from_ado_xml


def verify(ado, cfg, users, report_path, log=print, only=None):
    out = cfg["paths"]["export_dir"]
    meta = load_meta(cfg)
    journal = Journal(journal_path(cfg), readonly=True)
    all_issues = list(load_issues(out))
    test_keys = {i["key"] for i in all_issues if is_test_case_issue(i, cfg)}
    planner = Planner(cfg, meta, users, issues=all_issues)
    only_set = set(only) if only else None
    existing_by_key = {}
    if only_set and report_path and os.path.exists(report_path):
        try:
            with open(report_path, newline="", encoding="utf-8") as fh:
                existing_by_key = {r["jira_key"]: r for r in csv.DictReader(fh) if r.get("jira_key")}
        except OSError:
            existing_by_key = {}
    ids_to_fetch = []
    for issue in all_issues:
        key = issue["key"]
        if only_set is not None and key not in only_set:
            continue
        st = journal.items.get(key, {})
        if st.get("id"):
            ids_to_fetch.append(st["id"])

    items_by_id = {}
    batch_fn = getattr(ado, "get_items_batch", None)
    if batch_fn and ids_to_fetch:
        items_by_id = batch_fn(ids_to_fetch) or {}

    comments_by_id = {}
    if isinstance(ado, AdoClient) and len(ids_to_fetch) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(ids_to_fetch))) as pool:
            futs = {iid: pool.submit(ado.comment_count, iid) for iid in ids_to_fetch}
            for iid, fut in futs.items():
                comments_by_id[iid] = fut.result()

    rows, bad, held, before = [], 0, 0, 0
    for issue in all_issues:
        key = issue["key"]
        st = journal.items.get(key, {})
        if only_set is not None and key not in only_set:
            if key in existing_by_key:
                prev = existing_by_key[key]
                prob = (prev.get("problems") or "").strip()
                if prob.startswith("already there before this run"):
                    before += 1
                elif prob.startswith("left out, possible duplicate"):
                    held += 1
                elif prob and prob != "not imported":
                    bad += 1
                rows.append(prev)
            elif not st.get("id") and key in journal.held:
                rows.append({"jira_key": key, "ado_id": "", "problems": f"left out, possible duplicate: {journal.held[key]}"})
                held += 1
            continue
        plan = planner.plan(issue)
        revs = plan["revisions"]
        row = {"jira_key": key, "ado_id": st.get("id", ""), "problems": ""}
        problems = []
        if not st.get("id") and key in journal.held:
            row["problems"] = f"left out, possible duplicate: {journal.held[key]}"
            held += 1
            rows.append(row)
            continue
        if not st.get("id"):
            problems.append("not imported")
        else:
            item = items_by_id.get(st["id"]) if st["id"] in items_by_id else ado.get_item(st["id"])
            fields, rels = item.get("fields", {}), item.get("relations") or []
            final = {}
            for r in revs:
                final.update(r["fields"])
            exp_comments = sum(1 for r in revs if r["comment"])
            exp_atts = 0
            skipped_atts = 0
            for r in revs:
                for a in r["attachments"]:
                    ap = os.path.join(out, a["local_path"]) if a.get("local_path") else ""
                    if ap and os.path.exists(ap) and os.path.getsize(ap) > MAX_ATTACHMENT:
                        skipped_atts += 1
                    else:
                        exp_atts += 1
            exp_links = sum(1 for _, other in link_target(issue, cfg, test_keys=test_keys) if journal.items.get(other, {}).get("id"))
            got_atts = sum(1 for r in rels if r.get("rel") == "AttachedFile")
            got_links = sum(1 for r in rels if r.get("rel", "").startswith(("System.LinkTypes.", "Microsoft.VSTS.Common.TestedBy")))
            got_comments = comments_by_id[st["id"]] if st["id"] in comments_by_id else ado.comment_count(st["id"])
            exp_steps = plan.get("test_steps_count") or 0
            got_steps = step_count_from_ado_xml(fields.get("Microsoft.VSTS.TCM.Steps")) if exp_steps else 0
            row.update(revisions_expected=len(revs), revisions_found=item.get("rev"),
                       comments_expected=exp_comments, comments_found=got_comments,
                       attachments_expected=exp_atts, attachments_found=got_atts,
                       attachments_skipped=skipped_atts,
                       links_expected=exp_links, links_found=got_links,
                       steps_expected=exp_steps, steps_found=got_steps)
            if st.get("revs", 0) < len(revs):
                problems.append("import incomplete")
            if fields.get("System.Title") != final.get("System.Title"):
                problems.append("title differs")
            if fields.get("System.State") != final.get("System.State"):
                problems.append(f"state is {fields.get('System.State')}, expected {final.get('System.State')}")
            if final.get("System.AreaPath") and fields.get("System.AreaPath") != final.get("System.AreaPath"):
                problems.append(f"area is {fields.get('System.AreaPath')}, expected {final.get('System.AreaPath')}")
            if final.get("System.IterationPath") and fields.get("System.IterationPath") != final.get("System.IterationPath"):
                problems.append(f"iteration is {fields.get('System.IterationPath')}, expected {final.get('System.IterationPath')}")
            got_assigned = fields.get("System.AssignedTo")
            if isinstance(got_assigned, dict):
                got_assigned = got_assigned.get("uniqueName") or got_assigned.get("displayName") or ""
            got_assigned = (got_assigned or "").strip().lower()
            exp_assigned = (final.get("System.AssignedTo") or "").strip().lower()
            if exp_assigned and got_assigned != exp_assigned:
                problems.append(f"assignee is {got_assigned or 'unassigned'}, expected {exp_assigned}")
            if (item.get("rev") or 0) < len(revs):
                problems.append("fewer revisions than planned")
            if got_comments < exp_comments:
                problems.append("comments missing")
            if got_atts < exp_atts:
                problems.append("attachments missing")
            if got_links < exp_links:
                problems.append("links missing")
            if exp_steps and got_steps < exp_steps:
                problems.append(f"test steps missing ({got_steps}/{exp_steps})")
        if st.get("found"):  # already there before this run, so differences are notes, not failures
            row["problems"] = "already there before this run" + (f": {'; '.join(problems)}" if problems else "")
            before += 1
            problems = []
        else:
            row["problems"] = "; ".join(problems)
        if problems:
            bad += 1
        rows.append(row)
    cols = ["jira_key", "ado_id", "revisions_expected", "revisions_found", "comments_expected", "comments_found",
            "attachments_expected", "attachments_found", "attachments_skipped", "links_expected", "links_found",
            "steps_expected", "steps_found", "problems"]
    with open(report_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"Verified {len(rows)} issues. {bad} with problems"
        + (f", {before} already there before this run (differences noted, not counted)" if before else "")
        + (f", {held} left out as possible duplicates" if held else "") + f". Report: {report_path}")
    return len(rows), bad
