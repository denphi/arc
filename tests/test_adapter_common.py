"""Shared runtime-adapter helpers (reconcile_inputs / filter_outputs).

These were lifted out of LocalRuntimeAdapter so Docker/Slurm/K8s adapters
reuse the same input/output handling. Pin the reconciliation rules.
"""

from __future__ import annotations

import pytest

from arc.runtime._adapter_common import filter_outputs, reconcile_inputs

pytestmark = pytest.mark.chat


def test_reconcile_overlays_caller_inputs_on_defaults():
    schema = {"x": {"default": 1.0}, "y": {"default": 2.0}}
    assert reconcile_inputs(schema, {"x": 5.0}) == {"x": 5.0, "y": 2.0}


def test_reconcile_drops_keys_not_in_schema():
    schema = {"x": {"default": 1.0}}
    # 'z' isn't declared → dropped (prevents "unexpected fields").
    assert reconcile_inputs(schema, {"x": 3.0, "z": 9.0}) == {"x": 3.0}


def test_reconcile_passes_through_when_no_schema():
    # Schema-less artifact accepts arbitrary inputs.
    assert reconcile_inputs({}, {"anything": 1, "else": 2}) == {"anything": 1, "else": 2}


def test_reconcile_omits_fields_with_no_declared_default():
    """A field that declares no default is omitted — never given a
    fabricated value (1.0 is nonsense for Text/Boolean inputs; let
    simulate()'s own signature default apply)."""
    schema = {"x": {}, "element": {"type": "Text"}}
    assert reconcile_inputs(schema, {}) == {}
    # The declared-but-defaultless key still accepts caller values.
    assert reconcile_inputs(schema, {"element": "Si"}) == {"element": "Si"}


def test_reconcile_tolerates_non_dict_field_spec():
    schema = {"x": "not-a-dict"}
    # Declared (so caller values pass the filter) but contributes no default.
    assert reconcile_inputs(schema, {}) == {}
    assert reconcile_inputs(schema, {"x": 3.0}) == {"x": 3.0}


def test_filter_outputs_projects_onto_schema_keys():
    out_schema = {"z": {}, "w": {}}
    assert filter_outputs(out_schema, {"z": 1.0, "extra": 9}) == {"z": 1.0, "w": None}


def test_filter_outputs_passthrough_when_no_schema():
    assert filter_outputs({}, {"a": 1, "b": 2}) == {"a": 1, "b": 2}
