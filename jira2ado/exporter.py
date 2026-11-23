"""Phase 1: copy everything out of Jira into local files. No ADO access needed."""
from concurrent.futures import ThreadPoolExecutor
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


def _has_dev_status(issue, meta=None):
    """Return False when Jira's Development summary field shows the ticket has zero PRs/branches/commits."""
    f = (issue or {}).get("fields") or {}
    dev_fids = (meta or {}).get("dev_fields") or []
    if dev_fids:
        vals = [f.get(fid) for fid in dev_fids]
        if all(v in (None, "", "{}") for v in vals):
            return False
        joined = " ".join(str(v).lower() for v in vals if v)
        if not any(tok in joined for tok in ("pullrequest", "repository", "branch", "commit")):
            return False
    return True


def _export_single_issue(jira, key, out, m, force=False, expected_updated=None, meta=None, log=print):
    path = issue_path(out, key)
    cached = read_json(path) if (os.path.exists(path) and not force) else None
    if cached is not None and expected_updated is not None:
        cached_updated = (cached.get("fields") or {}).get("updated")
        if cached_updated and cached_updated == expected_updated:
            return cached, False
    issue = jira.issue(key)
    if cached is not None and expected_updated is None:
        live_updated = (issue.get("fields") or {}).get("updated")
        cached_updated = (cached.get("fields") or {}).get("updated")
        live_status = ((issue.get("fields") or {}).get("status") or {}).get("name")
        cached_status = ((cached.get("fields") or {}).get("status") or {}).get("name")
        if live_updated and live_updated == cached_updated and live_status == cached_status:
            return cached, False

    # Use inline expanded changelog when complete; otherwise fetch full paged changelog
    cl = issue.get("changelog")
    if isinstance(cl, dict) and "histories" in cl:
        histories = cl.get("histories") or []
        if cl.get("total", len(histories)) <= len(histories):
            issue["changelog"] = histories
        else:
            issue["changelog"] = jira.changelog(key) if getattr(jira, "changelog", None) else histories
    elif not isinstance(cl, list):
        issue["changelog"] = jira.changelog(key) if getattr(jira, "changelog", None) else []

    # Use inline comments when 0 comments or when all comments + renderedBody are already inline
    f_comment = (issue.get("fields") or {}).get("comment")
    r_comment = (issue.get("renderedFields") or {}).get("comment")
    if not isinstance(issue.get("comments"), list):
        if isinstance(f_comment, dict) and "comments" in f_comment:
            c_list = f_comment.get("comments") or []
            total_c = f_comment.get("total", len(c_list))
            if total_c == 0:
                issue["comments"] = []
            elif total_c <= len(c_list) and isinstance(r_comment, dict) and len(r_comment.get("comments") or []) == len(c_list):
                r_by_id = {rc.get("id"): rc.get("renderedBody") for rc in (r_comment.get("comments") or []) if isinstance(rc, dict) and rc.get("id")}
                for c in c_list:
                    if c.get("id") in r_by_id and r_by_id[c["id"]]:
                        c["renderedBody"] = r_by_id[c["id"]]
                issue["comments"] = c_list
            else:
                issue["comments"] = jira.comments(key) if getattr(jira, "comments", None) else c_list
        else:
            issue["comments"] = jira.comments(key) if getattr(jira, "comments", None) else []

    # Use inline worklogs when 0 worklogs or all worklogs are already inline
    if m.get("include_worklogs", True):
        f_wl = (issue.get("fields") or {}).get("worklog")
        r_wl = (issue.get("renderedFields") or {}).get("worklog")
        if not isinstance(issue.get("worklogs"), list):
            if isinstance(f_wl, dict) and "worklogs" in f_wl:
                w_list = f_wl.get("worklogs") or []
                total_w = f_wl.get("total", len(w_list))
                if total_w == 0:
                    issue["worklogs"] = []
                elif total_w <= len(w_list):
                    if isinstance(r_wl, dict):
                        rw_by_id = {rw.get("id"): rw.get("renderedBody") for rw in (r_wl.get("worklogs") or []) if isinstance(rw, dict) and rw.get("id")}
                        for w in w_list:
                            if w.get("id") in rw_by_id and rw_by_id[w["id"]]:
                                w["renderedBody"] = rw_by_id[w["id"]]
                    issue["worklogs"] = w_list
                else:
                    issue["worklogs"] = jira.worklogs(key) if getattr(jira, "worklogs", None) else w_list
            elif getattr(jira, "worklogs", None):
                issue["worklogs"] = jira.worklogs(key)

    if m.get("include_remote_links", True) and getattr(jira, "remote_links", None) and "remotelinks" not in issue:
        issue["remotelinks"] = jira.remote_links(key)
    if m.get("include_dev_links", True) and getattr(jira, "dev_links", None) and issue.get("id") and "devlinks" not in issue:
        issue["devlinks"] = jira.dev_links(issue["id"]) if _has_dev_status(issue, meta) else []

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


def search_run_key_timestamps(jira, cfg):
    """Fetch [(key, updated_timestamp)] for this run via JQL (including resolved board filter JQL)."""
    run_info = cfg.get("run") or {}
    j_cfg = cfg.get("jira") or {}
    jql = j_cfg.get("jql")
    board_id = j_cfg.get("board_id") or run_info.get("board_id")
    if jql and getattr(jira, "search_key_timestamps", None):
        return list(jira.search_key_timestamps(jql))
    if jql and getattr(jira, "search_keys", None):
        return [(k, None) for k in jira.search_keys(jql)]
    if run_info.get("mode") == "board" and board_id and getattr(jira, "board_keys", None):
        return [(k, None) for k in jira.board_keys(board_id, jql=j_cfg.get("board_filter_jql"))]
    return [(k, None) for k in jira.search_keys(jql)]


