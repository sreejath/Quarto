"""Compare what was exported from Jira with what now exists in Azure DevOps."""
import csv
import os

from .exporter import load_issues
from .importer import journal_path, link_target, load_meta
from .planner import Planner
from .util import Journal


def verify(ado, cfg, users, report_path, log=print):
    out = cfg["paths"]["export_dir"]
    meta = load_meta(cfg)
    journal = Journal(journal_path(cfg), readonly=True)
    planner = Planner(cfg, meta, users)
    rows, bad, held, before = [], 0, 0, 0
    for issue in load_issues(out):
        key = issue["key"]
        plan = planner.plan(issue)
        revs = plan["revisions"]
        st = journal.items.get(key, {})
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
            item = ado.get_item(st["id"])
            fields, rels = item.get("fields", {}), item.get("relations") or []
            final = {}
            for r in revs:
                final.update(r["fields"])
            exp_comments = sum(1 for r in revs if r["comment"])
            exp_atts = sum(len(r["attachments"]) for r in revs)
            exp_links = sum(1 for _, other in link_target(issue, cfg) if journal.items.get(other, {}).get("id"))
            got_atts = sum(1 for r in rels if r.get("rel") == "AttachedFile")
            got_links = sum(1 for r in rels if r.get("rel", "").startswith("System.LinkTypes."))
            got_comments = ado.comment_count(st["id"])
            row.update(revisions_expected=len(revs), revisions_found=item.get("rev"),
                       comments_expected=exp_comments, comments_found=got_comments,
                       attachments_expected=exp_atts, attachments_found=got_atts,
                       links_expected=exp_links, links_found=got_links)
            if st.get("revs", 0) < len(revs):
                problems.append("import incomplete")
            if fields.get("System.Title") != final.get("System.Title"):
                problems.append("title differs")
            if fields.get("System.State") != final.get("System.State"):
                problems.append(f"state is {fields.get('System.State')}, expected {final.get('System.State')}")
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
            "attachments_expected", "attachments_found", "links_expected", "links_found", "problems"]
    with open(report_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    log(f"Verified {len(rows)} issues. {bad} with problems"
        + (f", {before} already there before this run (differences noted, not counted)" if before else "")
        + (f", {held} left out as possible duplicates" if held else "") + f". Report: {report_path}")
    return len(rows), bad
