import base64
import os
from urllib.parse import quote

import requests

from .util import backoff_sleep


def _message(r):
    try:
        return (r.json() or {}).get("message") or r.text[:300]
    except ValueError:
        return r.text[:300]


class AdoError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class AdoClient:
    """Azure DevOps work item REST client. All writes bypass process rules so
    original authors, dates and states can be set. The PAT's owner needs the
    'Bypass rules on work item updates' permission on the project."""

    def __init__(self, org_url, project, pat, api_version="7.1", session=None):
        self.org, self.project, self.v = (org_url or "").strip().rstrip("/"), project or "", api_version
        if self.org.lower().startswith("http://"):
            raise AdoError("Azure DevOps URL must use https:// so your access token is never sent over plain HTTP.")
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
        self.proj = f"{self.org}/{quote(self.project)}" if self.project else self.org

    def _request(self, method, url, **kw):
        params = dict(kw.pop("params", None) or {})
        params.setdefault("api-version", self.v)
        for attempt in range(8):
            if hasattr(kw.get("data"), "seek"):
                try:
                    kw["data"].seek(0)
                except Exception:
                    pass
            r = self.s.request(method, url, params=params, timeout=300, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                backoff_sleep(attempt, r.headers.get("Retry-After"))
                continue
            org_slug = self.org.rsplit("/", 1)[-1] if self.org else "target"
            token_hint = (f"Check that the token was pasted once, is for the {org_slug} organization, "
                          "has the Work Items (Read, write & manage) scope, and has not expired or been revoked.")
            if r.status_code == 203 or "text/html" in r.headers.get("Content-Type", ""):
                raise AdoError("Azure DevOps did not accept the access token "
                               f"(it sent back its sign-in page, HTTP {r.status_code}). " + token_hint, r.status_code)
            if r.status_code == 401:
                raise AdoError(f"Azure DevOps rejected the access token (HTTP 401): {_message(r)} " + token_hint, 401)
            if r.status_code == 403:
                raise AdoError(f"Azure DevOps refused this step (HTTP 403): {_message(r)} The token was accepted, "
                               "so the token's scope or its owner's permissions are missing something.", 403)
            if r.status_code >= 400:
                raise AdoError(f"ADO {method} {url} failed: {r.status_code} {r.text[:500]}", r.status_code)
            return r
        raise AdoError(f"ADO {method} {url} kept failing after retries")

    _patch = {"Content-Type": "application/json-patch+json"}
    _write = {"bypassRules": "true", "suppressNotifications": "true"}

    def create_item(self, wit, ops):
        url = f"{self.proj}/_apis/wit/workitems/${quote(wit)}"
        return self._request("POST", url, json=ops, headers=self._patch, params=dict(self._write)).json()

    def update_item(self, item_id, ops):
        url = f"{self.org}/_apis/wit/workitems/{item_id}"
        return self._request("PATCH", url, json=ops, headers=self._patch, params=dict(self._write)).json()

    def delete_item(self, item_id, destroy=False):
        """Delete a work item (moves to ADO Recycle Bin unless destroy=True)."""
        url = f"{self.proj}/_apis/wit/workitems/{item_id}"
        params = {"destroy": "true"} if destroy else {}
        return self._request("DELETE", url, params=params).json()

    def get_item(self, item_id):
        url = f"{self.org}/_apis/wit/workitems/{item_id}"
        return self._request("GET", url, params={"$expand": "relations"}).json()

    def comment_count(self, item_id):
        url = f"{self.proj}/_apis/wit/workItems/{item_id}/comments"
        return self._request("GET", url, params={"api-version": self.v + "-preview.4", "$top": 1}).json().get("totalCount", 0)

    def comments(self, item_id):
        """Return the list of existing comment texts (HTML strings) on a work item."""
        url = f"{self.proj}/_apis/wit/workItems/{item_id}/comments"
        out, token = [], None
        while True:
            params = {"api-version": self.v + "-preview.4", "$top": 200}
            if token:
                params["continuationToken"] = token
            data = self._request("GET", url, params=params).json()
            out.extend(c.get("text") or "" for c in (data.get("comments") or []))
            token = data.get("continuationToken")
            if not token:
                return out

    def upload_attachment(self, path, filename):
        url = f"{self.proj}/_apis/wit/attachments"
        with open(path, "rb") as fh:
            r = self._request("POST", url, data=fh, params={"fileName": filename},
                              headers={"Content-Type": "application/octet-stream"})
        return r.json()["url"]

    def item_url(self, item_id):
        return f"{self.org}/_apis/wit/workItems/{item_id}"

    def projects(self):
        """List all Azure DevOps projects in the organization: [{id, name, description}]."""
        try:
            data = self._request("GET", f"{self.org}/_apis/projects", params={"$top": 500}).json()
            items = data.get("value") or []
            return sorted(
                [{"id": p.get("id", ""), "name": p["name"], "description": p.get("description") or ""} for p in items if p.get("name")],
                key=lambda x: x["name"].lower(),
            )
        except AdoError:
            return []

    def teams(self):
        """List teams in this project: [{id, name}]."""
        if not self.project:
            return []
        try:
            data = self._request("GET", f"{self.org}/_apis/projects/{quote(self.project)}/teams", params={"$top": 200}).json()
            return [{"id": t["id"], "name": t["name"]} for t in (data.get("value") or []) if t.get("name")]
        except AdoError:
            return []

    def discover_users(self):
        """Discover ADO users (email -> identity, display_name -> unique identity) from project teams."""
        by_email, by_name, ambiguous_names = {}, {}, set()
        if not self.project:
            return {"by_email": by_email, "by_name": by_name}
        try:
            for team in self.teams()[:15]:
                r = self._request("GET", f"{self.org}/_apis/projects/{quote(self.project)}/teams/{quote(team['id'])}/members")
                for m in r.json().get("value") or []:
                    ident = m.get("identity") or m
                    unique = (ident.get("uniqueName") or "").strip()
                    disp = (ident.get("displayName") or "").strip()
                    if unique:
                        by_email[unique.lower()] = unique
                        if disp:
                            k = disp.lower()
                            if k in ambiguous_names:
                                continue
                            if k in by_name and by_name[k].lower() != unique.lower():
                                del by_name[k]
                                ambiguous_names.add(k)
                            else:
                                by_name[k] = unique
        except Exception:
            pass
        return {"by_email": by_email, "by_name": by_name}

    def work_item_types(self):
        """List work item type names available in this project."""
        data = self._request("GET", f"{self.proj}/_apis/wit/workitemtypes").json()
        return [w["name"] for w in (data.get("value") or []) if w.get("name") and not w.get("isDisabled")]

    def node_exists(self, group, path):
        """True if an Areas or Iterations path exists. path is below the project root; empty means the root."""
        here = "/".join(quote(p) for p in path.replace("/", "\\").split("\\") if p)
        url = f"{self.proj}/_apis/wit/classificationnodes/{group}" + (f"/{here}" if here else "")
        return self.s.get(url, params={"api-version": self.v}, timeout=60).status_code == 200

    def get_node(self, group, path):
        """Return the classification node dict (including 'identifier' GUID) for an Area or Iteration path."""
        here = "/".join(quote(p) for p in path.replace("/", "\\").split("\\") if p)
        url = f"{self.proj}/_apis/wit/classificationnodes/{group}" + (f"/{here}" if here else "")
        return self._request("GET", url).json()

    def _create_node(self, group, path, attributes=None):
        """Create every missing level of an Areas or Iterations path below the project root.
        Returns True if anything was created, False if the whole path was already there."""
        parts = [p for p in path.replace("/", "\\").split("\\") if p]
        made = False
        for i, name in enumerate(parts):
            parent = "/".join(quote(p) for p in parts[:i])
            url = f"{self.proj}/_apis/wit/classificationnodes/{group}" + (f"/{parent}" if parent else "")
            if self.node_exists(group, "\\".join(parts[: i + 1])):
                continue  # already there, possibly made by an admin
            body = {"name": name}
            if attributes and i == len(parts) - 1:
                body["attributes"] = attributes
            try:
                self._request("POST", url, json=body)
                made = True
            except AdoError as e:
                if e.status != 409 and "already exists" not in str(e) and "VS402371" not in str(e):
                    raise
        return made

    def create_iteration(self, name, start=None, finish=None, parent=None):
        attrs = {"startDate": start, "finishDate": finish} if start and finish else None
        return self._create_node("Iterations", f"{parent}\\{name}" if parent else name, attrs)

    def subscribe_team_iteration(self, rel_path, team=None):
        """Subscribe the project's team to an Iteration node so it immediately appears on the ADO Sprint Board."""
        try:
            node = self.get_node("Iterations", rel_path)
            ident = node.get("identifier")
            if not ident:
                return False
            url = (f"{self.org}/{quote(self.project)}/{quote(team)}/_apis/work/teamsettings/iterations"
                   if team else f"{self.proj}/_apis/work/teamsettings/iterations")
            self._request("POST", url, json={"id": ident})
            return True
        except Exception:
            return False

    def create_area(self, path):
        return self._create_node("Areas", path)

    def type_fields(self, wit):
        """Reference names of every field on a work item type in this project."""
        url = f"{self.proj}/_apis/wit/workitemtypes/{quote(wit)}/fields"
        return {f["referenceName"] for f in self._request("GET", url).json().get("value", [])}

    def check_access(self):
        """Confirm the token works for this project. Returns the signed-in name when Azure DevOps reports it."""
        try:
            self._request("GET", f"{self.proj}/_apis/wit/workitemtypes")
        except AdoError as e:
            if e.status == 404:
                raise AdoError(f"Azure DevOps project '{self.project}' was not found in the "
                               f"{self.org.rsplit('/', 1)[-1]} organization. Check the name given to --to.", 404)
            raise
        try:
            r = self.s.get(f"{self.org}/_apis/connectionData", timeout=60)
            user = r.json().get("authenticatedUser") or {}
            return user.get("providerDisplayName") or user.get("customDisplayName")
        except Exception:
            return None

    def whoami(self):
        """Check the token against the organization, without a project. Returns the signed-in name."""
        self._request("GET", f"{self.org}/_apis/wit/workitems", params={"ids": "1", "errorPolicy": "omit"})
        try:
            user = self._request("GET", f"{self.org}/_apis/connectionData").json().get("authenticatedUser") or {}
            return user.get("providerDisplayName") or user.get("customDisplayName")
        except (AdoError, ValueError):
            return None

    def _classification_paths(self, group, depth=6):
        root = self._request("GET", f"{self.proj}/_apis/wit/classificationnodes/{group}",
                             params={"$depth": depth}).json()
        out = []

        def walk(node, prefix):
            for child in node.get("children") or []:
                path = f"{prefix}\\{child['name']}" if prefix else child["name"]
                out.append(path)
                walk(child, path)

        walk(root, "")
        return out

    def iteration_paths(self, depth=6):
        """Every iteration below the project root, as paths like 'Team A\\Sprint 3'."""
        return self._classification_paths("Iterations", depth)

    def area_paths(self, depth=6):
        """Every area path below the project root."""
        return self._classification_paths("Areas", depth)

    def project_name(self):
        """The project's name exactly as Azure DevOps spells it. Read from the root iteration node,
        which has the project's name, so the Work Items scope is enough."""
        try:
            return self._request("GET", f"{self.proj}/_apis/wit/classificationnodes/Iterations").json()["name"]
        except AdoError as e:
            if e.status == 404:
                raise AdoError(f"Azure DevOps project '{self.project}' was not found in the "
                               f"{self.org.rsplit('/', 1)[-1]} organization. Check the name given to --to.", 404)
            raise

    def find_duplicates(self, entries, key_field=None, titles=True):
        """Items in this project that may already hold these Jira tickets. entries: [(key, summary)].
        Looks for the key as a tag, in key_field and in titles, and for titles equal to the summary.
        Returns {key: [match]}, see duplicates.classify."""
        from .duplicates import classify

        def q(text):
            return "'" + text.replace("'", "''") + "'"

        fields = ["System.Title", "System.Tags", "System.State", "System.WorkItemType", "System.IterationPath"]
        fields += [key_field] if key_field else []
        found, batch, conds = {}, [], []

        def run():
            if not conds:
                return
            query = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project AND ("
                     + " OR ".join(conds) + ")")
            ids = [w["id"] for w in self._request("POST", f"{self.proj}/_apis/wit/wiql",
                                                  json={"query": query}).json().get("workItems", [])]
            items = []
            for j in range(0, len(ids), 200):
                r = self._request("GET", f"{self.org}/_apis/wit/workitems", params={
                    "ids": ",".join(str(x) for x in ids[j:j + 200]), "fields": ",".join(fields),
                    "errorPolicy": "omit"}).json()
                items += [x for x in r.get("value") or [] if x]
            for key, matches in classify(batch, items, key_field, titles).items():
                found.setdefault(key, []).extend(matches)
            batch.clear()
            conds.clear()

        for key, summary in entries:
            mine = [f"[System.Tags] CONTAINS {q(key)}", f"[System.Title] CONTAINS {q(key)}"]
            if key_field:
                mine.append(f"[{key_field}] = {q(key)}")
            if titles:
                for t in sorted({(summary or "").strip()[:255], " ".join((summary or "").split())[:255]} - {""}):
                    mine.append(f"[System.Title] = {q(t)}")
            if conds and sum(len(c) + 4 for c in conds + mine) > 24000:  # WIQL allows 32,000 characters
                run()
            batch.append((key, summary))
            conds.extend(mine)
        run()
        return found

    def state_details(self, wit):
        """Return [{"name": state_name, "category": state_category}] for a work item type."""
        url = f"{self.proj}/_apis/wit/workitemtypes/{quote(wit)}/states"
        return [{"name": s["name"], "category": s.get("category", "")}
                for s in self._request("GET", url).json().get("value", []) if s.get("name")]

    def states(self, wit):
        return [s["name"] for s in self.state_details(wit)]
