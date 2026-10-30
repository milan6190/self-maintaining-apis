import json
from pathlib import Path

import pytest

from apimend.diff import BREAKING, INFO, WARNING
from apimend.gql import check as gql_check
from apimend.gql.diff import diff_graphql, safe_for_input, safe_for_output, type_tree
from apimend.gql.model import from_introspection, from_sdl
from apimend.gql.operations import (
    Edit, OperationSet, SourceDoc, apply_edits, extract_snippets, load_operations, plan_fixes, validate,
)
from apimend.gql.parser import (
    FieldNode, FragmentSpread, GraphQLSyntaxError, InlineFragment, document_kind, parse_operations, parse_schema,
    print_value,
)

SDL = '''
"""Docs"""
schema { query: Root mutation: Mut }
interface Node { id: ID! }
type Root {
  user(id: ID!): User
  users(first: Int = 10, filter: Filter = {role: ADMIN, tags: ["a"]}): [User!]!
  search(term: String!): [Result!]!
}
type Mut { createUser(input: NewUser!): User }
type User implements Node {
  id: ID!
  email: String!
  name: String @deprecated(reason: "use fullName")
  fullName: String
  role: Role
}
type Bot implements Node { id: ID! }
union Result = User | Bot
enum Role { ADMIN GUEST @deprecated }
input Filter { role: Role, tags: [String!] }
input NewUser { email: String!, role: Role = GUEST }
extend type User { age: Int }
'''


def model(sdl: str = SDL) -> dict:
    return from_sdl(sdl)


# ---------------------------------------------------------------- parsing


def test_sdl_model_shape():
    m = model()
    assert m["roots"] == {"query": "Root", "mutation": "Mut"}
    user = m["types"]["User"]
    assert user["interfaces"] == ["Node"]
    assert user["fields"]["name"]["deprecated"] == "use fullName"
    assert user["fields"]["age"]["type"] == "Int"  # from `extend type`
    assert m["types"]["Role"]["values"]["GUEST"] == {"deprecated": "No longer supported"}
    assert m["types"]["Node"]["possible"] == ["Bot", "User"]
    assert m["types"]["Root"]["fields"]["users"]["args"]["filter"]["default"] == '{ role: ADMIN, tags: ["a"] }'
    assert m["types"]["NewUser"]["inputs"]["role"]["default"] == "GUEST"
    assert m["types"]["String"] == {"kind": "SCALAR"}


def test_default_roots_and_legacy_implements():
    m = model("type Query { a: A } interface I { x: Int } interface J { y: Int } type A implements I, J { x: Int y: Int }")
    assert m["roots"] == {"query": "Query"}
    assert m["types"]["A"]["interfaces"] == ["I", "J"]


def test_block_strings_and_escapes():
    m = model('type Query { a: String @deprecated(reason: """\n    multi\n      line\n  """) '
              'b(x: String = "q\\"\\u00e9\\n"): Int }')
    assert m["types"]["Query"]["fields"]["a"]["deprecated"] == "multi\n  line"
    assert print_value(("string", 'q"é\n')) == '"q\\"é\\n"'
    assert m["types"]["Query"]["fields"]["b"]["args"]["x"]["default"] == '"q\\"é\\n"'


def test_introspection_and_sdl_agree():
    def ref(t):
        if t.endswith("!"):
            return {"kind": "NON_NULL", "ofType": ref(t[:-1])}
        if t.startswith("["):
            return {"kind": "LIST", "ofType": ref(t[1:-1])}
        return {"kind": "OBJECT", "name": t}

    intro = {"data": {"__schema": {
        "queryType": {"name": "Query"}, "mutationType": None, "subscriptionType": None,
        "types": [
            {"kind": "OBJECT", "name": "Query", "interfaces": [], "fields": [
                {"name": "items", "type": ref("[Item!]!"), "isDeprecated": False,
                 "args": [{"name": "first", "type": ref("Int"), "defaultValue": "5"}]}]},
            {"kind": "OBJECT", "name": "Item", "interfaces": [], "fields": [
                {"name": "id", "type": ref("ID!"), "args": [], "isDeprecated": True, "deprecationReason": "gone"}]},
            {"kind": "SCALAR", "name": "__Internal"},
        ]}}}
    sdl = 'type Query { items(first: Int = 5): [Item!]! } type Item { id: ID! @deprecated(reason: "gone") }'
    assert from_introspection(intro) == from_sdl(sdl)


