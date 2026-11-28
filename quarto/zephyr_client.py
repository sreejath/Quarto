"""100% read-only Zephyr Squad Cloud REST client with stdlib Atlassian Connect JWT (HS256 + QSH) signing."""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter

from .util import backoff_sleep

DEFAULT_SQUAD_URL = "https://prod-api.zephyr4jiracloud.com/connect"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonical_query(params):
    if not params:
        return ""
    items = sorted(params.items(), key=lambda kv: str(kv[0]))
    parts = []
    for k, v in items:
        if v is None:
            continue
        ek = quote(str(k), safe="-._~")
        if isinstance(v, (list, tuple)):
            ev = ",".join(quote(str(x), safe="-._~") for x in sorted(v, key=str))
        else:
            ev = quote(str(v), safe="-._~")
        parts.append(f"{ek}={ev}")
    return "&".join(parts)


def build_jwt(access_key, secret_key, account_id, method, relative_path, params=None, ttl=300, now=None):
    """Build an Atlassian Connect HS256 JWT with Query String Hash (QSH) for Zephyr Squad Cloud."""
    path = relative_path if relative_path.startswith("/") else "/" + relative_path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    qs = _canonical_query(params)
    canonical = f"{method.upper()}&{path}&{qs}"
    qsh = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    iat = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(account_id or ""),
        "qsh": qsh,
        "iss": str(access_key),
        "iat": iat,
        "exp": iat + int(ttl),
    }
    h_b64 = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(str(secret_key).encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url(sig)}"


