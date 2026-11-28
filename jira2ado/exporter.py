"""Phase 1: copy everything out of Jira into local files. No ADO access needed."""
from concurrent.futures import ThreadPoolExecutor
import csv
import glob
import os
import re
from datetime import datetime, timezone

from .util import name_key, node_name, read_json, write_json
from .zephyr import is_test_case_issue


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


def _attachments_complete(cached, out):
    """Return True only when every issue and Zephyr step attachment on a cached issue exists on disk."""
    for att in ((cached or {}).get("fields") or {}).get("attachment") or []:
        rel = att.get("local_path")
        if not rel or not os.path.exists(os.path.join(out, rel)):
            return False
    for st in (cached or {}).get("zephyr_steps") or []:
        for att in st.get("attachments") or []:
            rel = att.get("local_path")
            if not rel or not os.path.exists(os.path.join(out, rel)):
                return False
    return True


def _zephyr_steps_ready(cached, cfg, zephyr):
    """Return True if Zephyr is not active, issue is not a Test Case, or zephyr_steps were already fetched."""
    if zephyr is None:
        return True
    if not is_test_case_issue(cached, cfg):
        return True
    return isinstance((cached or {}).get("zephyr_steps"), list)


def _attach_zephyr_steps(zephyr, issue, out, cfg, meta=None, log=print):
    """Fetch Zephyr Squad test steps and step-level attachments for a Jira Test issue."""
    if zephyr is None or not is_test_case_issue(issue, cfg):
        return False
    key = issue.get("key") or ""
    issue_id = issue.get("id")
    project_id = ((issue.get("fields") or {}).get("project") or {}).get("id") or (meta or {}).get("project_id")
    if not (issue_id and project_id):
        return False
    steps = zephyr.test_steps(issue_id, project_id) or []
    zcfg = (cfg or {}).get("zephyr") or {}
    if zcfg.get("include_step_attachments", True) and getattr(zephyr, "step_attachments", None):
        for st in steps:
            step_atts = zephyr.step_attachments(st["id"], issue_id, project_id) or []
            saved = []
            for att in step_atts:
                clean_name = re.sub(r"[^\w.\-]+", "_", att["filename"])
                local = os.path.join("attachments", key, f'zstep_{st["id"]}_{att["id"]}_{clean_name}')
                dest = os.path.join(out, local)
                entry = dict(att)
                if not os.path.exists(dest):
                    try:
                        zephyr.download_attachment(att["id"], st["id"], issue_id, project_id, dest)
                    except Exception as e:
                        log(f"  {key}: Zephyr step attachment {att['filename']} failed: {e}")
                        saved.append(entry)
                        continue
                entry["local_path"] = local
                saved.append(entry)
            st["attachments"] = saved
    issue["zephyr_steps"] = steps
    return True


def _export_single_issue(jira, key, out, m, force=False, expected_updated=None, meta=None, log=print,
                         prefetched_issue=None, cfg=None, zephyr=None):
    path = issue_path(out, key)
    cached = read_json(path) if (os.path.exists(path) and not force) else None
    if cached is not None and expected_updated is not None and _attachments_complete(cached, out):
        cached_updated = (cached.get("fields") or {}).get("updated")
        if cached_updated and cached_updated == expected_updated:
            if _zephyr_steps_ready(cached, cfg, zephyr):
                return cached, False
            # Fast-path: Jira issue unchanged, only enrich with Zephyr steps without re-fetching Jira
            _attach_zephyr_steps(zephyr, cached, out, cfg, meta=meta, log=log)
            write_json(path, cached)
            return cached, True
    issue = prefetched_issue if prefetched_issue is not None else jira.issue(key)
    if cached is not None and expected_updated is None and _attachments_complete(cached, out):
        live_updated = (issue.get("fields") or {}).get("updated")
        cached_updated = (cached.get("fields") or {}).get("updated")
        live_status = ((issue.get("fields") or {}).get("status") or {}).get("name")
        cached_status = ((cached.get("fields") or {}).get("status") or {}).get("name")
        if live_updated and live_updated == cached_updated and live_status == cached_status:
            if _zephyr_steps_ready(cached, cfg, zephyr):
                return cached, False
            _attach_zephyr_steps(zephyr, cached, out, cfg, meta=meta, log=log)
            write_json(path, cached)
            return cached, True

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
    if zephyr is not None:
        try:
            _attach_zephyr_steps(zephyr, issue, out, cfg, meta=meta, log=log)
        except Exception as e:
            log(f"  {key}: Zephyr test steps fetch failed: {e}")
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


