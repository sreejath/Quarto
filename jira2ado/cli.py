import argparse
import copy
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

import yaml

from . import config
from .exporter import export, load_issues, load_users_file, write_users_file
from .planner import Planner, closest_ado_state, map_state, type_for
from .util import Journal, name_key, read_json, slug, write_json


def _jira(cfg):
    from .jira_client import JiraClient
    return JiraClient(cfg["jira"]["base_url"], config.secret("JIRA_EMAIL"), config.secret("JIRA_API_TOKEN"))


def _ado(cfg):
    from .ado_client import AdoClient
    return AdoClient(cfg["ado"]["org_url"], cfg["ado"]["project"], config.secret("ADO_PAT"), cfg["ado"]["api_version"])


def _users(cfg):
    return load_users_file(cfg["paths"]["users_file"]) or {"ids": {}, "names": {}}


def cmd_access(cfg, a):
    """Confirm both tokens work before anything else runs."""
    problems, jira = [], None
    try:
        jira = _jira(cfg)
        print(f"Jira: signed in as {jira.myself()}")
    except Exception as e:
        problems.append(f"Jira did not accept the email and API token. {e}")
        jira = None
    if jira:
        try:
            keys = list(jira.search_keys(cfg["jira"]["jql"]))
            shown = ", ".join(keys[:8]) + (f" and {len(keys) - 8} more" if len(keys) > 8 else "")
            print(f"Jira: {len(keys)} ticket(s) match the query" + (f": {shown}" if keys else ""))
            if not keys:
                problems.append("No Jira tickets match the query, so there is nothing to migrate.")
        except Exception as e:
            problems.append(f"Jira did not accept the query. {e}")
    try:
        who = _ado(cfg).check_access()
        print(f"Azure DevOps: reached project '{cfg['ado']['project']}'" + (f", signed in as {who}" if who else ""))
    except Exception as e:
        problems.append(str(e))
    if problems:
        raise SystemExit("\nStopped. Nothing was exported or written.\n  " + "\n  ".join(problems))


def resolve_sprint(want, sprints, project):
    """Find one Jira sprint by number or name. '&' and '-' count as the same, as do upper and lower case."""
    w = want.strip()
    if w.isdigit():
        hits = [s for s in sprints if str(s["id"]) == w]
    else:
        hits = ([s for s in sprints if s["name"] == w]
                or [s for s in sprints if name_key(s["name"]) == name_key(w)]
                or [s for s in sprints if name_key(w) in name_key(s["name"])])
    if len(hits) == 1:
        return hits[0]
    listed = lambda ss: "; ".join(f"{s['name']} (number {s['id']}, {s.get('state')})" for s in ss)
    if not hits:
        recent = sorted(sprints, key=lambda s: s.get("startDate") or "", reverse=True)[:8]
        raise SystemExit(f"No sprint called '{want}' on the {project} boards."
                         + (f" Recent sprints: {listed(recent)}." if recent else " No sprints were found on its boards."))
    raise SystemExit(f"'{want}' matches more than one sprint: {listed(hits)}. "
                     f"Give the sprint number instead, for example --sprint {hits[0]['id']}")


