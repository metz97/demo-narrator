"""Stage 2 — optional context gathering (Azure DevOps sprint/task, or a markdown file).

Azure DevOps access goes through the `az` CLI with the azure-devops extension,
authenticated via the AZURE_DEVOPS_EXT_PAT environment variable (PAT auth).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import AzureDevOpsConfig
from .errors import ContextError

logger = logging.getLogger(__name__)

_COMPLETED_STATES = ("Done", "Closed", "Resolved", "Completed")


def _strip_html(text: str) -> str:
    """Azure DevOps descriptions/acceptance criteria are HTML; flatten to text."""
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _az_binary() -> str:
    # On Windows the runnable entry point is az.cmd (the extension-less `az` is a
    # shell shim that CreateProcess cannot launch → WinError 193).
    binary = shutil.which("az.cmd") or shutil.which("az")
    if binary is None:
        raise ContextError(
            "Azure DevOps context requested but the 'az' CLI was not found.\n"
            "Install it (https://aka.ms/azure-cli), then: az extension add --name azure-devops"
        )
    return binary


def _run_az(args: list[str], pat: str) -> Any:
    binary = _az_binary()
    cmd = [binary, *args, "--output", "json"]
    # A .cmd/.bat batch file must be run through the command interpreter.
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    logger.debug("exec: az %s", " ".join(args))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "AZURE_DEVOPS_EXT_PAT": pat},
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-8:])
        raise ContextError(
            f"Azure DevOps CLI call failed (az {' '.join(args[:3])} ...):\n{tail}\n"
            "Check azure_devops.organization/project in config/config.yaml and that "
            "AZURE_DEVOPS_EXT_PAT holds a valid PAT with Work Items (read) scope."
        )
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise ContextError(f"Unexpected non-JSON output from az CLI: {exc}") from exc


def _format_work_items(items: list[dict[str, Any]], heading: str) -> str:
    lines = [f"# {heading}", ""]
    for item in items:
        fields = item.get("fields", {})
        title = fields.get("System.Title", "(untitled)")
        wi_id = item.get("id", "?")
        wi_type = fields.get("System.WorkItemType", "Work item")
        state = fields.get("System.State", "")
        lines.append(f"## [{wi_id}] {wi_type}: {title} ({state})")
        description = _strip_html(fields.get("System.Description", "") or "")
        if description:
            lines += ["", "### Description", description]
        criteria = _strip_html(
            fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
        )
        if criteria:
            lines += ["", "### Acceptance criteria", criteria]
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def fetch_sprint_context(cfg: AzureDevOpsConfig, sprint: int, pat: str) -> str:
    """Completed work items for a sprint, via a WIQL query on the iteration path."""
    if not cfg.organization or not cfg.project:
        raise ContextError(
            "--sprint requires azure_devops.organization and azure_devops.project "
            "to be set in config/config.yaml."
        )
    states = ", ".join(f"'{s}'" for s in _COMPLETED_STATES)
    wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{cfg.project}' "
        f"AND [System.IterationPath] UNDER '{cfg.project}' "
        f"AND [System.IterationPath] CONTAINS 'Sprint {sprint}' "
        f"AND [System.State] IN ({states}) "
        "ORDER BY [System.WorkItemType]"
    )
    result = _run_az(
        [
            "boards", "query",
            "--wiql", wiql,
            "--organization", cfg.organization,
            "--project", cfg.project,
        ],
        pat,
    )
    if not result:
        raise ContextError(
            f"No completed work items found for sprint {sprint} in project "
            f"{cfg.project!r}. Check the sprint number, or run without --sprint."
        )
    ids = [str(item["id"]) for item in result]
    logger.info("Sprint %d: %d completed work items", sprint, len(ids))
    items = [
        _run_az(
            [
                "boards", "work-item", "show",
                "--id", wi_id,
                "--organization", cfg.organization,
            ],
            pat,
        )
        for wi_id in ids
    ]
    return _format_work_items(items, f"Sprint {sprint} — completed work items")


def fetch_task_context(cfg: AzureDevOpsConfig, task_id: int, pat: str) -> str:
    """A single work item by id."""
    if not cfg.organization:
        raise ContextError(
            "--task requires azure_devops.organization to be set in config/config.yaml."
        )
    item = _run_az(
        [
            "boards", "work-item", "show",
            "--id", str(task_id),
            "--organization", cfg.organization,
        ],
        pat,
    )
    return _format_work_items([item], f"Work item {task_id}")


def gather_context(
    *,
    cfg: AzureDevOpsConfig,
    sprint: int | None,
    task: int | None,
    context_file: Path | None,
    pat: str | None,
) -> str | None:
    """Combine all requested context sources; None means vision-only narration."""
    parts: list[str] = []

    if context_file is not None:
        if not context_file.exists():
            raise ContextError(f"--context file not found: {context_file}")
        parts.append(context_file.read_text(encoding="utf-8").strip())

    if sprint is not None or task is not None:
        if not pat:
            logger.warning(
                "Sprint/task context requested but AZURE_DEVOPS_EXT_PAT is not set — "
                "skipping Azure DevOps lookup. Narration will be vision-only."
            )
        else:
            if sprint is not None:
                parts.append(fetch_sprint_context(cfg, sprint, pat))
            if task is not None:
                parts.append(fetch_task_context(cfg, task, pat))

    if not parts:
        logger.warning(
            "No sprint/task/context provided — narration will be based on visuals only. "
            "Narration quality is noticeably better with context (--sprint, --task or --context)."
        )
        return None
    return "\n\n---\n\n".join(parts)
