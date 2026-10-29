from apimend.diff import (
    BREAKING, INFO, REQUEST, RESPONSE, WARNING, diff_fields, diff_specs, name_similarity, worst,
)
from apimend.schema import collect_values, flatten, flatten_samples


def kinds(changes):
    return {(c.kind, c.severity) for c in changes}


def test_name_similarity():
    assert name_similarity("user_id", "userId") == 1.0
    assert name_similarity("email", "emailAddress") >= 0.8
    assert name_similarity("price", "quantity") < 0.6


def sample_diff(old, new, **kw):
    return diff_fields(flatten_samples(old), flatten_samples(new), direction=RESPONSE,
                       old_values=collect_values(old), new_values=collect_values(new),
                       from_samples=True, **kw)


def test_detects_rename_by_name():
    fd = sample_diff([{"user_id": 1}], [{"userId": 1}])
    assert [(r.old_name, r.new_name) for r in fd.renames] == [("user_id", "userId")]
    assert fd.changes[0].kind == "field-renamed" and fd.changes[0].healable


def test_detects_rename_by_values_when_names_differ():
    old = [{"label": v} for v in ("alpha", "beta", "gamma", "delta")]
    new = [{"title": v} for v in ("alpha", "beta", "gamma", "delta")]
    fd = sample_diff(old, new)
    assert [(r.old_name, r.new_name) for r in fd.renames] == [("label", "title")]


def test_value_match_needs_enough_distinct_values():
    fd = sample_diff([{"active": True}], [{"enabled_flag": True}])
    assert not fd.renames
    assert ("field-removed", BREAKING) in kinds(fd.changes)


def test_nested_rename_under_renamed_parent():
    old = [{"owner": {"email": "a@x"}}, {"owner": {"email": "b@x"}}]
    new = [{"ownerInfo": {"emailAddress": "a@x"}}, {"ownerInfo": {"emailAddress": "b@x"}}]
    fd = sample_diff(old, new)
    assert [(r.parent, r.old_name, r.new_name) for r in fd.renames] == [
        ("$", "owner", "ownerInfo"), ("$.owner", "email", "emailAddress"),
    ]
    assert not [c for c in fd.changes if c.kind == "field-removed"]


def test_type_change_is_healable_when_values_convert():
    fd = sample_diff([{"age": 3}], [{"age": "3"}])
    c = next(c for c in fd.changes if c.kind == "type-changed")
    assert c.severity == BREAKING and c.healable
    assert fd.coercions[0].to_type == "integer"


def test_type_change_not_healable_when_values_dont_convert():
    fd = sample_diff([{"age": 3}], [{"age": "three"}])
    c = next(c for c in fd.changes if c.kind == "type-changed")
    assert not c.healable


def test_now_nullable_is_a_warning():
    fd = sample_diff([{"a": "x"}], [{"a": "x"}, {"a": None}])
    assert ("now-nullable", WARNING) in kinds(fd.changes)


def test_added_field_is_info():
    fd = sample_diff([{"a": 1}], [{"a": 1, "b": 2}])
    assert kinds(fd.changes) == {("field-added", INFO)}


def test_request_direction_rules():
    old = flatten({"type": "object", "properties": {"a": {"type": "string"}}})
    new = flatten({"type": "object", "required": ["a", "b"],
                   "properties": {"a": {"type": "string"}, "b": {"type": "string"}}})
    fd = diff_fields(old, new, direction=REQUEST)
    assert ("required-field-added", BREAKING) in kinds(fd.changes)
    assert ("now-required", BREAKING) in kinds(fd.changes)


def test_enum_values_added_warns_for_responses():
    old = flatten({"type": "string", "enum": ["a", "b"]})
    new = flatten({"type": "string", "enum": ["a", "b", "c"]})
    assert ("enum-values-added", WARNING) in kinds(diff_fields(old, new).changes)
    assert ("enum-values-added", INFO) in kinds(diff_fields(old, new, direction=REQUEST).changes)
    assert ("enum-values-removed", BREAKING) in kinds(diff_fields(new, old, direction=REQUEST).changes)


def _spec(paths, schemas=None):
    return {"openapi": "3.0.0", "paths": paths, "components": {"schemas": schemas or {}}}


def test_spec_diff_operations_and_params():
    op = {"responses": {"200": {"description": "ok"}}}
    old = _spec({"/a": {"get": op}, "/b": {"delete": op}})
    new = _spec({"/a": {"get": {**op, "parameters": [
        {"name": "region", "in": "query", "required": True, "schema": {"type": "string"}}]}},
        "/c": {"get": op}})
    ch = kinds(diff_specs(old, new))
    assert ("operation-removed", BREAKING) in ch
    assert ("operation-added", INFO) in ch
    assert ("required-param-added", BREAKING) in ch


def test_spec_diff_response_rename_via_ref():
    def doc(prop):
        return _spec({"/u": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/U"}}}}}}}},
            {"U": {"type": "object", "required": [prop], "properties": {prop: {"type": "string"}}}})
    ch = diff_specs(doc("user_name"), doc("userName"))
    assert ch[0].kind == "field-renamed"


def test_swagger2_body_param():
    def doc(required):
        return {"swagger": "2.0", "paths": {"/p": {"post": {
            "parameters": [{"in": "body", "name": "b", "schema": {
                "type": "object", "required": required,
                "properties": {"x": {"type": "string"}, "y": {"type": "string"}}}}],
            "responses": {"200": {"description": "ok", "schema": {"type": "string"}}}}}}}
    assert ("now-required", BREAKING) in kinds(diff_specs(doc(["x"]), doc(["x", "y"])))


def test_worst():
    assert worst([]) is None
    fd = sample_diff([{"a": 1}], [{"b": "zzz"}])
    assert worst(fd.changes) == BREAKING


def test_shared_null_does_not_make_types_compatible_for_renames():
    old = flatten({"type": "object", "properties": {"countries": {"type": "array", "nullable": True,
                                                                  "items": {"type": "string"}}}})
    new = flatten({"type": "object", "properties": {"country": {"type": "string", "nullable": True}}})
    fd = diff_fields(old, new)
    assert not fd.renames


def test_shared_schema_graded_by_where_it_is_used():
    def doc(props, required):
        ref = {"$ref": "#/components/schemas/Thing"}
        body = {"content": {"application/json": {"schema": ref}}}
        return _spec(
            {"/in": {"post": {"requestBody": body, "responses": {"204": {"description": "ok"}}}},
             "/out": {"get": {"responses": {"200": {"description": "ok", **body}}}}},
            {"Thing": {"type": "object", "required": required, "properties": props}})
    old = doc({"a": {"type": "string"}}, [])
    new = doc({"a": {"type": "string"}, "b": {"type": "string"}}, ["b"])
    ch = [c for c in diff_specs(old, new) if c.location == "schema Thing"]
    # used in a request too, so a new required field is breaking
    assert ("required-field-added", BREAKING) in kinds(ch)
    assert len([c for c in ch if c.path == "$.b"]) == 1  # reported once, not per direction


def test_large_spec_stays_linear():
    # 60 schemas that all reference each other would explode if refs were inlined
    schemas = {f"S{i}": {"type": "object", "properties": {
        f"p{j}": {"$ref": f"#/components/schemas/S{j}"} for j in range(60) if j != i}} for i in range(60)}
    spec = _spec({"/x": {"get": {"responses": {"200": {"content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/S0"}}}}}}}}, schemas)
    assert diff_specs(spec, spec) == []