def cmd_prepare(a):
    """Write a run config from --from, --to and --sprint. Its path is the only thing printed on stdout."""
    from .ado_client import AdoClient, AdoError
    from .jira_client import JiraClient
    say = lambda *x: print(*x, file=sys.stderr)
    raw = config._read(a.base) if os.path.exists(a.base) else copy.deepcopy(config.DEFAULTS)
    if getattr(a, "jira_url", None) or os.environ.get("JIRA_BASE_URL"):
        raw.setdefault("jira", {})["base_url"] = (getattr(a, "jira_url", None) or os.environ["JIRA_BASE_URL"]).rstrip("/")
    if getattr(a, "ado_url", None) or os.environ.get("ADO_ORG_URL"):
        raw.setdefault("ado", {})["org_url"] = (getattr(a, "ado_url", None) or os.environ["ADO_ORG_URL"]).rstrip("/")
    targets = {}
    if os.path.exists(a.targets):
        with open(a.targets, encoding="utf-8") as fh:
            targets = yaml.safe_load(fh) or {}
    org = (raw.get("ado") or {}).get("org_url", "").rstrip("/")
    try:
        project = AdoClient(org, a.target.strip(), config.secret("ADO_PAT")).project_name()
    except AdoError as e:
        raise SystemExit(f"\nStopped: {e}")
    match = next((k for k in targets if name_key(k) == name_key(project)), None)
    if match is None:
        say(f"Note: {a.targets} has no section for '{project}'. Using the mapping in {a.base} with the area and "
            "iteration at the project root. The preview will show any Jira status that has no state there.")
        profile = {"ado": {"area_path": None, "create_area": False}, "mapping": {"iteration_parent": None}}
    else:
        profile = targets[match] or {}
    config._merge(raw, copy.deepcopy(profile))

    source = a.source.strip().upper()
    jira = JiraClient(raw["jira"]["base_url"].rstrip("/"), config.secret("JIRA_EMAIL"), config.secret("JIRA_API_TOKEN"))
    board_id = getattr(a, "board", None)
    try:
        try:
            sprints = jira.project_sprints(source, board_id=board_id) if board_id else jira.project_sprints(source)
        except TypeError:
            sprints = jira.project_sprints(source)
    except RuntimeError as e:
        if " 401 " in str(e) or " 403 " in str(e):
            raise SystemExit(f"\nStopped: Jira did not accept the email and API token. {e}")
        raise SystemExit(f"\nStopped: could not read the boards of Jira project '{source}'. Check the name given to --from. {e}")
    wanted = [p.strip() for v in a.sprints for p in v.split(",") if p.strip()]
    chosen = []
    for w in wanted:
        sp = resolve_sprint(w, sprints, source)
        if sp["id"] not in [c["id"] for c in chosen]:
            chosen.append(sp)

    jql = f'project = "{source}" AND sprint in ({", ".join(str(s["id"]) for s in chosen)})'
    if not a.include_done:
        jql += " AND status != Done"
    jql += " ORDER BY key ASC"
    run_dir = os.path.join("runs", slug(source), "+".join(slug(s["name"]) for s in chosen))
    target_dir = os.path.join(run_dir, "to-" + slug(project))
    raw["jira"]["jql"] = jql
    if board_id:
        raw["jira"]["board_id"] = board_id
    raw.setdefault("ado", {})["project"] = project
    if getattr(a, "area", None):
        raw["ado"]["area_path"] = a.area
    raw.setdefault("paths", {}).update(export_dir=os.path.join(run_dir, "export"), state_dir=target_dir,
                                       users_file="users.csv")
    m = raw.setdefault("mapping", {})
    m.update(only_sprints=[s["name"] for s in chosen], skip_future_sprints=False, iteration_override=a.iteration or None)
    if getattr(a, "include_hierarchy", False):
        m["include_hierarchy"] = True
    if getattr(a, "history", None) in ("true", "false"):
        m["history"] = a.history == "true"
    raw["run"] = {"from": source, "to": project, "board_id": board_id, "include_done": bool(a.include_done),
                  "iteration": a.iteration or None,
                  "sprints": [{"number": s["id"], "name": s["name"], "state": s.get("state"),
                               "goal": s.get("goal", ""),
                               "start": (s.get("startDate") or "")[:10], "end": (s.get("endDate") or "")[:10]}
                              for s in chosen]}

    if not os.path.exists("users.csv") and os.path.exists(os.path.join("sprint28", "users.csv")):
        shutil.copyfile(os.path.join("sprint28", "users.csv"), "users.csv")
        say("Started users.csv from sprint28/users.csv. New people are added to it on every run.")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# Written by jira2ado on {datetime.now():%Y-%m-%d %H:%M}. It is rewritten on every run,\n"
                 f"# so change {a.targets} or {a.base} instead of this file.\n")
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True, width=120)

    for s in chosen:
        dates = f", {s['startDate'][:10]} to {s['endDate'][:10]}" if s.get("startDate") and s.get("endDate") else ""
        say(f"Jira sprint:  {s['name']} (number {s['id']}, {s.get('state')}{dates})")
    say(f"From:         Jira project {source}, " + ("all statuses" if a.include_done else "status not Done"))
    say(f"To:           Azure DevOps project {project}" + (f" (settings from {a.targets})" if match else ""))
    if a.iteration:
        say(f"Iteration:    every ticket goes to {a.iteration}")
    else:
        say("Iteration:    each ticket goes to the iteration named after its sprint")
    say(f"Run folder:   {run_dir}")
    print(path)


