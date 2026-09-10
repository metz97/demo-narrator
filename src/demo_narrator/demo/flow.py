"""Load and strictly validate a flow spec (*.demoflow.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import FlowError
from .models import FlowSpec


def load_flow(path: Path) -> FlowSpec:
    if not path.exists():
        raise FlowError(f"Flow spec not found: {path}")
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FlowError(f"Could not parse flow spec {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FlowError(f"{path} must contain a YAML mapping at the top level.")
    try:
        return FlowSpec.model_validate(raw)
    except ValidationError as exc:
        raise FlowError(f"Invalid flow spec {path}:\n{exc}") from exc


def merge_flows(flows: list[FlowSpec], *, name: str, title: str) -> FlowSpec:
    """Combine several flows' beats into one (for a single multi-task video)."""
    if not flows:
        raise FlowError("No flows to merge.")
    projects = {f.project for f in flows}
    if len(projects) > 1:
        raise FlowError(f"Cannot merge flows from different projects: {projects}")
    beats = [beat for f in flows for beat in f.beats]
    first = flows[0]
    return FlowSpec(
        version=1, name=name, title=title, project=first.project,
        locale=first.locale, viewport=first.viewport, beats=beats,
    )


def save_flow(flow: FlowSpec, path: Path) -> Path:
    """Write a flow spec as readable YAML (only meaningful keys, source order)."""
    data = flow.model_dump(exclude_none=True, exclude_defaults=True)
    if not data.get("viewport"):  # drop empty/default viewport ({} noise)
        data.pop("viewport", None)
    data.setdefault("version", flow.version)
    data["name"] = flow.name
    data["project"] = flow.project
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Generated flow for {flow.title or flow.name}. Review before running:\n"
        f"#   demo-narrator demo --project {flow.project} --flow {path.as_posix()}\n\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path