def _infer_project_key(cfg, keys):
    proj = ((cfg.get("run") or {}).get("from") or "").strip().upper()
    if proj:
        return proj
    jql = (cfg.get("jira") or {}).get("jql") or ""
    m = re.search(r'\bproject\s*=\s*"?([A-Za-z][A-Za-z0-9_]*)"?', jql, re.I)
    if m:
        return m.group(1).upper()
    prefixes = {k.rpartition("-")[0] for k in (keys or ()) if "-" in k}
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return ""


def _board_sort_key(counts, bid_s):
    return (counts.get(bid_s, 0), int(bid_s) if bid_s.isdigit() else 0, bid_s)


def _assign_ticket_boards(target_keys, board_raw_keys, area_by_bid):
    key_set = set(target_keys)
    counts = {bid_s: len(raw_set & key_set) for bid_s, raw_set in board_raw_keys.items()}
    ticket_boards = {}
    for k in target_keys:
        matches = [bid_s for bid_s, raw_set in board_raw_keys.items() if k in raw_set and bid_s in area_by_bid]
        if matches:
            best = min(matches, key=lambda bid_s: _board_sort_key(counts, bid_s))
            ticket_boards[k] = area_by_bid[best]
    return ticket_boards, counts


def _discover_project_boards(jira, cfg, keys, meta, log=print):
    """Discover project boards, their sprint ownership, and ticket-to-board mapping for boards_to_areas."""
    meta["boards"] = []
    meta["sprint_boards"] = {}
    meta["ticket_boards"] = {}
    project_key = _infer_project_key(cfg, keys)
    if not project_key or not getattr(jira, "boards", None):
        return {}, {}
    try:
        raw_boards = jira.boards(project_key) or []
    except Exception as e:
        log(f"  Warning: could not list Jira boards for project {project_key}: {e}")
        return {}, {}
    if not raw_boards:
        log(f"  No Jira boards found for project {project_key}; items will use the target project root.")
        return {}, {}

    norm_counts = {}
    for b in raw_boards:
        clean = node_name(b.get("name") or f"Board {b.get('id')}")
        nk = name_key(clean)
        norm_counts[nk] = norm_counts.get(nk, 0) + 1

    boards_meta, area_by_bid = [], {}
    for b in raw_boards:
        bid = b.get("id")
        raw_name = b.get("name") or f"Board {bid}"
        clean = node_name(raw_name)
        area = f"{clean} ({bid})" if (norm_counts.get(name_key(clean), 0) > 1 and bid is not None) else clean
        boards_meta.append({"id": bid, "name": raw_name, "type": b.get("type", "scrum"), "area": area})
        if bid is not None:
            area_by_bid[str(bid)] = area

    board_raw_keys = {}
    for b in boards_meta:
        bid = b["id"]
        if bid is None:
            continue
        bid_s = str(bid)
        matched = set()
        bf_jql = None
        if getattr(jira, "board_filter_jql", None):
            try:
                bf_jql = jira.board_filter_jql(bid)
            except Exception:
                bf_jql = None
        if bf_jql and getattr(jira, "search_keys", None):
            scoped_jql = f'({bf_jql}) AND project = "{project_key}"'
            try:
                matched = set(jira.search_keys(scoped_jql))
            except Exception as e:
                log(f"  Warning: board '{b['name']}' filter query failed ({e}); falling back to board issues")
                bf_jql = None
        if not bf_jql and getattr(jira, "board_keys", None):
            try:
                matched = set(jira.board_keys(bid))
            except Exception as e:
                log(f"  Warning: could not list issues for board '{b['name']}': {e}")
        board_raw_keys[bid_s] = matched

    ticket_boards, counts = _assign_ticket_boards(keys, board_raw_keys, area_by_bid)

    sprints_by_id = {}
    sprint_candidates = {}
    sprint_origin = {}

    def _record_sprint(sp, owner_bid_s=None):
        if not isinstance(sp, dict):
            return
        sid = sp.get("id") if sp.get("id") is not None else sp.get("number")
        if sid is None:
            return
        sid_s = str(sid)
        cur = sprints_by_id.get(sid_s)
        if cur is None:
            sprints_by_id[sid_s] = dict(sp)
        else:
            for k, v in sp.items():
                if v not in (None, "", []):
                    cur.setdefault(k, v)
        if owner_bid_s is not None:
            sprint_candidates.setdefault(sid_s, set()).add(owner_bid_s)
        for bid in sp.get("board_ids") or []:
            if bid is not None:
                sprint_candidates.setdefault(sid_s, set()).add(str(bid))
        orig = sp.get("originBoardId")
        if orig is not None:
            sprint_origin[sid_s] = str(orig)

    for sp in meta.get("sprints") or []:
        _record_sprint(sp)

    if getattr(jira, "board_sprints", None):
        for b in boards_meta:
            bid = b["id"]
            if bid is None:
                continue
            bid_s = str(bid)
            try:
                try:
                    bsp_list = jira.board_sprints(bid, board_name=b["name"]) or []
                except TypeError:
                    bsp_list = jira.board_sprints(bid) or []
            except Exception:
                bsp_list = []
            for sp in bsp_list:
                _record_sprint(sp, owner_bid_s=bid_s)

    sprint_boards = {}
    for sid_s in set(sprint_candidates) | set(sprint_origin):
        orig_bid = sprint_origin.get(sid_s)
        if orig_bid and orig_bid in area_by_bid:
            sprint_boards[sid_s] = area_by_bid[orig_bid]
            continue
        cands = [bid_s for bid_s in sprint_candidates.get(sid_s, ()) if bid_s in area_by_bid]
        if cands:
            best = min(cands, key=lambda bid_s: _board_sort_key(counts, bid_s))
            sprint_boards[sid_s] = area_by_bid[best]

    meta["boards"] = boards_meta
    meta["sprint_boards"] = sprint_boards
    meta["ticket_boards"] = ticket_boards
    if sprints_by_id:
        meta["sprints"] = list(sprints_by_id.values())
    log(f"Boards mapped for per-board Areas/Iterations ({len(boards_meta)}): " + ", ".join(b["area"] for b in boards_meta))
    return board_raw_keys, area_by_bid