def cmd_show(cfg, a):
    """Print one config value, for run.sh. Example: show ado.project"""
    v = cfg
    for part in a.key.split("."):
        v = v.get(part) if isinstance(v, dict) else None
    print("true" if v is True else "false" if v is False else "" if v is None else v)


def _duplicate_report(cfg, plans, journal, ado):
    """Print the duplicate check and save it as duplicates.csv and duplicates.json in the target folder.
    ado None means offline: only tickets in this run that share a summary are checked."""
    import csv
    from .ado_client import AdoError
    from .duplicates import describe, same_summary_groups
    from .importer import check_duplicates, plan_title, state_dir
    titles = {p["key"]: plan_title(p) for p in plans}
    found, possible, failed = {}, {}, None
    if ado is not None:
        try:
            found, possible = check_duplicates(ado, cfg, plans, journal)
        except AdoError as e:
            failed = str(e)
    groups = same_summary_groups(list(titles.items()))
    project = cfg["ado"]["project"]
    print(f"\nDuplicate check" + (f" in {project}:" if ado is not None else " (this run only):"))
    if failed:
        print(f"  Could not search Azure DevOps: {failed}")
    elif ado is not None:
        print(f"  {len(found)} already there with the same Jira key (skipped)"
              + (": " + ", ".join(f"{k} (#{v})" for k, v in sorted(found.items())) if found else ""))
        print(f"  {len(possible)} possible duplicate(s)" + (":" if possible else ""))
        for key, matches in sorted(possible.items()):
            for i, m in enumerate(matches):
                print(f"    {key if i == 0 else '':<10} {describe(m)}: {m['title'][:90]}")
    print(f"  {len(groups)} group(s) of tickets in this run with the same summary (all are imported)"
          + (":" if groups else ""))
    for keys in groups:
        print(f"    {', '.join(keys)}: {titles[keys[0]][:90]}")

    rows = [("already there", k, titles.get(k, ""), "same Jira key", v, "", "", "", "") for k, v in sorted(found.items())]
    for key, matches in sorted(possible.items()):
        rows += [("possible", key, titles.get(key, ""), m["kind"], m["id"], m["title"], m["state"] or "",
                  m["iteration"] or "", " ".join(m["jira_keys"])) for m in matches]
    for keys in groups:
        rows += [("same summary in run", k, titles[k], "same summary as " + ", ".join(x for x in keys if x != k),
                  "", "", "", "", "") for k in keys]
    folder = state_dir(cfg)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "duplicates.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["result", "jira_key", "jira_summary", "match", "ado_id", "ado_title", "ado_state",
                    "ado_iteration", "jira_keys_on_ado_item"])
        w.writerows(rows)
    write_json(os.path.join(folder, "duplicates.json"),
               {"checked_online": ado is not None and not failed, "already_there": found,
                "possible": possible, "same_summary_in_run": groups})
    print(f"  Saved to {path}")
    return found, possible


def cmd_duplicates(cfg, a):
    """Print how many tickets the last preview found as possible duplicates, for run.sh."""
    from .importer import state_dir
    data = read_json(os.path.join(state_dir(cfg), "duplicates.json"), {}) or {}
    print(len(data.get("possible") or {}))


