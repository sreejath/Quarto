"""Phase 1: copy everything out of Jira into local files. No ADO access needed."""
import csv
import glob
import os
import re
from datetime import datetime, timezone

from .util import read_json, write_json


def issue_path(export_dir, key):
    return os.path.join(export_dir, "issues", f"{key}.json")


def load_issues(export_dir):
    keys = read_json(os.path.join(export_dir, "keys.json"))
    if keys is None:  # older exports: every issue on disk
        paths = glob.glob(os.path.join(export_dir, "issues", "*.json"))
    else:  # only what the latest query returned, so a ticket that left the sprint or became Done drops out
        paths = [p for p in (issue_path(export_dir, k) for k in keys) if os.path.exists(p)]

    def order(p):
        k = os.path.basename(p)[:-5]
        proj, _, num = k.rpartition("-")
        return (proj, int(num) if num.isdigit() else 0)

    for p in sorted(paths, key=order):
        yield read_json(p)


def _export_single_issue(jira, key, out, m, force=False, log=print):
    path = issue_path(out, key)
    if os.path.exists(path) and not force:
        return read_json(path), False
    issue = jira.issue(key)
    issue["changelog"] = jira.changelog(key)
    issue["comments"] = jira.comments(key)
    if m.get("include_worklogs", True) and getattr(jira, "worklogs", None):
        issue["worklogs"] = jira.worklogs(key)
    if m.get("include_remote_links", True) and getattr(jira, "remote_links", None):
        issue["remotelinks"] = jira.remote_links(key)
    if m.get("include_dev_links", True) and getattr(jira, "dev_links", None) and issue.get("id"):
        issue["devlinks"] = jira.dev_links(issue["id"])
    for att in issue["fields"].get("attachment") or []:
        name = re.sub(r"[^\w.\-]+", "_", att["filename"])
        local = os.path.join("attachments", key, f'{att["id"]}_{name}')
        dest = os.path.join(out, local)
        if not os.path.exists(dest):
            try:
                jira.download(att["content"], dest)
            except Exception as e:  # keep going; verify will flag it
                log(f"  {key}: attachment {att['filename']} failed: {e}")
                continue
        att["local_path"] = local
    write_json(path, issue)  # written last, so a present file means a complete issue
    return issue, True