def test_parse_operations():
    doc = parse_operations('query Q($id: ID!, $n: Int = 3) { u: user(id: $id) { ...F ... on User { email } } }'
                           ' fragment F on User { id }  { root }')
    assert [o.name for o in doc.operations] == ["Q", None]
    op = doc.operations[0]
    assert [(v.name, v.type) for v in op.variables] == [("id", "ID!"), ("n", "Int")]
    field = op.selections[0]
    assert isinstance(field, FieldNode) and field.alias == "u" and field.name == "user"
    assert isinstance(field.selections[0], FragmentSpread)
    assert isinstance(field.selections[1], InlineFragment) and field.selections[1].type_condition == "User"
    assert doc.fragments[0].name == "F"


def test_document_kind_and_errors():
    assert document_kind('"desc" type A { x: Int }') == "schema"
    assert document_kind("query { a }") == "operations"
    assert document_kind("{ a }") == "operations"
    assert document_kind("hello") is None
    with pytest.raises(GraphQLSyntaxError, match="operations, not a schema"):
        parse_schema("query { a }")
    with pytest.raises(GraphQLSyntaxError, match="line 2"):
        parse_operations("query {\n  a(")


# ------------------------------------------------------------------- diff


def kinds(changes):
    return {(c.kind, c.severity) for c in changes}


@pytest.mark.parametrize("old,new,out,inp", [
    ("String", "String!", True, False),
    ("String!", "String", False, True),
    ("[Int]", "[Int!]", True, False),
    ("[Int]", "Int", False, False),
    ("Int", "String", False, False),
])
def test_type_safety(old, new, out, inp):
    assert safe_for_output(type_tree(old), type_tree(new)) is out
    assert safe_for_input(type_tree(old), type_tree(new)) is inp


def test_schema_diff_rules():
    new = SDL.replace("email: String!", "emailAddress: String!") \
             .replace("fullName: String", "fullName: String @deprecated") \
             .replace("search(term: String!)", "search(term: String!, region: String!)") \
             .replace("users(first: Int = 10", "users(first: Int = 20") \
             .replace("enum Role { ADMIN GUEST @deprecated }", "enum Role { ADMIN GUEST @deprecated OWNER }") \
             .replace("union Result = User | Bot", "union Result = User") \
             .replace("input NewUser { email: String!, role: Role = GUEST }",
                      "input NewUser { email: String!, role: Role = GUEST, team: ID! }") \
             .replace("role: Role\n}", "role: Role!\n}")
    d = diff_graphql(model(), model(new))
    k = kinds(d.changes)
    assert ("field-renamed", BREAKING) in k
    assert [(r.type, r.old, r.new) for r in d.field_renames] == [("User", "email", "emailAddress")]
    assert ("deprecated", WARNING) in k
    assert ("required-argument-added", BREAKING) in k
    assert ("default-changed", WARNING) in k
    assert ("enum-value-added", WARNING) in k
    assert ("union-member-removed", BREAKING) in k
    assert ("required-input-field-added", BREAKING) in k
    assert ("field-type-changed", INFO) in k  # Role -> Role! is safe for readers


def test_root_rename_is_not_a_removal():
    old = model("type Query { a: Int }")
    new = model("schema { query: RootQuery } type RootQuery { a: Int }")
    assert diff_graphql(old, new).changes == []


def test_default_whitespace_is_ignored():
    old, new = model(), model()
    new["types"]["Root"]["fields"]["users"]["args"]["filter"]["default"] = '{role: ADMIN, tags: ["a"]}'
    assert diff_graphql(old, new).changes == []


# -------------------------------------------------------------- operations


def opset(text: str, file: str = "q.graphql") -> OperationSet:
    return OperationSet([SourceDoc(file, text, parse_operations(text))])


def errors(m, text):
    issues, _ = validate(m, opset(text))
    return [i.kind for i in issues if i.severity == "error"]


