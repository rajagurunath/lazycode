"""Tests for output contracts and context spec (ir/contracts.py, ir/context_spec.py)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from lazycode.ir import (
    CommandContract,
    ContextSpec,
    DiffContract,
    JsonContract,
    OutputContract,
)

_contract_adapter: TypeAdapter[OutputContract] = TypeAdapter(OutputContract)


def test_contract_union_discriminates_on_type():
    assert isinstance(_contract_adapter.validate_python({"type": "diff"}), DiffContract)
    assert isinstance(
        _contract_adapter.validate_python({"type": "command", "cmd": "pytest", "timeout_s": 60}),
        CommandContract,
    )
    assert isinstance(
        _contract_adapter.validate_python({"type": "json", "schema": {"type": "object"}}),
        JsonContract,
    )


def test_command_contract_defaults():
    c = CommandContract(cmd="pytest -q", timeout_s=120)
    assert c.expect_exit == 0
    assert c.type == "command"


def test_json_contract_schema_alias():
    c = JsonContract(schema={"type": "object"})
    assert c.json_schema == {"type": "object"}
    assert c.model_dump(by_alias=True)["schema"] == {"type": "object"}


def test_json_contract_populate_by_name():
    c = JsonContract(json_schema={"a": 1})
    assert c.json_schema == {"a": 1}


def test_contract_extra_field_rejected():
    with pytest.raises(ValidationError):
        DiffContract(files_within=["a"], bogus=1)


def test_unknown_contract_type_rejected():
    with pytest.raises(ValidationError):
        _contract_adapter.validate_python({"type": "nope"})


def test_context_spec_defaults():
    cs = ContextSpec()
    assert cs.files == []
    assert cs.repo_map is False
    assert cs.house_rules is False
    assert cs.extras == {}


def test_context_spec_extra_field_rejected():
    with pytest.raises(ValidationError):
        ContextSpec(files=["a"], unexpected=True)
