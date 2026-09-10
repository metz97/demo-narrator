"""Azure DevOps over REST — server-friendly work-item access.

Auth, in order of preference:
1. **PAT** (``AZURE_DEVOPS_EXT_PAT`` or the env var named by the profile's
   ``issue_tracker.pat_env``) — the server path; plain Basic auth.
2. **Entra ID via the local az CLI** — developer-machine fallback; acquires a
   bearer token from the existing ``az login`` session (no PAT needed). This
   mirrors what the old az-CLI provider relied on, without shelling out per call.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..context import _az_binary, _format_work_items
from ..errors import ContextError
from ..retry import RetryableError, with_backoff

logger = logging.getLogger(__name__)

_API_VERSION = "7.1"
# Well-known Azure DevOps resource id for AAD access-token requests.
_ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

_WORK_ITEM_FIELDS = [
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.IterationPath",
]


class _Unset:
    """Sentinel: the team's area-path scope has not been fetched yet."""


_UNSET = _Unset()


@dataclass(frozen=True)
class Sprint:
    id: str
    name: str
    path: str            # e.g. "Cirrus\\Sprint 174"
    timeframe: str | None  # past | current | future (as reported by the API)


@dataclass(frozen=True)
class WorkItemSummary:
    id: int
    title: str
    state: str
    work_item_type: str
    iteration_path: str


