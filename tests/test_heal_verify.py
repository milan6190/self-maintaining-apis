import json

import pytest

from apimend.diff import RESPONSE, diff_fields
from apimend.heal import Healer, apply_plan, build_plan, coerce_value, merge_manual_ops
from apimend.schema import collect_values, flatten_samples
from apimend.verify import validate, verify_plan

OLD = [[{"id": 1, "name": "Rex", "age": 3, "owner": {"email": "a@x"}},
        {"id": 2, "name": "Milo", "age": 5, "owner": {"email": "b@x"}}],
       [{"id": 3, "name": "Luna", "age": 2, "owner": {"email": "c@x"}}]]
NEW = [[{"id": 1, "petName": "Rex", "age": "3", "owner": {"emailAddress": "a@x"}},
        {"id": 2, "petName": "Milo", "age": "5", "owner": {"emailAddress": "b@x"}}],
       [{"id": 3, "petName": "Luna", "age": "2", "owner": {"emailAddress": "c@x"}}]]


def make_plan(old=OLD, new=NEW):
    fd = diff_fields(flatten_samples(old), flatten_samples(new), direction=RESPONSE,
                     old_values=collect_values(old), new_values=collect_values(new), from_samples=True)
    return build_plan(fd, api="pets", endpoint="list")


def test_plan_restores_old_shape_and_verifies():
    plan = make_plan()
    assert {op["op"] for op in plan["ops"]} == {"rename", "coerce"}
    assert plan["unresolved"] == []
    healed = apply_plan(plan, NEW[0])
    assert healed == OLD[0]
    res = verify_plan(plan, NEW, flatten_samples(OLD))
    assert res.ok and res.passed == 2


def test_apply_does_not_mutate_input():
    plan = make_plan()
    original = json.loads(json.dumps(NEW[0]))
    apply_plan(plan, NEW[0])
    assert NEW[0] == original


def test_healer_from_file(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(make_plan()))
    assert Healer.from_file(p)(NEW[1]) == OLD[1]


def test_healer_rejects_future_plans():
    with pytest.raises(ValueError):
        Healer({"version": 99, "ops": []})


def test_removed_field_is_unresolved_until_defaulted():
    old = [{"a": 1, "tag": "x"}, {"a": 2, "tag": "y"}]
    new = [{"a": 1}, {"a": 2}]
    plan = make_plan(old, new)
    assert plan["unresolved"] and plan["unresolved"][0]["path"] == "$.tag"
    assert not verify_plan(plan, new, flatten_samples(old)).ok
    plan["ops"].append({"op": "default", "path": "$.tag", "value": "?"})
    assert verify_plan(plan, new, flatten_samples(old)).ok


def test_merge_manual_ops_keeps_defaults_and_clears_unresolved():
    fresh = {"ops": [{"op": "rename", "parent": "$", "from": "b", "to": "a"}],
             "unresolved": [{"path": "$.tag", "message": "gone"}]}
    prev = {"ops": [{"op": "default", "path": "$.tag", "value": None}]}
    merged = merge_manual_ops(prev, fresh)
    assert len(merged["ops"]) == 2 and merged["unresolved"] == []


def test_coerce_and_drop_ops():
    plan = {"ops": [{"op": "coerce", "path": "$.ids[]", "to": "integer"},
                    {"op": "drop", "path": "$.debug"}]}
    assert apply_plan(plan, {"ids": ["1", "2"], "debug": True}) == {"ids": [1, 2]}


def test_unknown_op_raises():
    with pytest.raises(ValueError):
        apply_plan({"ops": [{"op": "explode"}]}, {})


@pytest.mark.parametrize("value,to,expected", [
    ("3", "integer", 3), ("3.0", "integer", 3), (3, "string", "3"), (True, "string", "true"),
    ("2.5", "number", 2.5), ("yes", "boolean", True), (None, "integer", None),
])
def test_coerce_value(value, to, expected):
    assert coerce_value(value, to) == expected


def test_coerce_value_strict_raises():
    with pytest.raises(ValueError):
        coerce_value("abc", "integer", strict=True)


def test_validate_reports_missing_and_wrong_types():
    baseline = flatten_samples([{"a": 1, "b": {"c": "x"}}])
    assert validate({"a": 1, "b": {"c": "x"}}, baseline) == []
    errors = validate({"a": "1", "b": {}}, baseline)
    assert any("missing `$.b.c`" in e for e in errors)
    assert any("`$.a` is string" in e for e in errors)


def test_validate_accepts_integer_for_number_and_empty_lists():
    baseline = flatten_samples([{"x": 1.5, "items": [{"id": 1}]}])
    assert validate({"x": 2, "items": []}, baseline) == []
