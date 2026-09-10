"""Project profile: everything app-specific for demo mode.

One YAML per app (profiles/<name>.yaml). Secrets are referenced by env-var name
only; ``${VAR}`` / ``${VAR:-default}`` placeholders are expanded from the
environment at load time (never stored in the file).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ProfileError
from .models import Step, Viewport

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class DevServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    cwd: str | None = None
    url: str
    timeout_sec: int = 120


class LoginCredentialsEnv(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class LoginRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials_env: LoginCredentialsEnv | None = None
    steps: list[Step]


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "storage_state"           # storage_state | login_recipe
    role: str = "core"
    storage_state: str | None = None
    login_recipe: LoginRecipe | None = None
    # URL fragment that identifies the app's sign-in gate. Landing on a URL
    # containing this means the session is invalid and must be refreshed.
    sign_in_path: str = "/sign-in"


class Selectors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id_attribute: str = "data-testid"
    fallback: list[str] = Field(default_factory=lambda: ["role", "label", "placeholder", "text"])
    # testid of the app's loading spinner; the generator waits for it to disappear
    # before reading the page, so it never observes a half-loaded screen.
    loading_spinner: str = "loading-spinner"


class IssueTracker(BaseModel):
    model_config = ConfigDict(extra="allow")  # provider-specific keys vary
    provider: str = "azure_devops"


class SourceHints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framework: str | None = None
    repo_path: str | None = None
    router_file: str | None = None
    e2e_path: str | None = None


class ProjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    route_prefix: str = ""
    default_locale: str = "en"
    viewport: Viewport = Field(default_factory=Viewport)
    dev_server: DevServer | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    selectors: Selectors = Field(default_factory=Selectors)
    issue_tracker: IssueTracker | None = None
    source: SourceHints | None = None
    feature_map: dict[str, str] = Field(default_factory=dict)
    glossary_path: str | None = None
    style_path: str | None = None
    # Free-form description of the whole app: what it is, key terminology, the
    # main areas and how to navigate them, gotchas. Injected into every flow
    # generation so Claude understands the product, not just one task. Built and
    # refined by the onboarding assistant, but editable in the profile too.
    context: str | None = None

    def route(self, path: str, locale: str | None = None) -> str:
        """Full URL for a locale-free flow path."""
        loc = locale or self.default_locale
        prefix = self.route_prefix.replace("{locale}", loc)
        return self.base_url.rstrip("/") + prefix + path


def load_profile(path: Path) -> ProjectProfile:
    if not path.exists():
        raise ProfileError(
            f"Profile not found: {path}. Profiles live in profiles/<name>.yaml."
        )
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"Could not parse profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path} must contain a YAML mapping at the top level.")
    try:
        return ProjectProfile.model_validate(expand_env(raw))
    except ValidationError as exc:
        raise ProfileError(f"Invalid profile {path}:\n{exc}") from exc


def resolve_profile_path(project: str, profiles_dir: Path = Path("profiles")) -> Path:
    """`--project lca-tool` -> profiles/lca-tool.yaml (or an explicit path)."""
    p = Path(project)
    if p.suffix in {".yaml", ".yml"} and p.exists():
        return p
    return profiles_dir / f"{project}.yaml"