def export(jira, cfg, force=False, log=print):
    out = cfg["paths"]["export_dir"]
    m = cfg["mapping"]
    fields = jira.fields()
    by_name = {}
    for f in fields:
        by_name.setdefault(f["name"], []).append(f["id"])
    meta = {
        "base_url": cfg["jira"]["base_url"],
        "jql": cfg["jira"]["jql"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "sprint_fields": by_name.get(m["sprint_jira_name"], []),
        "points_fields": [i for n in m["points_jira_names"] for i in by_name.get(n, [])],
        "field_names": {f["id"]: f["name"] for f in fields},
        "statuses": {s["name"]: (s.get("statusCategory") or {}).get("key") for s in jira.statuses()},
    }

    done = skipped = 0
    keys = list(jira.search_keys(cfg["jira"]["jql"]))
    meta["rank_order"] = {k: idx + 1 for idx, k in enumerate(keys)}
    write_json(os.path.join(out, "meta.json"), meta)

    seen_keys = set(keys)
    extra_keys = []
    for key in keys:
        issue, is_new = _export_single_issue(jira, key, out, m, force=force, log=log)
        if is_new:
            done += 1
            if done % 25 == 0:
                log(f"  exported {done} issues")
        else:
            skipped += 1
        if m.get("include_hierarchy") and issue:
            f = issue.get("fields") or {}
            parent_key = (f.get("parent") or {}).get("key")
            if parent_key and parent_key not in seen_keys:
                seen_keys.add(parent_key)
                extra_keys.append(parent_key)
            for st in f.get("subtasks") or []:
                st_key = st.get("key") if isinstance(st, dict) else None
                if st_key and st_key not in seen_keys:
                    seen_keys.add(st_key)
                    extra_keys.append(st_key)

    for key in extra_keys:
        try:
            issue, is_new = _export_single_issue(jira, key, out, m, force=force, log=log)
            if issue:
                issue["hierarchy_auto_included"] = True
                write_json(issue_path(out, key), issue)
                keys.append(key)
                if is_new:
                    done += 1
                else:
                    skipped += 1
                log(f"  auto-included hierarchy issue {key} ({issue['fields']['issuetype']['name']})")
        except Exception as e:
            log(f"  could not auto-include hierarchy issue {key}: {e}")

    write_json(os.path.join(out, "keys.json"), keys)
    log(f"Export finished. {done} new, {skipped} already on disk.")
    return done, skipped


def collect_users(export_dir):
    users = {}

    def add(u):
        if isinstance(u, dict) and u.get("accountId"):
            cur = users.setdefault(u["accountId"], {"name": "", "email": "", "active": ""})
            cur["name"] = u.get("displayName") or cur["name"]
            cur["email"] = u.get("emailAddress") or cur["email"]
            if "active" in u:
                cur["active"] = str(u["active"])

    for issue in load_issues(export_dir):
        f = issue["fields"]
        for k in ("creator", "reporter", "assignee"):
            add(f.get(k))
        for v in f.values():
            if isinstance(v, dict) and v.get("accountId"):
                add(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and item.get("accountId"):
                        add(item)
        for c in issue.get("comments", []):
            add(c.get("author"))
        for w in issue.get("worklogs", []):
            add(w.get("author"))
        for a in f.get("attachment") or []:
            add(a.get("author"))
        for h in issue.get("changelog", []):
            add(h.get("author"))
            for it in h.get("items", []):
                if (it.get("field") or "").lower() == "assignee":
                    for side in ("from", "to"):
                        if it.get(side):
                            add({"accountId": it[side], "displayName": it.get(side + "String")})
    return users


def write_users_file(export_dir, path, domains=None, ado_discovered=None):
    """Create or extend the user mapping file, which all runs share. Every existing row is kept
    exactly, blanks included, so a person left blank on purpose stays blank and people from
    earlier runs stay in the file. Only people new to the file are added, with a guessed identity.
    domains rewrites email domains, for example {"company.com": "company.org"}.
    ado_discovered can pass {"by_email": {...}, "by_name": {...}} from Azure DevOps team discovery.
    Returns (people in this export, of them without an ADO identity)."""
    existing = load_users_file(path, raw=True)
    users = collect_users(export_dir)
    disc_email = (ado_discovered or {}).get("by_email") or {}
    disc_name = (ado_discovered or {}).get("by_name") or {}
    has_ado_directory = bool(disc_email or disc_name)

    def guess(email, name=""):
        if email and "@" in email:
            local, dom = email.rsplit("@", 1)
            rewritten = f"{local}@{(domains or {}).get(dom.lower(), dom)}"
            if has_ado_directory:
                if rewritten.lower() in disc_email:
                    return disc_email[rewritten.lower()]
                if email.lower() in disc_email:
                    return disc_email[email.lower()]
            else:
                return rewritten
        if has_ado_directory and name and name.lower() in disc_name:
            return disc_name[name.lower()]
        return ""

    rows = [dict(r) for r in existing.values()]
    for acc, u in sorted(users.items(), key=lambda kv: kv[1]["name"].lower()):
        if acc in existing:
            row = next(r for r in rows if r["account_id"] == acc)
            row["display_name"] = u["name"] or row.get("display_name", "")
            row["jira_email"] = u["email"] or row.get("jira_email", "")
            row["active"] = u["active"] or row.get("active", "")
        else:
            rows.append({"account_id": acc, "display_name": u["name"], "jira_email": u["email"],
                         "active": u["active"], "ado_identity": guess(u["email"], u["name"])})
    cols = ["account_id", "display_name", "jira_email", "active", "ado_identity"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    mine = [r for r in rows if r["account_id"] in users]
    return len(mine), sum(1 for r in mine if not (r.get("ado_identity") or "").strip())


def load_users_file(path, raw=False):
    if not path or not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if raw:
        return {r["account_id"]: r for r in rows}
    return {
        "ids": {r["account_id"]: r["ado_identity"].strip() for r in rows if r.get("ado_identity", "").strip()},
        "names": {r["account_id"]: r.get("display_name", "") for r in rows},
    }