def search_run_keys(jira, cfg):
    """Fetch the target issue keys for this run."""
    return [k for k, _ in search_run_key_timestamps(jira, cfg)]


def export(jira, cfg, force=False, log=print):
    out = cfg["paths"]["export_dir"]
    state_folder = cfg["paths"].get("state_dir") or out
    pause_file = os.path.join(state_folder, ".pause")
    preview_file = os.path.join(state_folder, "preview.json")
    # Remove any old preview.json before modifying export scope so a paused export is never mistaken for a finished preview
    if os.path.exists(preview_file):
        try:
            os.remove(preview_file)
        except OSError:
            pass
    m = cfg["mapping"]
    fields = jira.fields()
    by_name = {}
    for f in fields:
        by_name.setdefault(f["name"], []).append(f["id"])
    dev_fields = [
        f["id"] for f in fields
        if (f.get("name") or "").lower() == "development"
        or "devsummary" in str(((f.get("schema") or {}).get("custom")) or "").lower()
    ]
    meta = {
        "base_url": cfg["jira"]["base_url"],
        "jql": cfg["jira"]["jql"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "sprint_fields": by_name.get(m["sprint_jira_name"], []),
        "points_fields": [i for n in m["points_jira_names"] for i in by_name.get(n, [])],
        "dev_fields": dev_fields,
        "field_names": {f["id"]: f["name"] for f in fields},
        "statuses": {s["name"]: (s.get("statusCategory") or {}).get("key") for s in jira.statuses()},
        "sprints": list((cfg.get("run") or {}).get("sprints") or []),
    }

    done = skipped = 0
    key_ts = search_run_key_timestamps(jira, cfg)
    keys = [k for k, _ in key_ts]
    live_updated = {k: ts for k, ts in key_ts}
    meta["rank_order"] = {k: idx + 1 for idx, k in enumerate(keys)}
    write_json(os.path.join(out, "meta.json"), meta)
    write_json(os.path.join(out, "keys.json"), keys)

    seen_keys = set(keys)
    extra_keys = []

    def _record_hierarchy(issue):
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

    # Process in chunks of 16 across up to 8 worker threads so .pause is checked promptly and exports run concurrently
    chunk_size = 16
    for start_idx in range(0, len(keys), chunk_size):
        if os.path.exists(pause_file):
            try:
                os.remove(pause_file)
            except OSError:
                pass
            write_json(os.path.join(out, "keys.json"), keys)
            log(f"Export paused cleanly after {done} new/updated, {skipped} cached issue(s). Click Resume to continue.")
            raise SystemExit(2)
        chunk = keys[start_idx:start_idx + chunk_size]
        # Fast-path check for unchanged cached files before spawning threads
        to_fetch = []
        for key in chunk:
            if os.path.exists(pause_file):
                break
            path = issue_path(out, key)
            exp_ts = live_updated.get(key)
            if not force and exp_ts is not None and os.path.exists(path):
                cached = read_json(path)
                if cached and (cached.get("fields") or {}).get("updated") == exp_ts:
                    skipped += 1
                    _record_hierarchy(cached)
                    continue
            to_fetch.append(key)
        if os.path.exists(pause_file):
            try:
                os.remove(pause_file)
            except OSError:
                pass
            write_json(os.path.join(out, "keys.json"), keys)
            log(f"Export paused cleanly after {done} new/updated, {skipped} cached issue(s). Click Resume to continue.")
            raise SystemExit(2)
        if not to_fetch:
            continue
        if len(to_fetch) == 1:
            key = to_fetch[0]
            issue, is_new = _export_single_issue(
                jira, key, out, m, force=force, expected_updated=live_updated.get(key), meta=meta, log=log
            )
            if is_new:
                done += 1
                if done % 25 == 0:
                    log(f"  exported {done} issues")
            else:
                skipped += 1
            _record_hierarchy(issue)
        else:
            workers = min(8, len(to_fetch))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    (key, pool.submit(
                        _export_single_issue, jira, key, out, m, force, live_updated.get(key), meta, log
                    ))
                    for key in to_fetch
                ]
                for key, fut in futures:
                    issue, is_new = fut.result()
                    if is_new:
                        done += 1
                        if done % 25 == 0:
                            log(f"  exported {done} issues")
                    else:
                        skipped += 1
                    _record_hierarchy(issue)

    for key in extra_keys:
        try:
            issue, is_new = _export_single_issue(jira, key, out, m, force=force, meta=meta, log=log)
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


def collect_users(export_dir, issues=None):
    users = {}

    def add(u):
        if isinstance(u, dict) and u.get("accountId"):
            cur = users.setdefault(u["accountId"], {"name": "", "email": "", "active": ""})
            cur["name"] = u.get("displayName") or cur["name"]
            cur["email"] = u.get("emailAddress") or cur["email"]
            if "active" in u:
                cur["active"] = str(u["active"])

    for issue in (issues if issues is not None else load_issues(export_dir)):
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
    has_ado_directory = ado_discovered is not None

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