def _below_root(path, project):
    if path == project:
        return ""
    return path[len(project) + 1:] if path.startswith(project + "\\") else path


def cmd_export(cfg, a):
    export(_jira(cfg), cfg, force=a.force)


def cmd_users(cfg, a):
    ado_disc = None
    try:
        if os.environ.get("ADO_PAT") and cfg["ado"].get("org_url") and cfg["ado"].get("project"):
            ado_disc = _ado(cfg).discover_users()
    except Exception:
        ado_disc = None
    total, unmapped = write_users_file(
        cfg["paths"]["export_dir"], cfg["paths"]["users_file"], cfg["mapping"]["identity_domains"], ado_discovered=ado_disc
    )
    print(f"{cfg['paths']['users_file']}: {total} people in this run, {unmapped} without an ADO identity."
          + (" Fill in the ado_identity column for them." if unmapped else ""))


def cmd_check(cfg, a):
    """Preview a run without writing to Azure DevOps. Prints a report and saves the same
    data as preview.json in the target folder, one row per ticket, for the browser view."""
    from .duplicates import describe
    from .exporter import collect_users, load_users_file
    from .importer import journal_path, plan_title, state_dir
    from .planner import field_ids
    out = cfg["paths"]["export_dir"]
    meta = read_json(os.path.join(out, "meta.json"), {})
    users = _users(cfg)
    project = cfg["ado"]["project"]

    valid, ado, available_wits = {}, None, None
    if a.online:
        from .ado_client import AdoError
        ado = _ado(cfg)
        try:
            who = ado.check_access()
        except AdoError as e:
            raise SystemExit(f"\nStopped: {e}")
        print(f"Connected to Azure DevOps project '{project}'" + (f" as {who}" if who else ""))
        if getattr(ado, "work_item_types", None):
            try:
                available_wits = ado.work_item_types()
            except Exception:
                available_wits = None

    raw_issues = list(load_issues(out))
    used_wits = {type_for(issue, cfg, available_wits) for issue in raw_issues}
    if a.online and ado:
        for wit in used_wits:
            try:
                valid[wit] = list(ado.states(wit))
            except Exception:
                valid[wit] = []

    types, pairs, places, plans, issues = Counter(), Counter(), Counter(), [], []
    planner = Planner(cfg, meta, users, valid_states_by_type=valid, available_wits=available_wits)
    for issue in raw_issues:
        wit = type_for(issue, cfg, available_wits)
        types[(issue["fields"]["issuetype"]["name"], wit)] += 1
        names = {issue["fields"]["status"]["name"]}
        for h in issue.get("changelog", []):
            for it in h.get("items", []):
                if (it.get("field") or "").lower() == "status":
                    names.update(x for x in (it.get("fromString"), it.get("toString")) if x)
        for st in names:
            pairs[(wit, st, map_state(st, wit, cfg, meta, valid_states=valid.get(wit)))] += 1
        plan = planner.plan(issue)
        plans.append(plan)
        issues.append(issue)
        where = {}
        for rev in plan["revisions"]:
            where.update({k: v for k, v in rev["fields"].items() if k in ("System.AreaPath", "System.IterationPath")})
        places[(where.get("System.AreaPath", project), where.get("System.IterationPath", project))] += 1

    report = {"project": project, "org_url": cfg["ado"]["org_url"], "jql": cfg["jira"]["jql"],
              "jira_url": cfg["jira"]["base_url"], "area_path": cfg["ado"].get("area_path"),
              "confirm_with_project_name": bool(cfg["ado"].get("confirm_with_project_name")),
              "online": bool(a.online), "issues": len(plans), "run": cfg.get("run"),
              "available_wits": available_wits or sorted(used_wits)}

    print(f"{len(plans)} issues in {out}\n\nIssue types:")
    report["types"] = []
    for (jt, wit), c in sorted(types.items()):
        mapped = jt in cfg["mapping"]["types"]
        report["types"].append({"jira": jt, "ado": wit, "count": c, "mapped": mapped})
        print(f"  {jt:<20} -> {wit:<24} {c}" + ("" if mapped else "   <-- not in mapping.types, default used"))

    print("\nStatuses (ADO type, Jira status -> ADO state):")
    report["statuses"], report["valid_states"], bad_types = [], valid, set()
    for (wit, st, (state, ok)), c in sorted(pairs.items()):
        flag = "" if ok else "   <-- no mapping, add it to mapping.states"
        exists = None if not a.online else state in valid.get(wit, [])
        if exists is False:
            flag += f"   <-- '{state}' is not a state of {wit} in ADO"
            bad_types.add(wit)
        _, _, match_reason = closest_ado_state(st, wit, valid.get(wit), cfg, meta)
        report["statuses"].append({
            "type": wit, "jira": st, "ado": state, "count": c,
            "mapped": ok, "exists": exists, "match_reason": match_reason,
            "valid_states": valid.get(wit, []),
        })
        print(f"  {wit:<24} {st:<24} -> {state:<12} {c}{flag}")
    for wit in sorted(bad_types):
        print(f"  States that {wit} has in {project}: {', '.join(valid[wit])}")

    report["custom_fields"] = []
    if cfg["mapping"]["custom_fields"]:
        print("\nCustom fields (ADO field <- Jira field):")
        for spec in cfg["mapping"]["custom_fields"]:
            ids = field_ids(meta, spec["jira"])
            found = ", ".join(f'{(meta.get("field_names") or {}).get(i, i)} ({i})' for i in ids)
            report["custom_fields"].append({"ado": spec["ado"], "label": spec.get("label") or spec["ado"],
                                            "jira": found or None})
            print(f'  {spec["ado"]:<42} <- {found or "NOT FOUND in Jira"}')

    report["missing_fields"] = {}
    if a.online and ado:
        print("\nFields the target project does not have (their values go into a note on each item):")
        wanted = {sp["ado"] for sp in cfg["mapping"]["custom_fields"]}
        wanted |= {v for v in cfg["mapping"]["points_field"].values() if v}
        if cfg["mapping"].get("jira_key_field"):
            wanted.add(cfg["mapping"]["jira_key_field"])
        for wit in sorted({w for w, _, _ in pairs}):
            missing = sorted(r for r in wanted if r not in ado.type_fields(wit))
            report["missing_fields"][wit] = missing
            print(f"  {wit:<24} {', '.join(missing) or 'none'}")

    print("\nWhere the items will go:")
    report["places"] = []
    for (area, it), c in sorted(places.items()):
        note, exists = "", None
        if a.online and getattr(ado, "node_exists", None):
            rel = _below_root(it, project)
            exists = True if not rel else ado.node_exists("Iterations", rel)
            if not exists:
                note = "   <-- iteration does not exist yet; the import creates it (needs 'Create child nodes')"
            elif rel:
                note = "   (iteration exists)"
        report["places"].append({"area": area, "iteration": it, "count": c, "iteration_exists": exists})
        print(f"  {c} item(s): area {area}, iteration {it}{note}")

    journal = Journal(journal_path(cfg), readonly=True)
    finished = [p["key"] for p in plans if journal.items.get(p["key"], {}).get("revs", 0) >= len(p["revisions"])]
    partial = [p["key"] for p in plans if p["key"] in journal.items and p["key"] not in finished]
    found, possible = _duplicate_report(cfg, plans, journal, ado)
    same_groups = (read_json(os.path.join(state_dir(cfg), "duplicates.json"), {}) or {}).get("same_summary_in_run", [])
    in_run_dup_map = {}
    for g in same_groups:
        for k in g:
            in_run_dup_map[k] = [other for other in g if other != k]

    create = len(plans) - len(finished) - len(partial) - len(found) - len(possible)
    print("\nWhat the import will do:")
    print(f"  create {create} new item(s)")
    if partial:
        print(f"  finish {len(partial)} item(s) an earlier run started: {', '.join(partial)}")
    if finished:
        print(f"  skip {len(finished)} item(s) this folder already imported: {', '.join(finished)}")
    if found:
        print(f"  skip {len(found)} item(s) already in Azure DevOps with the same Jira key")
    if possible:
        print(f"  {len(possible)} possible duplicate(s): left out unless you choose to create them")
    if not a.online:
        print("  (add --online to also look for items already in Azure DevOps)")
    report["summary"] = {"create": create, "finish": len(partial), "done_before": len(finished),
                         "already_there": len(found), "possible": len(possible),
                         "same_summary_groups": len(same_groups)}

    tickets = []
    ticket_states_ov = cfg["mapping"].get("ticket_states") or {}
    ticket_actions_ov = cfg["mapping"].get("ticket_actions") or {}
    for issue, p in zip(issues, plans):
        key, revs = p["key"], p["revisions"]
        f = issue["fields"]
        final = {}
        for r in revs:
            final.update(r["fields"])
        st = journal.items.get(key, {})
        if key in finished:
            outcome = "found" if st.get("found") else "imported"
        elif key in partial:
            outcome = "partial"
        elif key in found:
            outcome = "already_there"
        elif key in possible:
            outcome = "possible"
        else:
            outcome = "create"
        assignee_obj = f.get("assignee") or {}
        assignee_acc = assignee_obj.get("accountId") or ""
        assignee_name = assignee_obj.get("displayName") or (users["names"].get(assignee_acc) if assignee_acc else "") or "Unassigned"
        ado_assignee = final.get("System.AssignedTo") or (users["ids"].get(assignee_acc, "") if assignee_acc else "")
        reporter_obj = f.get("reporter") or f.get("creator") or {}
        jira_status = (f.get("status") or {}).get("name") or ""
        wit = p["type"]
        wit_states = valid.get(wit) or []
        _, _, match_reason = closest_ado_state(jira_status, wit, wit_states, cfg, meta, (f.get("resolution") or {}).get("name"))
        if key in ticket_states_ov:
            match_reason = "override"
        desc_field = cfg["mapping"]["description_field"].get(wit, cfg["mapping"]["description_field"]["default"])
        custom_preview = {
            k: v for k, v in final.items()
            if not k.startswith("System.") and k != desc_field
        }
        default_action = (
            "skip" if outcome in ("already_there", "possible")
            else "create"
        )
        tickets.append({
            "key": key,
            "title": plan_title(p),
            "jira_type": f["issuetype"]["name"],
            "jira_status": jira_status,
            "resolution": (f.get("resolution") or {}).get("name") or "",
            "priority": (f.get("priority") or {}).get("name") or "",
            "assignee": assignee_name,
            "ado_assignee": ado_assignee,
            "reporter": reporter_obj.get("displayName") or "",
            "ado_type": wit,
            "ado_state": final.get("System.State"),
            "state_match_reason": match_reason,
            "valid_states": wit_states,
            "iteration": final.get("System.IterationPath"),
            "area": final.get("System.AreaPath"),
            "revisions": len(revs),
            "comments": len(issue.get("comments") or []),
            "attachments": len(f.get("attachment") or []),
            "worklogs": len(issue.get("worklogs") or []),
            "links": len(f.get("issuelinks") or []) + (1 if (f.get("parent") or {}).get("key") else 0) + len(issue.get("remotelinks") or []) + len(issue.get("devlinks") or []),
            "hierarchy_auto_included": bool(issue.get("hierarchy_auto_included")),
            "outcome": outcome,
            "action": ticket_actions_ov.get(key, default_action),
            "ado_id": st.get("id") or found.get(key),
            "in_run_duplicates": in_run_dup_map.get(key, []),
            "matches": [{**m, "text": describe(m)} for m in possible.get(key, [])],
            "description_html": (final.get(desc_field) or "")[:4000],
            "custom_fields": custom_preview,
            "tags": final.get("System.Tags") or "",
        })
    report["tickets"] = tickets
    report["same_summary"] = same_groups

    people = collect_users(out)
    rows = load_users_file(cfg["paths"]["users_file"], raw=True)
    report["people_without_identity"] = sorted(
        u["name"] or acc for acc, u in people.items() if not (rows.get(acc, {}).get("ado_identity") or "").strip())
    report["people_list"] = [
        {
            "account_id": acc,
            "name": u["name"] or rows.get(acc, {}).get("display_name", acc),
            "jira_email": u["email"] or rows.get(acc, {}).get("jira_email", ""),
            "ado_identity": (rows.get(acc, {}).get("ado_identity") or "").strip(),
        }
        for acc, u in sorted(people.items(), key=lambda kv: (kv[1]["name"] or kv[0]).lower())
    ]
    report["mode"] = "full history" if cfg["mapping"]["history"] else "current values, comments and attachments"
    report["links"] = cfg["mapping"].get("link_mode", "all")
    report["warnings"] = list(planner.warnings)
    print(f"\nMode: {report['mode']}; links: {report['links']}")
    print(f"\nWarnings: {len(planner.warnings)}")
    for w in planner.warnings[:50]:
        print("  " + w)
    if len(planner.warnings) > 50:
        print(f"  ... and {len(planner.warnings) - 50} more")
    write_json(os.path.join(state_dir(cfg), "preview.json"), report)