class ZephyrError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ZephyrSquadClient:
    """Read-only Zephyr Squad Cloud REST client (GET requests only)."""

    def __init__(self, access_key, secret_key, account_id, base_url=DEFAULT_SQUAD_URL, session=None):
        self.base = (base_url or DEFAULT_SQUAD_URL).strip().rstrip("/")
        if self.base.lower().startswith("http://"):
            raise ZephyrError("Zephyr URL must use https:// so your API keys are never sent over plain HTTP.")
        if not access_key or not secret_key:
            raise ZephyrError("Zephyr Squad Access Key and Secret Key are required.")
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.account_id = (account_id or "").strip()
        if session is None:
            self.s = requests.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
            self.s.mount("https://", adapter)
            self.s.mount("http://", adapter)
        else:
            self.s = session

    def _get(self, relative_path, params=None, stream=False, allow_redirects=True):
        path = relative_path if relative_path.startswith("/") else "/" + relative_path
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        url = self.base + path
        for attempt in range(6):
            token = build_jwt(self.access_key, self.secret_key, self.account_id, "GET", path, clean_params)
            headers = {
                "Authorization": f"JWT {token}",
                "zapiAccessKey": self.access_key,
                "Accept": "application/json",
            }
            r = self.s.request(
                "GET", url, params=clean_params, headers=headers,
                timeout=120, stream=stream, allow_redirects=allow_redirects,
            )
            if r.status_code == 429 or r.status_code >= 500:
                backoff_sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code in (401, 403):
                raise ZephyrError(
                    f"Zephyr Squad rejected the API keys (HTTP {r.status_code}): {r.text[:300]}. "
                    "Verify your Zephyr Squad Access Key, Secret Key, and Jira user access to Zephyr Squad.",
                    r.status_code,
                )
            if r.status_code >= 400:
                raise ZephyrError(f"Zephyr GET {path} failed: {r.status_code} {r.text[:300]}", r.status_code)
            return r
        raise ZephyrError(f"Zephyr GET {path} kept failing after retries")

    def check_access(self, project_id=None):
        """Confirm the Zephyr Squad JWT keys are accepted by the API."""
        if project_id is not None:
            self._get("/public/rest/api/1.0/cycles/search", params={"projectId": project_id, "versionId": "-1"})
            return True
        try:
            self._get("/public/rest/api/1.0/config/generalinformation")
            return True
        except ZephyrError as e:
            if e.status == 404:
                self._get("/public/rest/api/1.0/cycles/search", params={"projectId": "-1", "versionId": "-1"})
                return True
            raise

    def test_steps(self, issue_id, project_id):
        """Return ordered Zephyr Squad test steps for a Jira Test issue."""
        try:
            data = self._get(f"/public/rest/api/1.0/teststep/{issue_id}", params={"projectId": project_id}).json()
        except ZephyrError as e:
            if e.status == 404:
                return []
            raise
        raw = data.get("stepBeanCollection") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        steps = []
        for idx, s in enumerate(raw, 1):
            if not isinstance(s, dict):
                continue
            order = s.get("orderId")
            try:
                order_int = int(order) if order is not None else idx
            except (TypeError, ValueError):
                order_int = idx
            steps.append({
                "id": str(s.get("id") if s.get("id") is not None else idx),
                "order": order_int,
                "step": s.get("step") or "",
                "data": s.get("data") or "",
                "result": s.get("result") or "",
                "html_step": s.get("htmlStep") or "",
                "html_data": s.get("htmlData") or "",
                "html_result": s.get("htmlResult") or "",
                "attachments": [],
            })
        steps.sort(key=lambda x: (x["order"], x["id"]))
        return steps

    def step_attachments(self, step_id, issue_id, project_id):
        """Return attachment metadata dicts for a single Zephyr test step."""
        try:
            data = self._get(
                "/public/rest/api/1.0/attachment/attachmentsByEntity",
                params={
                    "entityId": step_id,
                    "entityName": "TESTSTEP",
                    "issueId": issue_id,
                    "projectId": project_id,
                },
            ).json()
        except ZephyrError as e:
            if e.status in (400, 404):
                return []
            raise
        raw = data.get("data") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        out = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            fid = a.get("fileId") or a.get("id")
            fname = a.get("fileName") or a.get("name") or a.get("filename")
            if fid is not None and fname:
                out.append({
                    "id": str(fid),
                    "filename": str(fname),
                    "size": a.get("fileSize") or a.get("size"),
                })
        return out

    def download_attachment(self, att_id, step_id, issue_id, project_id, dest):
        """Download a Zephyr step attachment safely to `dest`, stripping auth on pre-signed redirects."""
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        r = self._get(
            f"/public/rest/api/1.0/attachment/{att_id}",
            params={
                "entityId": step_id,
                "entityName": "TESTSTEP",
                "issueId": issue_id,
                "projectId": project_id,
            },
            stream=True,
            allow_redirects=False,
        )
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            loc = r.headers["Location"]
            loc_p = urlparse(loc)
            base_p = urlparse(self.base)
            if not loc.startswith("http") or loc_p.netloc.lower() == base_p.netloc.lower():
                r = self._get(loc_p.path, stream=True)
            elif loc_p.scheme.lower() == "https":
                r = requests.get(loc, stream=True, timeout=120)
                if r.status_code >= 400:
                    raise ZephyrError(f"Zephyr attachment redirect failed: {r.status_code}", r.status_code)
            else:
                raise ZephyrError(f"Refusing insecure Zephyr attachment redirect: {loc}")
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    fh.write(chunk)
        os.replace(tmp, dest)

    def cycles(self, project_id, version_id="-1"):
        """Return Zephyr Squad test cycles for a project and versionId ('-1' = Unscheduled)."""
        try:
            data = self._get(
                "/public/rest/api/1.0/cycles/search",
                params={"projectId": project_id, "versionId": version_id},
            ).json()
        except ZephyrError as e:
            if e.status == 404:
                return []
            raise
        raw = data if isinstance(data, list) else (data.get("cycles") if isinstance(data, dict) else [])
        out = []
        for c in raw or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            name = (c.get("name") or "").strip()
            if cid is None or not name:
                continue
            out.append({
                "id": str(cid),
                "name": name,
                "description": c.get("description") or "",
                "startDate": c.get("startDate") or "",
                "endDate": c.get("endDate") or "",
                "versionId": str(c.get("versionId") if c.get("versionId") is not None else version_id),
                "versionName": c.get("versionName") or "",
            })
        return out

    def folders(self, cycle_id, project_id, version_id="-1"):
        """Return folders nested inside a Zephyr Squad test cycle."""
        try:
            data = self._get(
                "/public/rest/api/1.0/folders",
                params={"cycleId": cycle_id, "projectId": project_id, "versionId": version_id},
            ).json()
        except ZephyrError as e:
            if e.status in (400, 404):
                return []
            raise
        raw = data if isinstance(data, list) else (data.get("folders") if isinstance(data, dict) else [])
        out = []
        for f in raw or []:
            if not isinstance(f, dict):
                continue
            fid = f.get("id")
            name = (f.get("name") or "").strip()
            if fid is None or not name:
                continue
            out.append({
                "id": str(fid),
                "name": name,
                "description": f.get("description") or "",
            })
        return out

    def _paged_execution_keys(self, path, base_params):
        keys, seen = [], set()
        offset, size = 0, 50
        while True:
            params = dict(base_params, offset=offset, size=size)
            try:
                data = self._get(path, params=params).json()
            except ZephyrError as e:
                if e.status in (400, 404):
                    return keys
                raise
            rows = []
            if isinstance(data, dict):
                rows = data.get("searchObjectList") or data.get("executions") or []
                total = data.get("totalCount")
            elif isinstance(data, list):
                rows = data
                total = None
            else:
                total = 0
            if not rows:
                return keys
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = row.get("issueKey") or ((row.get("execution") or {}).get("issueKey"))
                if key and key not in seen:
                    seen.add(key)
                    keys.append(key)
            offset += len(rows)
            if len(rows) < size or (total is not None and offset >= int(total)):
                return keys

    def cycle_execution_keys(self, cycle_id, project_id, version_id="-1"):
        """Return ordered unique Jira issue keys assigned to a Zephyr Squad cycle."""
        return self._paged_execution_keys(
            f"/public/rest/api/1.0/executions/search/cycle/{cycle_id}",
            {"projectId": project_id, "versionId": version_id},
        )

    def folder_execution_keys(self, folder_id, cycle_id, project_id, version_id="-1"):
        """Return ordered unique Jira issue keys assigned to a Zephyr Squad folder."""
        return self._paged_execution_keys(
            f"/public/rest/api/1.0/executions/search/folder/{folder_id}",
            {"cycleId": cycle_id, "projectId": project_id, "versionId": version_id},
        )


def make_zephyr_client(cfg, jira=None):
    """Instantiate a ZephyrSquadClient from environment/.env if Zephyr is enabled and keys are set."""
    from . import config as cfg_mod
    zcfg = (cfg or {}).get("zephyr") or {}
    if not zcfg.get("enabled"):
        return None
    ak = cfg_mod.secret_optional("ZEPHYR_ACCESS_KEY")
    sk = cfg_mod.secret_optional("ZEPHYR_SECRET_KEY")
    if not (ak and sk):
        raise SystemExit(
            "Zephyr migration is enabled, but ZEPHYR_ACCESS_KEY and ZEPHYR_SECRET_KEY are not set. "
            "Add them to your local .env file or enter them on the Connect screen."
        )
    account_id = cfg_mod.secret_optional("ZEPHYR_ACCOUNT_ID")
    if not account_id and jira is not None:
        if getattr(jira, "myself_details", None):
            account_id = (jira.myself_details() or {}).get("accountId") or ""
    base_url = zcfg.get("base_url") or cfg_mod.secret_optional("ZEPHYR_BASE_URL") or DEFAULT_SQUAD_URL
    return ZephyrSquadClient(ak, sk, account_id, base_url=base_url)