@pytest.mark.parametrize("q,expected", [
    ("query($id: ID!) { user(id: $id) { id email role } }", []),
    ("{ search(term: \"x\") { __typename ... on User { email } ... on Bot { id } } }", []),
    ("mutation { createUser(input: {email: \"a\", role: ADMIN}) { id } }", []),
    ("query($f: Filter) { users(filter: $f) { id } }", []),
    ("{ user(id: 1) { nope } }", ["unknown-field"]),
    ("{ user(id: 1) { id } users(frist: 1) { id } }", ["unknown-argument"]),
    ("{ user { id } }", ["missing-argument"]),
    ("{ user(id: 1) { email { x } } }", ["leaf-selection"]),
    ("{ user(id: 1) }", ["missing-selection"]),
    ("{ users(filter: {role: OWNER}) { id } }", ["unknown-enum-value"]),
    ("mutation { createUser(input: {role: ADMIN}) { id } }", ["missing-input-field"]),
    ("mutation { createUser(input: {email: \"a\", x: 1}) { id } }", ["unknown-input-field"]),
    ("query($id: String!) { user(id: $id) { id } }", ["variable-type-mismatch"]),
    ("query($id: ID) { user(id: $id) { id } }", ["variable-type-mismatch"]),
    ("query($id: Nope!) { user(id: $id) { id } }", ["unknown-type"]),
    ("{ search(term: \"x\") { id } }", ["unknown-field"]),
    ("{ user(id: 1) { ...Missing } }", ["unknown-fragment"]),
    ("{ user(id: $x) { id } }", ["unknown-variable"]),
    ("{ user(id: null) { id } }", ["null-value"]),
])
def test_validation(q, expected):
    assert errors(model(), q) == expected


def test_deprecated_usage_and_usage_map():
    issues, usage = validate(model(), opset(
        "query Q { user(id: 1) { name ...F } users(filter: {role: GUEST}) { id } } fragment F on User { email }"))
    assert {i.kind for i in issues if i.severity == "deprecated"} == {"deprecated-field", "deprecated-enum-value"}
    u = usage["query Q"]
    assert {"User.name", "User.email", "Root.user(id:)", "Filter.role", "Role.GUEST"} <= u.coordinates
    assert "Filter" in u.types


def test_extract_snippets_keep_offsets():
    ts = ('const A = gql`\n  query A { user(id: 1) { ...${F} } }\n  ${FRAGMENT}\n`;\n'
          'const B = graphql(`fragment X on ${T} { id }`);\n')
    snips = extract_snippets("x.ts", ts)
    assert len(snips) == 2
    for s in snips:
        assert len(s.text) == len(ts[s.offset:s.offset + len(s.text)])
    # names stand in where a name is expected, blanks where whole text was interpolated
    assert "...____ }" in snips[0].text and "\n  " + " " * len("${FRAGMENT}") + "\n" in snips[0].text
    assert snips[1].text == "fragment X on ____ { id }"
    m = model("type Query { user(id: ID): User } type User { id: ID }")
    assert errors(m, snips[0].text) == [] and errors(m, snips[1].text) == []
    py = 'q = gql("""\nquery P { a }\n""")\n'
    assert extract_snippets("x.py", py)[0].text.strip() == "query P { a }"