def export_zephyr_cycles(jira, zephyr, cfg, out, meta, keys=None, log=print):
    """Export Zephyr Squad Test Cycles, Folders, and test-case memberships into export/zephyr-cycles.json."""
    zcfg = (cfg or {}).get("zephyr") or {}
    if zephyr is None or not zcfg.get("include_test_plans", True):
        return None
    project_key = _infer_project_key(cfg, keys or [])
    project_id = (meta or {}).get("project_id")
    if not project_id and project_key and getattr(jira, "project_info", None):
        try:
            project_id = (jira.project_info(project_key) or {}).get("id")
        except Exception:
            project_id = None
    if not project_id:
        log("  Warning: could not determine Jira numeric project ID; skipping Zephyr Test Cycles export.")
        return None

    versions = []
    if project_key and getattr(jira, "project_versions", None):
        try:
            versions = list(jira.project_versions(project_key) or [])
        except Exception as e:
            log(f"  Warning: could not list Jira versions for {project_key}: {e}")
    versions.append({"id": "-1", "name": "Unscheduled", "startDate": "", "releaseDate": "", "description": ""})

    exported_versions = []
    total_cycles = total_folders = 0
    for v in versions:
        vid = str(v["id"])
        try:
            cyc_list = zephyr.cycles(project_id, version_id=vid) or []
        except Exception as e:
            log(f"  Warning: could not fetch Zephyr cycles for version '{v['name']}': {e}")
            continue
        if not cyc_list:
            continue
        out_cycles = []
        for c in cyc_list:
            cid = str(c["id"])
            folder_entries = []
            folder_keys_seen = set()
            if getattr(zephyr, "folders", None):
                try:
                    raw_folders = zephyr.folders(cid, project_id, version_id=vid) or []
                except Exception:
                    raw_folders = []
                for fld in raw_folders:
                    fkeys = []
                    if getattr(zephyr, "folder_execution_keys", None):
                        try:
                            fkeys = list(zephyr.folder_execution_keys(fld["id"], cid, project_id, version_id=vid) or [])
                        except Exception:
                            fkeys = []
                    folder_keys_seen.update(fkeys)
                    folder_entries.append({
                        "id": str(fld["id"]),
                        "name": fld["name"],
                        "description": fld.get("description") or "",
                        "keys": fkeys,
                    })
                    total_folders += 1
            direct_keys = []
            if getattr(zephyr, "cycle_execution_keys", None):
                try:
                    all_ckeys = list(zephyr.cycle_execution_keys(cid, project_id, version_id=vid) or [])
                except Exception:
                    all_ckeys = []
                direct_keys = [k for k in all_ckeys if k not in folder_keys_seen]
            out_cycles.append({
                "id": cid,
                "name": c["name"],
                "description": c.get("description") or "",
                "startDate": c.get("startDate") or "",
                "endDate": c.get("endDate") or "",
                "direct_keys": direct_keys,
                "folders": folder_entries,
            })
            total_cycles += 1
        exported_versions.append({
            "id": vid,
            "name": v["name"],
            "startDate": v.get("startDate") or "",
            "releaseDate": v.get("releaseDate") or "",
            "description": v.get("description") or "",
            "cycles": out_cycles,
        })

    payload = {
        "project_key": project_key,
        "project_id": str(project_id),
        "versions": exported_versions,
    }
    write_json(os.path.join(out, "zephyr-cycles.json"), payload)
    meta["zephyr"] = {
        "enabled": True,
        "provider": zcfg.get("provider", "squad"),
        "versions": len(exported_versions),
        "cycles": total_cycles,
        "folders": total_folders,
    }
    log(f"Zephyr Squad cycles exported: {len(exported_versions)} version(s), {total_cycles} cycle(s), {total_folders} folder(s).")
    return payload