def cmd_plan(cfg, a):
    from .importer import state_dir
    out = cfg["paths"]["export_dir"]
    meta = read_json(os.path.join(out, "meta.json"), {})
    planner = Planner(cfg, meta, _users(cfg))
    n = revs = 0
    for issue in load_issues(out):
        if a.keys and issue["key"] not in a.keys:
            continue
        plan = planner.plan(issue)
        write_json(os.path.join(state_dir(cfg), "plan", f"{issue['key']}.json"), plan)
        n += 1
        revs += len(plan["revisions"])
    print(f"Planned {n} issues, {revs} revisions. Nothing was sent to ADO. See {os.path.join(state_dir(cfg), 'plan')}")


def cmd_import(cfg, a):
    from .importer import import_items, import_links, state_dir
    ado = _ado(cfg)
    jira = None
    if cfg["mapping"].get("stamp_jira_migrated"):
        try:
            jira = _jira(cfg)
        except Exception:
            jira = None
    stats = import_items(ado, cfg, _users(cfg), only=set(a.keys) or None, limit=a.limit,
                         possible_duplicates=a.possible_duplicates, jira=jira)
    tagged = len(stats["possible"]) - len(stats["held"])
    print(f"Created {stats['created']}, resumed {stats['resumed']}, already complete {stats['complete']} "
          f"(found in Azure DevOps by Jira key: {len(stats['existing'])}), "
          f"possible duplicates left out: {len(stats['held'])}"
          + (f", created and tagged 'Possible duplicate': {tagged}" if tagged else "")
          + (f", delta synced: {len(stats.get('synced', []))}" if stats.get("synced") else "")
          + f", revisions written {stats['revisions']}, failed {len(stats['failed'])}, warnings {len(stats['warnings'])}")
    write_json(os.path.join(state_dir(cfg), "import-warnings.json"),
               {"failed": stats["failed"], "warnings": stats["warnings"]})
    write_json(os.path.join(state_dir(cfg), "import-result.json"), {
        "created": stats["created"], "resumed": stats["resumed"], "complete": stats["complete"],
        "revisions": stats["revisions"], "failed": stats["failed"], "warnings": len(stats["warnings"]),
        "already_there": stats["existing"], "held": stats["held"],
        "synced": stats.get("synced", []),
        "tagged": sorted(set(stats["possible"]) - set(stats["held"]) - set(stats.get("synced", [])))})
    if not a.no_links:
        links = import_links(ado, cfg)
        write_json(os.path.join(state_dir(cfg), "link-report.json"), links)
    if stats["failed"]:
        sys.exit(1)


