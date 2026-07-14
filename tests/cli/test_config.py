from __future__ import annotations

from pathlib import Path

import pytest

from lazycode.cli.config import ConfigError, load_config


def test_defaults_with_no_config_files(tmp_path: Path):
    config = load_config(tmp_path, global_config_path_override=tmp_path / "no-such-global.toml")
    assert config.verify_command == "true"
    assert config.slider == 70
    assert config.keep_awake == "ask"
    assert config.resolve_model() == "claude-haiku-4-5"
    assert config.default_provider == "anthropic"


def test_repo_config_overrides_defaults(tmp_path: Path):
    (tmp_path / "lazycode.toml").write_text(
        '[verify]\ncommand = "pytest -q"\ncontainer = "python:3.12"\n\n'
        "[defaults]\nslider = 30\n"
        'model_map = { generate = "claude-sonnet-4-5" }\n\n'
        '[providers.anthropic]\nmodel_default = "claude-opus-4"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path, global_config_path_override=tmp_path / "no-such-global.toml")
    assert config.verify_command == "pytest -q"
    assert config.verify_container == "python:3.12"
    assert config.slider == 30
    assert config.model_map == {"generate": "claude-sonnet-4-5"}
    assert config.resolve_model() == "claude-opus-4"


def test_global_config_supplies_api_key_env_and_keep_awake(tmp_path: Path):
    global_path = tmp_path / "global.toml"
    global_path.write_text(
        '[providers.anthropic]\napi_key_env = "MY_CUSTOM_KEY"\n\n'
        "[daemon]\nkeep_awake = true\n\n"
        "[notify]\nenabled = true\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path, global_config_path_override=global_path)
    assert config.api_key_env_name() == "MY_CUSTOM_KEY"
    assert config.keep_awake is True
    assert config.notify == {"enabled": True}


def test_precedence_cli_flag_beats_repo_beats_global(tmp_path: Path):
    (tmp_path / "lazycode.toml").write_text('[verify]\ncommand = "repo-command"\n', encoding="utf-8")
    global_path = tmp_path / "global.toml"
    global_path.write_text("", encoding="utf-8")

    repo_only = load_config(tmp_path, global_config_path_override=global_path)
    assert repo_only.verify_command == "repo-command"

    cli_override = load_config(
        tmp_path, cli_verify_command="cli-command", global_config_path_override=global_path
    )
    assert cli_override.verify_command == "cli-command"


def test_provider_fields_merge_across_repo_and_global(tmp_path: Path):
    """Repo sets model_default, global sets api_key_env, for the SAME
    provider -- both should be visible (per-field merge, not per-provider
    override)."""
    (tmp_path / "lazycode.toml").write_text(
        '[providers.anthropic]\nmodel_default = "claude-opus-4"\n', encoding="utf-8"
    )
    global_path = tmp_path / "global.toml"
    global_path.write_text('[providers.anthropic]\napi_key_env = "ANOTHER_KEY"\n', encoding="utf-8")

    config = load_config(tmp_path, global_config_path_override=global_path)
    assert config.resolve_model() == "claude-opus-4"
    assert config.api_key_env_name() == "ANOTHER_KEY"


def test_require_api_key_raises_clear_error_naming_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = load_config(tmp_path, global_config_path_override=tmp_path / "no-such-global.toml")
    with pytest.raises(ConfigError) as excinfo:
        config.require_api_key()
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_require_api_key_succeeds_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    config = load_config(tmp_path, global_config_path_override=tmp_path / "no-such-global.toml")
    assert config.require_api_key() == "sk-test-123"


def test_require_api_key_names_custom_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    global_path = tmp_path / "global.toml"
    global_path.write_text('[providers.anthropic]\napi_key_env = "CUSTOM_ANTHROPIC_KEY"\n', encoding="utf-8")
    monkeypatch.delenv("CUSTOM_ANTHROPIC_KEY", raising=False)
    config = load_config(tmp_path, global_config_path_override=global_path)
    with pytest.raises(ConfigError) as excinfo:
        config.require_api_key()
    assert "CUSTOM_ANTHROPIC_KEY" in str(excinfo.value)


def test_to_scheduler_config_applies_overrides(tmp_path: Path):
    config = load_config(tmp_path, global_config_path_override=tmp_path / "no-such-global.toml")
    sched = config.to_scheduler_config(model="claude-opus-4", max_waves=3)
    assert sched.model == "claude-opus-4"
    assert sched.max_waves == 3
    assert sched.provider == "anthropic"
    assert sched.verify_command == "true"


def test_keep_awake_string_variants(tmp_path: Path):
    for raw, expected in (("true", True), ("false", False), ("ask", "ask"), ("weird", "ask")):
        global_path = tmp_path / f"global-{raw}.toml"
        global_path.write_text(f'[daemon]\nkeep_awake = "{raw}"\n', encoding="utf-8")
        config = load_config(tmp_path, global_config_path_override=global_path)
        assert config.keep_awake == expected
