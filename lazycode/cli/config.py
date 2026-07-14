"""CLI config loading (DESIGN.md Appendix B2).

Two on-disk sources, merged with CLI flags:

* **Repo-local** ``<repo>/lazycode.toml`` (checked in, no secrets):
  ``[verify] command, container``; ``[defaults] slider, model_map``;
  ``[providers.<name>] model_default, ...``.
* **User-global** ``~/.config/lazycode/config.toml``: ``[providers.<name>]
  api_key_env`` (a reference to an environment variable, never the key
  itself), ``[notify] ...``, ``[daemon] keep_awake = "ask" | true | false``.

Precedence: **CLI flag > repo > global** (B2). A missing file at either layer
degrades to defaults, not an error — ``pipx install lazycode && lazycode run
...`` must work with zero config. A missing/unset API key env var is deferred
until :meth:`LazycodeConfig.require_api_key` is called (usually right before
constructing a provider adapter), so ``status``/``explain``/``review`` never
need a key at all.

Pure stdlib (``tomllib`` — read-only, which is all this module needs; nothing
here ever writes a config file).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from lazycode.scheduler import SchedulerConfig

KeepAwake = Literal["ask", "true", "false"]

_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"


class ConfigError(Exception):
    """A config value is missing/invalid in a way the user must fix.

    Used specifically for the "clear error message naming the env var" case
    (B2) — every other gap in config has a sensible default and never raises.
    """


@dataclass(frozen=True)
class ProviderConfig:
    """One ``[providers.<name>]`` block, repo + global merged."""

    model_default: str | None = None
    api_key_env: str = _DEFAULT_API_KEY_ENV


@dataclass(frozen=True)
class LazycodeConfig:
    """The merged, typed view of repo + global config + CLI overrides.

    Attributes mirror Appendix B2 directly. ``keep_awake`` is intentionally
    typed ``bool | Literal["ask"]`` (not a 3-way string enum) so callers can
    ``if config.keep_awake is True`` / ``== "ask"`` without an extra parse
    step — TOML booleans decode natively via ``tomllib``.
    """

    verify_command: str = "true"
    verify_container: str | None = None
    verify_timeout_s: float = 300.0
    slider: int = 70
    model_map: dict[str, str] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(
        default_factory=lambda: {_DEFAULT_PROVIDER: ProviderConfig(model_default=_DEFAULT_MODEL)}
    )
    default_provider: str = _DEFAULT_PROVIDER
    notify: dict[str, Any] = field(default_factory=dict)
    keep_awake: bool | Literal["ask"] = "ask"
    max_waves: int = 8

    # --- derived lookups ---------------------------------------------------

    def provider_config(self, provider: str | None = None) -> ProviderConfig:
        name = provider or self.default_provider
        return self.providers.get(name, ProviderConfig())

    def resolve_model(self, cli_model: str | None = None, provider: str | None = None) -> str:
        """CLI flag > provider's ``model_default`` > hardcoded fallback."""
        if cli_model:
            return cli_model
        pc = self.provider_config(provider)
        return pc.model_default or _DEFAULT_MODEL

    def api_key_env_name(self, provider: str | None = None) -> str:
        return self.provider_config(provider).api_key_env

    def require_api_key(self, provider: str | None = None) -> str:
        """Return the resolved API key, or raise :class:`ConfigError` naming
        the missing environment variable (B2's "clear error message")."""
        name = provider or self.default_provider
        env_name = self.api_key_env_name(name)
        value = os.environ.get(env_name)
        if not value:
            raise ConfigError(
                f"provider {name!r} requires environment variable {env_name!r}, "
                "which is not set. Export it, or point providers."
                f"{name}.api_key_env at a different variable in "
                "~/.config/lazycode/config.toml."
            )
        return value

    def to_scheduler_config(
        self, *, model: str | None = None, provider: str | None = None, max_waves: int | None = None
    ) -> SchedulerConfig:
        """Build the :class:`~lazycode.scheduler.SchedulerConfig` the
        orchestrator needs, applying any last-mile CLI overrides."""
        provider = provider or self.default_provider
        return SchedulerConfig(
            provider=provider,
            model=self.resolve_model(model, provider),
            verify_command=self.verify_command,
            verify_timeout_s=self.verify_timeout_s,
            max_waves=max_waves if max_waves is not None else self.max_waves,
        )


# --- raw TOML loading -------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def repo_config_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "lazycode.toml"


def global_config_path() -> Path:
    override = os.environ.get("LAZYCODE_GLOBAL_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "lazycode" / "config.toml"


def _providers_from_raw(*raws: dict[str, Any]) -> dict[str, ProviderConfig]:
    """Merge ``[providers.<name>]`` tables across layers (later layers win
    per-field, not per-provider — so repo can set ``model_default`` while
    global sets ``api_key_env`` for the same provider)."""
    merged: dict[str, dict[str, Any]] = {}
    for raw in raws:
        for name, block in (raw.get("providers") or {}).items():
            if not isinstance(block, dict):
                continue
            merged.setdefault(name, {}).update(block)
    if not merged:
        merged[_DEFAULT_PROVIDER] = {"model_default": _DEFAULT_MODEL}
    out: dict[str, ProviderConfig] = {}
    for name, block in merged.items():
        out[name] = ProviderConfig(
            model_default=block.get("model_default"),
            api_key_env=block.get("api_key_env", _DEFAULT_API_KEY_ENV),
        )
    return out


def load_config(
    repo_root: str | Path,
    *,
    cli_verify_command: str | None = None,
    global_config_path_override: str | Path | None = None,
) -> LazycodeConfig:
    """Load + merge repo-local and user-global config (B2 precedence: CLI
    flag > repo > global). ``cli_verify_command`` is the only flag this
    function itself applies — model/max-waves overrides are threaded through
    ``to_scheduler_config``/``resolve_model`` at call sites instead, since
    those also need a per-command default (e.g. ``run --model``) that this
    loader has no opinion about."""
    repo_raw = _load_toml(repo_config_path(repo_root))
    global_path = Path(global_config_path_override) if global_config_path_override else global_config_path()
    global_raw = _load_toml(global_path)

    verify_raw = repo_raw.get("verify") or {}
    defaults_raw = repo_raw.get("defaults") or {}
    daemon_raw = global_raw.get("daemon") or {}
    notify_raw = global_raw.get("notify") or {}

    keep_awake_raw = daemon_raw.get("keep_awake", "ask")
    keep_awake: bool | Literal["ask"]
    if isinstance(keep_awake_raw, bool):
        keep_awake = keep_awake_raw
    elif str(keep_awake_raw).lower() in ("true", "false"):
        keep_awake = str(keep_awake_raw).lower() == "true"
    else:
        keep_awake = "ask"

    return LazycodeConfig(
        verify_command=cli_verify_command or verify_raw.get("command", "true"),
        verify_container=verify_raw.get("container"),
        slider=int(defaults_raw.get("slider", 70)),
        model_map=dict(defaults_raw.get("model_map") or {}),
        providers=_providers_from_raw(repo_raw, global_raw),
        notify=dict(notify_raw),
        keep_awake=keep_awake,
    )