def cmd_links(cfg, a):
    from .importer import import_links, state_dir
    write_json(os.path.join(state_dir(cfg), "link-report.json"), import_links(_ado(cfg), cfg))


def cmd_verify(cfg, a):
    from .verify import verify
    _, bad = verify(_ado(cfg), cfg, _users(cfg), a.report)
    sys.exit(1 if bad else 0)


def cmd_rollback(cfg, a):
    from .importer import rollback_run
    res = rollback_run(_ado(cfg), cfg)
    sys.exit(1 if res["failed"] else 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="jira2ado", description="Migrate Jira Cloud issues to Azure DevOps with full history.")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("export", help="copy issues, history, comments and attachments out of Jira")
    s.add_argument("--force", action="store_true", help="re-export issues already on disk")
    s = sub.add_parser("prepare", help="write a run config from --from, --to and --sprint (used by run.sh and UI)")
    s.add_argument("--from", dest="source", required=True, help="Jira project key, for example PROJ")
    s.add_argument("--to", dest="target", required=True, help="Azure DevOps project name")
    s.add_argument("--board", dest="board", help="Optional Jira board ID")
    s.add_argument("--sprint", dest="sprints", action="append", required=True,
                   help="Jira sprint name or number; repeat for more than one")
    s.add_argument("--iteration", help="put every item in this Azure DevOps iteration")
    s.add_argument("--area", help="put every item in this Azure DevOps area path")
    s.add_argument("--include-done", action="store_true", help="also take tickets whose status is Done")
    s.add_argument("--include-hierarchy", action="store_true", help="auto-include parent Epics and child Sub-tasks")
    s.add_argument("--history", choices=["true", "false"], help="override history mode (true or false)")
    s.add_argument("--jira-url", help="Atlassian Jira base URL")
    s.add_argument("--ado-url", help="Azure DevOps organization URL")
    s.add_argument("--base", default=os.path.join("configs", "enterprise-development.yaml"))
    s.add_argument("--targets", default=os.path.join("configs", "targets.yaml"))
    s = sub.add_parser("show", help="print one config value, for example ado.project")
    s.add_argument("key")
    sub.add_parser("access", help="check that the Jira and Azure DevOps tokens work")
    sub.add_parser("duplicates", help="print how many possible duplicates the last preview found")
    sub.add_parser("users", help="create or update the user mapping file from the export")
    s = sub.add_parser("check", help="report unmapped types, statuses and users before importing")
    s.add_argument("--online", action="store_true", help="also check states against the ADO project")
    s = sub.add_parser("plan", help="dry run: write the revisions that would be created")
    s.add_argument("keys", nargs="*")
    s = sub.add_parser("import", help="create work items in ADO, then links")
    s.add_argument("keys", nargs="*", help="only these Jira keys (for a pilot)")
    s.add_argument("--limit", type=int, help="stop after this many issues")
    s.add_argument("--no-links", action="store_true")
    s.add_argument("--possible-duplicates", choices=["skip", "create", "sync"],
                   help="skip (default): leave out tickets that may already be in the target; "
                        "create: create them tagged 'Possible duplicate' and linked to the match; "
                        "sync: delta-sync onto the existing ADO work item")
    sub.add_parser("links", help="add parent and issue links for imported items")
    s = sub.add_parser("verify", help="compare ADO with the export and write a CSV report")
    s.add_argument("--report", default="verify-report.csv")
    sub.add_parser("rollback", help="undo the run by deleting work items it created in ADO")
    a = p.parse_args(argv)
    if a.cmd == "prepare":
        return cmd_prepare(a)
    cfg = config.load(a.config)
    {"show": cmd_show, "duplicates": cmd_duplicates, "access": cmd_access, "export": cmd_export, "users": cmd_users,
     "check": cmd_check, "plan": cmd_plan, "import": cmd_import, "links": cmd_links, "verify": cmd_verify,
     "rollback": cmd_rollback}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