def export(jira, cfg, force=False, log=print, zephyr=None):
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
    if zephyr is not None:
        proj_key = _infer_project_key(cfg, keys)
        if proj_key and getattr(jira, "project_info", None):
            try:
                pinfo = jira.project_info(proj_key) or {}
                if pinfo.get("id"):
                    meta["project_id"] = str(pinfo["id"])
            except Exception:
                pass
    board_raw_keys, area_by_bid = {}, {}
    if m.get("boards_to_areas"):
        board_raw_keys, area_by_bid = _discover_project_boards(jira, cfg, keys, meta, log=log)
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

    # Process in chunks of 50 (matching bulk /search/jql page size) across up to 8 worker threads so .pause is checked promptly
    chunk_size = 50
    batch_fn = getattr(jira, "search_issues_batch", None)
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
                if (cached and (cached.get("fields") or {}).get("updated") == exp_ts
                        and _attachments_complete(cached, out) and _zephyr_steps_ready(cached, cfg, zephyr)):
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
        prefetched = {}
        # Only batch-fetch from Jira keys that don't already have an up-to-date local Jira issue (needing only Zephyr steps)
        jira_fetch_keys = []
        for key in to_fetch:
            path = issue_path(out, key)
            exp_ts = live_updated.get(key)
            if not force and exp_ts is not None and os.path.exists(path):
                cached = read_json(path)
                if cached and (cached.get("fields") or {}).get("updated") == exp_ts and _attachments_complete(cached, out):
                    continue
            jira_fetch_keys.append(key)
        if batch_fn and len(jira_fetch_keys) > 1:
            try:
                prefetched = batch_fn(jira_fetch_keys) or {}
            except Exception:
                prefetched = {}
        if len(to_fetch) == 1:
            key = to_fetch[0]
            issue, is_new = _export_single_issue(
                jira, key, out, m, force=force, expected_updated=live_updated.get(key), meta=meta, log=log,
                prefetched_issue=prefetched.get(key), cfg=cfg, zephyr=zephyr,
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
                        _export_single_issue, jira, key, out, m, force, live_updated.get(key), meta, log,
                        prefetched.get(key), cfg, zephyr,
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
            issue, is_new = _export_single_issue(
                jira, key, out, m, force=force, meta=meta, log=log, cfg=cfg, zephyr=zephyr
            )
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

    if zephyr is not None:
        export_zephyr_cycles(jira, zephyr, cfg, out, meta, keys=keys, log=log)
        write_json(os.path.join(out, "meta.json"), meta)

    if extra_keys and m.get("boards_to_areas") and board_raw_keys:
        meta["ticket_boards"], _ = _assign_ticket_boards(keys, board_raw_keys, area_by_bid)
        write_json(os.path.join(out, "meta.json"), meta)

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
