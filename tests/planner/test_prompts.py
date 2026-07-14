from __future__ import annotations

from lazycode.planner import operator_algebra_docs, planning_system_prompt


def test_algebra_is_generated_from_ir_schema():
    docs = operator_algebra_docs()
    # Every operator surfaces from the ir models.
    for op in ("Explore", "Decompose", "Generate", "Edit", "Verify", "Judge", "Reduce", "Gate"):
        assert op in docs
    # Per-operator required fields are pulled from model_fields, not hand-typed.
    assert "spec" in docs  # Generate.spec
    assert "context_spec" in docs  # Generate/Edit
    assert "output_contract" in docs
    assert "scope" in docs  # Explore.scope


def test_system_prompt_carries_m0_constraints_and_ledger():
    prompt = planning_system_prompt()
    assert "Do NOT emit Gate" in prompt
    assert "prefer_local" in prompt
    assert "assumptions" in prompt.lower()
    assert "emit_plan" in prompt