def test_operation_globs_support_exclusions(tmp_path):
    from apimend.gql.operations import expand_patterns

    for f in ("src/a.ts", "src/a.test.ts", "src/__tests__/b.ts", "a.ts"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("")
    got = expand_patterns(["src/**/*.ts", "*.ts", "!**/*.test.*", "!**/__tests__/**"], tmp_path)
    assert sorted(Path(f).relative_to(tmp_path).as_posix() for f in got) == ["a.ts", "src/a.ts"]


def test_load_operations_skips_schema_templates(tmp_path):
    f = tmp_path / "server.ts"
    f.write_text("const typeDefs = gql`type Query { a: Int }`;\nconst q = gql`query Q { a }`;\n")
    ops = load_operations([str(f)])
    assert [op.name for _, op in ops.operations] == ["Q"]


def test_alias_fix_for_renamed_field_and_arg_rename():
    old = model("type Query { items(pageSize: Int): [Item] } type Item { email: String title: String }")
    new = model("type Query { items(size: Int): [Item] } type Item { emailAddress: String title: String }")
    text = "query { items(pageSize: 2) { email t: email title } }"
    ops = opset(text)
    issues, _ = validate(new, ops)
    edits, unresolved = plan_fixes(issues, diff_graphql(old, new), {"q.graphql": text})
    assert not unresolved
    fixed = apply_edits(text, edits)
    assert fixed == "query { items(size: 2) { email: emailAddress t: emailAddress title } }"
    assert errors(new, fixed) == []


def test_apply_edits_refuses_moved_text():
    with pytest.raises(ValueError, match="re-run heal"):
        apply_edits("query { other }", [Edit("q", 8, 13, "email", "email: e", "")])


def test_fix_plan_survives_apply_and_manual_patch(tmp_path):
    old = model("type Query { a: Item } type Item { email: String name: String }")
    new = model("type Query { a: Item } type Item { emailAddress: String fullName: String }")
    f = tmp_path / "q.graphql"
    original = "query { a { email name } }\n"
    f.write_text(original)
    plan, patch = gql_check.heal(old, new, [str(f)], tmp_path, "x")
    assert len(plan["edits"]) == 2 and "+query { a { email: emailAddress name: fullName } }" in patch
    assert gql_check.verify(new, [str(f)], plan, tmp_path).ok  # pending edits are checked in memory
    gql_check.apply(plan, tmp_path)
    assert gql_check.verify(new, [str(f)], plan, tmp_path).ok  # already applied: not applied twice
    assert gql_check.apply(plan, tmp_path) == []
    f.write_text(original.replace("name", "nom"))
    with pytest.raises(ValueError, match="changed since"):
        gql_check.verify(new, [str(f)], plan, tmp_path)


def test_check_marks_breaks_and_only_used(tmp_path):
    f = tmp_path / "q.graphql"
    f.write_text("query Q { user(id: 1) { email } }\n")
    new_sdl = SDL.replace("email: String!", "emailAddress: String!").replace("type Bot implements Node { id: ID! }",
                                                                             "type Bot implements Node { id: ID! x: Int }")
    res = gql_check.check(model(), model(new_sdl), [str(f)], tmp_path)
    broken = [c for c in res.changes if c.kind == "operation-broken"]
    assert len(broken) == 1 and broken[0].healable and "q.graphql:1" in broken[0].location
    renamed = next(c for c in res.changes if c.kind == "field-renamed")
    assert renamed.used_by == ["query Q"]
    only = gql_check.check(model(), model(new_sdl), [str(f)], tmp_path, only_used=True).changes
    assert not any(c.kind == "field-added" for c in only)
    assert any(c.kind == "unused-changes-hidden" for c in only)


def test_check_invalid_at_baseline_is_a_warning(tmp_path):
    f = tmp_path / "q.graphql"
    f.write_text("{ user(id: 1) { nope } }")
    res = gql_check.check(model(), model(), [str(f)], tmp_path)
    assert [(c.kind, c.severity) for c in res.changes] == [("operation-invalid", WARNING)]


# -------------------------------------------------------------------- CLI


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "graphql-shop"


def test_graphql_example_loop(tmp_path, monkeypatch, capsys):
    import shutil

    from apimend.cli import main

    for name in ("apimend.yaml", "schema", "src"):
        src = EXAMPLE / name
        (shutil.copytree if src.is_dir() else shutil.copy)(src, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SHOP_SCHEMA", raising=False)
    assert main(["snapshot"]) == 0
    assert main(["check"]) == 0
    monkeypatch.setenv("SHOP_SCHEMA", "v2")
    assert main(["check", "--format", "json"]) == 1
    report = json.loads((tmp_path / ".apimend/report.json").read_text())
    by_kind = {c["kind"] for c in report["apis"][0]["changes"]}
    assert {"field-renamed", "argument-renamed", "operation-broken", "operation-uses-deprecated"} <= by_kind
    assert main(["heal"]) == 0
    plan = json.loads((tmp_path / ".apimend/graphql-fixes/shop.json").read_text())
    assert sorted(e["new"] for e in plan["edits"]) == ["email: emailAddress", "size"]
    assert len(plan["unresolved"]) == 1  # legacySku was removed
    assert main(["heal", "--apply"]) == 0
    assert "email: emailAddress" in (tmp_path / "src/queries/user.graphql").read_text()
    assert main(["verify"]) == 1
    ts = tmp_path / "src/products.ts"
    ts.write_text("\n".join(line for line in ts.read_text().splitlines() if "legacySku" not in line))
    assert main(["verify"]) == 0
    assert main(["verify"]) == 0  # repeatable: applied fixes are not re-applied
    assert main(["accept"]) == 0
    assert not (tmp_path / ".apimend/graphql-fixes/shop.json").exists()


def test_diff_command_graphql(tmp_path, capsys):
    from apimend.cli import main

    (tmp_path / "a.graphql").write_text("type Query { a: Int b: Int }")
    (tmp_path / "b.graphql").write_text("type Query { a: Int }")
    assert main(["diff", str(tmp_path / "a.graphql"), str(tmp_path / "b.graphql")]) == 1
    assert "`Query.b` was removed" in capsys.readouterr().out
