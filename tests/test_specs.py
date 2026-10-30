import json

import pytest

from apimend.diff import BREAKING, diff_specs
from apimend.fetch import FetchError
from apimend.openapi import named_schemas, operations
from apimend.specs import kind_of, load_spec
from apimend.store import BASELINE, Store


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def multi_file_spec(root, owner_email_field="email"):
    write(root / "openapi.yaml", """
openapi: 3.0.3
info: {title: Pets, version: "1"}
paths:
  /pets:
    $ref: "./paths/pets.yaml"
components:
  schemas:
    Error: {type: object, properties: {message: {type: string}}}
""")
    write(root / "paths/pets.yaml", """
get:
  responses:
    "200":
      description: ok
      content:
        application/json:
          schema:
            type: array
            items: {$ref: "../schemas/pet.yaml#/Pet"}
    default:
      description: error
      content:
        application/json:
          schema: {$ref: "../openapi.yaml#/components/schemas/Error"}
""")
    write(root / "schemas/pet.yaml", f"""
Pet:
  type: object
  required: [id, owner]
  properties:
    id: {{type: integer}}
    owner: {{$ref: "#/Owner"}}
Owner:
  type: object
  properties:
    {owner_email_field}: {{type: string}}
""")
    return root / "openapi.yaml"


def test_bundles_files_and_refs_inside_them(tmp_path):
    doc = load_spec(str(multi_file_spec(tmp_path)))
    ops = operations(doc)
    assert list(ops) == ["GET /pets"]
    assert {"$", "$[]"} <= set(ops["GET /pets"].responses["200"])
    schemas = named_schemas(doc)
    assert {"Pet", "Owner", "Error"} <= set(schemas)
    # the ref back into the root document stays internal
    assert ops["GET /pets"].responses["default"]["$"].ref == "Error"


def test_change_in_a_referenced_file_is_found(tmp_path):
    old = load_spec(str(multi_file_spec(tmp_path / "a")))
    new = load_spec(str(multi_file_spec(tmp_path / "b", owner_email_field="emailAddress")))
    changes = diff_specs(old, new)
    assert any(c.kind == "field-renamed" and c.location == "schema Owner" and c.severity == BREAKING
               for c in changes)


def test_formats_are_detected(tmp_path):
    sdl = write(tmp_path / "schema.graphqls", "type Query { a: Int }")
    assert kind_of(load_spec(str(sdl))) == "graphql"
    sdl_no_ext = write(tmp_path / "schema.txt", '"""docs"""\ntype Query { a: Int }')
    assert kind_of(load_spec(str(sdl_no_ext))) == "graphql"
    intro = write(tmp_path / "intro.json", json.dumps({"data": {"__schema": {
        "queryType": {"name": "Query"}, "types": [{"kind": "OBJECT", "name": "Query", "fields": []}]}}}))
    assert kind_of(load_spec(str(intro))) == "graphql"
    oas = write(tmp_path / "api.yaml", "swagger: '2.0'\npaths: {}\n")
    assert kind_of(load_spec(str(oas))) == "openapi"
    with pytest.raises(FetchError, match="not an OpenAPI"):
        load_spec(str(write(tmp_path / "x.yaml", "hello: world\n")))
    with pytest.raises(FetchError, match="not found"):
        load_spec(str(tmp_path / "missing.yaml"))
    with pytest.raises(FetchError, match="invalid GraphQL"):
        load_spec(str(write(tmp_path / "bad.graphql", "type Query {")))


def test_large_specs_are_stored_gzipped(tmp_path):
    store = Store(tmp_path)
    small = {"openapi": "3.0.0", "paths": {}}
    assert store.save_spec(BASELINE, "a", small).name == "spec.json"
    big = {"openapi": "3.0.0", "paths": {f"/p{i}": {"get": {"description": "x" * 200}} for i in range(6000)}}
    path = store.save_spec(BASELINE, "a", big)
    assert path.name == "spec.json.gz" and not (path.parent / "spec.json").exists()
    assert store.load_spec(BASELINE, "a") == big
    assert path.stat().st_size < 200_000
    store.save_spec(BASELINE, "a", small)  # back to small: the .gz is removed
    assert store.load_spec(BASELINE, "a") == small and not path.exists()