class AzureDevOpsTracker:
    """Minimal REST client for the sprint/work-item picking flow."""

    def __init__(
        self,
        organization: str,
        project: str,
        team: str = "",
        *,
        pat: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        org = organization.strip().rstrip("/")
        if not org:
            raise ContextError("Azure DevOps organization is not configured.")
        if not org.startswith(("http://", "https://")):
            org = f"https://dev.azure.com/{org}"
        self._org = org
        self._project = project
        self._team = team
        self._pat = pat or None
        self._bearer: str | None = None
        self._team_scope_cache: str | None | _Unset = _UNSET
        self._http = http or httpx.Client(timeout=30.0)

    # -- auth ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if self._pat:
            token = base64.b64encode(f":{self._pat}".encode()).decode("ascii")
            return {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if self._bearer is None:
            self._bearer = _aad_access_token(self._org_tenant())
        return {"Authorization": f"Bearer {self._bearer}", "Accept": "application/json"}

    def _org_tenant(self) -> str | None:
        """The Entra tenant backing this org (X-VSS-ResourceTenant header).

        Needed because `az account get-access-token` defaults to the current
        subscription's tenant, which may differ from the org's — yielding a
        valid-but-unauthorized token (TF400813)."""
        try:
            resp = self._http.get(self._org, follow_redirects=False)
            return resp.headers.get("X-VSS-ResourceTenant") or None
        except httpx.TransportError:
            return None

    # -- HTTP with retry ------------------------------------------------------

    def _request(self, method: str, url: str, json_body: dict[str, Any] | None = None) -> Any:
        def attempt() -> Any:
            try:
                resp = self._http.request(method, url, headers=self._headers(), json=json_body)
            except httpx.TransportError as exc:
                raise RetryableError(f"connection error: {exc}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableError(f"HTTP {resp.status_code} from Azure DevOps")
            if resp.status_code in (401, 403):
                raise ContextError(
                    f"Azure DevOps rejected the request ({resp.status_code}). "
                    "Check the PAT (Work Items: Read scope) or your az login session."
                )
            if resp.status_code >= 400:
                raise ContextError(
                    f"Azure DevOps error {resp.status_code} for {url}: {resp.text[:300]}"
                )
            return resp.json()

        return with_backoff(attempt, description=f"Azure DevOps {method} {url.split('?')[0]}")

    # -- API ------------------------------------------------------------------

    def list_sprints(self) -> list[Sprint]:
        """Iterations configured for the team (or the project's default team)."""
        segments = [self._org, quote(self._project)]
        if self._team:
            segments.append(quote(self._team))
        url = "/".join(segments) + f"/_apis/work/teamsettings/iterations?api-version={_API_VERSION}"
        data = self._request("GET", url)
        sprints = [
            Sprint(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                path=str(item.get("path", "")),
                timeframe=(item.get("attributes") or {}).get("timeFrame"),
            )
            for item in data.get("value", [])
        ]
        logger.info("Azure DevOps: %d sprints for %s/%s", len(sprints), self._project, self._team or "(default team)")
        return sprints

    def _team_scope(self) -> str | None:
        r"""WIQL clause restricting results to the configured team's area paths.

        Sprints are listed per team, but an iteration like ``Cirrus\Sprint 174``
        is shared by every team in the project — without this, picking a sprint
        returns the whole project's work, not this team's. ``None`` when no team
        is configured, or when the team-field lookup is unavailable."""
        if not self._team:
            return None
        segments = [self._org, quote(self._project), quote(self._team)]
        url = "/".join(segments) + f"/_apis/work/teamsettings/teamfieldvalues?api-version={_API_VERSION}"
        try:
            data = self._request("GET", url)
        except ContextError as exc:
            # Better to over-report than to fail the sprint listing outright.
            logger.warning("Azure DevOps: no team field values for team %r (%s)", self._team, exc)
            return None
        field = str((data.get("field") or {}).get("referenceName") or "System.AreaPath")
        tree = field.endswith("AreaPath")
        terms: list[str] = []
        for entry in data.get("values", []):
            value = str(entry.get("value") or "").strip()
            if not value:
                continue
            op = "UNDER" if tree and entry.get("includeChildren") else "="
            terms.append(f"[{field}] {op} '{_wiql_escape(value)}'")
        if not terms:
            return None
        logger.info("Azure DevOps: scoping work items to team %r via %s", self._team, field)
        return f"({' OR '.join(terms)})"

    def list_work_items(self, sprint: Sprint | str) -> list[WorkItemSummary]:
        """Work items in a sprint, scoped to the configured team's area paths.
        A bare string like "Sprint 174" is resolved to the matching iteration
        first — WIQL only allows =/UNDER on tree fields."""
        if isinstance(sprint, str):
            sprint = self._find_sprint(sprint)
        cond = f"[System.IterationPath] UNDER '{_wiql_escape(sprint.path)}'"
        if isinstance(self._team_scope_cache, _Unset):
            self._team_scope_cache = self._team_scope()
        if self._team_scope_cache:
            cond += f" AND {self._team_scope_cache}"
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{_wiql_escape(self._project)}' AND {cond} "
            "ORDER BY [System.WorkItemType]"
        )
        url = f"{self._org}/{quote(self._project)}/_apis/wit/wiql?api-version={_API_VERSION}"
        result = self._request("POST", url, {"query": wiql})
        ids = [int(item["id"]) for item in result.get("workItems", [])]
        if not ids:
            return []
        fields = ",".join(_WORK_ITEM_FIELDS)
        summaries: list[WorkItemSummary] = []
        for chunk_start in range(0, len(ids), 200):  # API caps ids per request
            chunk = ids[chunk_start : chunk_start + 200]
            url = (
                f"{self._org}/_apis/wit/workitems?ids={','.join(map(str, chunk))}"
                f"&fields={fields}&api-version={_API_VERSION}"
            )
            data = self._request("GET", url)
            for item in data.get("value", []):
                f = item.get("fields", {})
                summaries.append(
                    WorkItemSummary(
                        id=int(item.get("id", 0)),
                        title=str(f.get("System.Title", "")),
                        state=str(f.get("System.State", "")),
                        work_item_type=str(f.get("System.WorkItemType", "")),
                        iteration_path=str(f.get("System.IterationPath", "")),
                    )
                )
        return summaries

    def _find_sprint(self, name: str) -> Sprint:
        sprints = self.list_sprints()
        lowered = name.lower()
        exact = [s for s in sprints if s.name.lower() == lowered]
        partial = [s for s in sprints if lowered in s.name.lower()]
        match = exact or partial
        if not match:
            known = ", ".join(s.name for s in sprints[-8:]) or "(none)"
            raise ContextError(f"No sprint matching {name!r}. Known iterations include: {known}")
        return match[0]

    def get_work_item(self, item_id: int) -> dict[str, Any]:
        """Full work item (all fields), as raw JSON."""
        url = f"{self._org}/_apis/wit/workitems/{item_id}?api-version={_API_VERSION}"
        result: dict[str, Any] = self._request("GET", url)
        return result

    def work_item_text(self, item_id: int) -> str:
        """Markdown context for generation (same format the az-CLI path produced)."""
        return _format_work_items([self.get_work_item(item_id)], f"Work item {item_id}")


def _wiql_escape(value: str) -> str:
    return value.replace("'", "''")


def _aad_access_token(tenant: str | None = None) -> str:
    """Bearer token for Azure DevOps from the local az CLI session (dev fallback)."""
    binary = _az_binary()
    cmd = [binary, "account", "get-access-token", "--resource", _ADO_RESOURCE, "--output", "json"]
    if tenant:
        cmd += ["--tenant", tenant]
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-5:])
        raise ContextError(
            "No Azure DevOps PAT is set and acquiring an Entra ID token via "
            f"`az account get-access-token` failed:\n{tail}\n"
            "Set AZURE_DEVOPS_EXT_PAT (Work Items: Read) or run `az login`."
        )
    try:
        token = str(json.loads(proc.stdout)["accessToken"])
    except (json.JSONDecodeError, KeyError) as exc:
        raise ContextError(f"Unexpected az CLI token output: {exc}") from exc
    return token


def tracker_from_profile(profile: Any, *, pat: str | None = None) -> AzureDevOpsTracker:
    """Build a tracker from a ProjectProfile's issue_tracker block.

    ``pat`` overrides; otherwise the env var named by ``pat_env`` (default
    AZURE_DEVOPS_EXT_PAT) is consulted, then the az-CLI AAD fallback applies.
    """
    it = getattr(profile, "issue_tracker", None)
    if it is None:
        raise ContextError(f"Profile {getattr(profile, 'name', '?')!r} has no issue_tracker block.")
    provider = getattr(it, "provider", "azure_devops")
    if provider != "azure_devops":
        raise ContextError(f"Unsupported issue tracker provider {provider!r} (MVP: azure_devops).")
    org = getattr(it, "organization", "") or ""
    project = getattr(it, "project", "") or ""
    team = getattr(it, "team", "") or ""
    if not org or not project:
        raise ContextError(
            "issue_tracker.organization and issue_tracker.project must be set in the profile."
        )
    if pat is None:
        pat_env = getattr(it, "pat_env", None) or "AZURE_DEVOPS_EXT_PAT"
        pat = os.environ.get(pat_env) or None
    return AzureDevOpsTracker(org, project, team, pat=pat)
