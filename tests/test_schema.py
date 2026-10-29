from apimend.schema import (
    collect_values, flatten, flatten_samples, infer, parent, rebase, split_path, join_path,
)


def test_paths_round_trip():
    assert split_path("$") == []
    assert split_path("$.a.b[].c") == ["a", "b", "[]", "c"]
    assert join_path(["a", "[]", "b"]) == "$.a[].b"
    assert parent("$.a[].b") == "$.a[]"
    assert parent("$.a[]") == "$.a"
    assert parent("$") is None


def test_rebase_respects_segment_boundaries():
    assert rebase("$.ab.c", "$.a", "$.x") == "$.ab.c"
    assert rebase("$.a.c", "$.a", "$.x") == "$.x.c"
    assert rebase("$.a[].c", "$.a", "$.x") == "$.x[].c"


def test_infer_marks_fields_optional_when_missing_from_some_samples():
    fields = flatten_samples([{"id": 1, "nick": "a"}, {"id": 2}])
    assert fields["$.id"].required
    assert not fields["$.nick"].required


def test_infer_merges_integer_and_number():
    fields = flatten_samples([{"x": 1}, {"x": 1.5}])
    assert fields["$.x"].types == frozenset({"number"})


def test_infer_arrays_of_objects():
    fields = flatten_samples([[{"a": 1, "b": None}, {"a": 2, "b": "x"}]])
    assert fields["$"].types == {"array"}
    assert fields["$[].a"].types == {"integer"}
    assert fields["$[].b"].types == {"null", "string"}


def test_empty_list_has_no_item_paths():
    fields = flatten(infer([{"items": []}]))
    assert "$.items" in fields and "$.items[]" not in fields


def test_flatten_openapi_refs_allof_nullable_enum():
    doc = {
        "components": {"schemas": {
            "Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
            "Pet": {"allOf": [
                {"$ref": "#/components/schemas/Base"},
                {"type": "object", "properties": {
                    "status": {"type": "string", "enum": ["a", "b"]},
                    "nick": {"type": "string", "nullable": True},
                }},
            ]},
        }}
    }

    def resolve(ref):
        node = doc
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    f = flatten({"$ref": "#/components/schemas/Pet"}, resolve)
    assert f["$.id"].required and f["$.id"].types == {"integer"}
    assert f["$.status"].enum == ("a", "b")
    assert f["$.nick"].types == {"null", "string"}
    assert not f["$.nick"].required


def test_flatten_survives_recursive_refs():
    doc = {"Node": {"type": "object", "properties": {"child": {"$ref": "#/Node"}}}}
    f = flatten({"$ref": "#/Node"}, lambda r: doc["Node"])
    assert "$.child" in f


def test_oneof_unions_types_and_intersects_required():
    f = flatten({"oneOf": [
        {"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}},
        {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}},
    ]})
    assert f["$.a"].required
    assert not f["$.b"].required


def test_collect_values():
    vals = collect_values([[{"a": 1}, {"a": 2}], [{"a": None}]])
    assert vals["$[].a"] == [1, 2]


def test_anyof_enum_only_constrains_when_every_branch_does():
    open_union = flatten({"anyOf": [{"type": "array", "items": {"type": "string"}},
                                    {"type": "string", "enum": [""]}]})
    assert open_union["$"].enum is None
    closed = flatten({"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["b"]}]})
    assert closed["$"].enum == ("a", "b")
