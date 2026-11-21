import os

import requests

from .util import backoff_sleep


class JiraClient:
    """Jira Cloud REST v3 + Agile 1.0 client with paging, retry, boards, worklogs, and remote/dev links."""

    def __init__(self, base_url, email, token, session=None):
        self.base = (base_url or "").strip().rstrip("/")
        if self.base.lower().startswith("http://"):
            raise RuntimeError("Jira URL must use https:// so your API token is never sent over plain HTTP.")
        self.s = session or requests.Session()
        self.s.auth = (email, token)
        self.s.headers.update({"Accept": "application/json"})

    def _request(self, method, url, **kw):
        if not url.startswith("http"):
            url = self.base + url
        for attempt in range(8):
            r = self.s.request(method, url, timeout=120, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                backoff_sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Jira {method} {url} failed: {r.status_code} {r.text[:400]}")
            return r
        raise RuntimeError(f"Jira {method} {url} kept failing after retries")

    def myself(self):
        me = self._request("GET", "/rest/api/3/myself").json()
        return me.get("displayName") or me.get("emailAddress") or me.get("accountId")

    def projects(self):
        """Every Jira project this account can see: [{key, name}], sorted by key."""
        out, start = [], 0
        while True:
            data = self._request("GET", "/rest/api/3/project/search",
                                 params={"startAt": start, "maxResults": 50, "orderBy": "key"}).json()
            values = data.get("values", [])
            out += [{"key": p["key"], "name": p.get("name", "")} for p in values]
            start += len(values)
            if data.get("isLast", True) or not values:
                return out

    def _agile_pages(self, path, params=None):
        start = 0
        while True:
            p = dict(params or {}, startAt=start, maxResults=50)
            data = self._request("GET", path, params=p).json()
            values = data.get("values", [])
            yield from values
            start += len(values)
            if data.get("isLast", True) or not values:
                return

    def boards(self, project=None):
        """Every Agile board for a project (or all boards if project is None): [{id, name, type}]."""
        params = {"projectKeyOrId": project} if project else {}
        out = []
        for b in self._agile_pages("/rest/agile/1.0/board", params=params):
            out.append({
                "id": b["id"],
                "name": b.get("name") or f"Board {b['id']}",
                "type": b.get("type", "scrum"),
            })
        return sorted(out, key=lambda x: x["name"].lower())

    def board_sprints(self, board_id, board_name=None):
        """Every sprint on a single Jira board: [{id, name, state, startDate, endDate, goal, boards}]."""
        found = {}
        try:
            for sp in self._agile_pages(f"/rest/agile/1.0/board/{board_id}/sprint"):
                entry = found.setdefault(sp["id"], {**sp, "boards": [], "board_ids": []})
                bname = board_name or str(board_id)
                if bname not in entry["boards"]:
                    entry["boards"].append(bname)
                if board_id not in entry["board_ids"]:
                    entry["board_ids"].append(board_id)
        except RuntimeError:
            # Kanban boards do not support sprints
            return []
        return list(found.values())

    def project_sprints(self, project, board_id=None):
        """Every sprint on the project's boards (or a specific board_id if given)."""
        if board_id:
            return self.board_sprints(board_id)
        found = {}
        for board in self._agile_pages(f"/rest/agile/1.0/board", params={"projectKeyOrId": project}):
            try:
                for sp in self._agile_pages(f"/rest/agile/1.0/board/{board['id']}/sprint"):
                    entry = found.setdefault(sp["id"], {**sp, "boards": [], "board_ids": []})
                    bname = board.get("name") or str(board["id"])
                    if bname not in entry["boards"]:
                        entry["boards"].append(bname)
                    if board["id"] not in entry["board_ids"]:
                        entry["board_ids"].append(board["id"])
            except RuntimeError:
                continue  # a Kanban board has no sprints
        return list(found.values())

    def board_keys(self, board_id, jql=None):
        """Yield all issue keys on a Jira board (Scrum or Kanban, including backlog and unsprinted tickets)."""
        params = {"fields": "key"}
        if jql:
            params["jql"] = jql
        for issue in self._agile_pages(f"/rest/agile/1.0/board/{board_id}/issue", params=params):
            if issue.get("key"):
                yield issue["key"]

    def search_keys(self, jql):
        """Yield issue keys. Uses /search/jql, which replaced the removed /search endpoint."""
        token = None
        while True:
            body = {"jql": jql, "maxResults": 100, "fields": ["key"]}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", json=body).json()
            for i in data.get("issues", []):
                yield i["key"]
            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                return

    def issue(self, key):
        return self._request(
            "GET", f"/rest/api/3/issue/{key}", params={"fields": "*all", "expand": "renderedFields"}
        ).json()

    def _paged(self, path, list_key, params=None):
        start, out = 0, []
        while True:
            p = dict(params or {}, startAt=start, maxResults=100)
            data = self._request("GET", path, params=p).json()
            page = data.get(list_key, [])
            out.extend(page)
            start += len(page)
            if not page or data.get("isLast") or start >= data.get("total", start + 1):
                return out

    def changelog(self, key):
        """Full change history. The search API caps this at 100 entries, this call does not."""
        return self._paged(f"/rest/api/3/issue/{key}/changelog", "values")

    def comments(self, key):
        return self._paged(f"/rest/api/3/issue/{key}/comment", "comments", {"expand": "renderedBody"})

    def worklogs(self, key):
        """Full worklog list for time-tracking migration."""
        try:
            return self._paged(f"/rest/api/3/issue/{key}/worklog", "worklogs", {"expand": "renderedBody"})
        except RuntimeError:
            return []

    def remote_links(self, key):
        """Remote issue links (Confluence pages, web links, external tickets, PRs)."""
        try:
            data = self._request("GET", f"/rest/api/3/issue/{key}/remotelink").json()
            return data if isinstance(data, list) else []
        except RuntimeError:
            return []

    def dev_links(self, issue_id):
        """Extract Git PRs, branches, and commits from Jira's development panel if available."""
        if not issue_id:
            return []
        links = []
        seen = set()
        for app in ("github", "bitbucket", "gitlab", "stash"):
            for dtype in ("pullrequest", "repository"):
                try:
                    r = self.s.get(
                        f"{self.base}/rest/dev-status/1.0/issue/detail",
                        params={"issueId": str(issue_id), "applicationType": app, "dataType": dtype},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        continue
                    data = r.json() or {}
                    for detail in data.get("detail") or []:
                        for pr in detail.get("pullRequests") or []:
                            url = pr.get("url")
                            if url and url not in seen:
                                seen.add(url)
                                links.append({
                                    "url": url,
                                    "title": f"PR {pr.get('id', '')}: {pr.get('name', 'Pull Request')} ({pr.get('status', '')})".strip(),
                                    "kind": "Pull Request",
                                })
                        for br in detail.get("branches") or []:
                            url = br.get("url")
                            if url and url not in seen:
                                seen.add(url)
                                links.append({
                                    "url": url,
                                    "title": f"Branch: {br.get('name', 'branch')}",
                                    "kind": "Branch",
                                })
                        for repo in detail.get("repositories") or []:
                            for cm in repo.get("commits") or []:
                                url = cm.get("url")
                                if url and url not in seen:
                                    seen.add(url)
                                    links.append({
                                        "url": url,
                                        "title": f"Commit {str(cm.get('displayId') or cm.get('id', ''))[:8]}: {(cm.get('message') or '')[:80]}",
                                        "kind": "Commit",
                                    })
                except Exception:
                    continue
        return links

    def fields(self):
        return self._request("GET", "/rest/api/3/field").json()

    def statuses(self):
        return self._request("GET", "/rest/api/3/status").json()

    def download(self, url, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # redirect=false makes Jira serve the file itself instead of sending the
        # client on to api.media.atlassian.com, which many networks block.
        params = {"redirect": "false"} if "/attachment/content/" in url else None
        r = self._request("GET", url, stream=True, params=params)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        os.replace(tmp, dest)
