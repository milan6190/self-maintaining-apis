"""A small, dependency-free GraphQL lexer and parser.

It reads both kinds of GraphQL document:

* type-system documents (SDL): ``type``, ``interface``, ``enum``, ``input``,
  ``union``, ``scalar``, ``schema``, ``directive`` and ``extend``
* executable documents: queries, mutations, subscriptions and fragments

Every token keeps its character offset so callers can patch the source text
(see :mod:`apimend.gql.operations`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PUNCTUATORS = set("!$&()[]{}:=@|")
TYPE_SYSTEM_KEYWORDS = {"schema", "scalar", "type", "interface", "union", "enum", "input", "directive", "extend"}
EXECUTABLE_KEYWORDS = {"query", "mutation", "subscription", "fragment"}


class GraphQLSyntaxError(ValueError):
    def __init__(self, message: str, offset: int, source: str = ""):
        line = source.count("\n", 0, offset) + 1 if source else 0
        super().__init__(f"{message} (line {line})" if line else message)
        self.offset = offset
        self.line = line


@dataclass(slots=True)
class Token:
    kind: str  # name | int | float | string | punct | spread | eof
    value: Any
    start: int
    end: int
    after_comma: bool = False


_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _block_string_value(raw: str) -> str:
    """The GraphQL spec's BlockStringValue: drop common indentation and blank edges."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    indent = None
    for line in lines[1:]:
        stripped = line.lstrip(" \t")
        if stripped:
            n = len(line) - len(stripped)
            indent = n if indent is None else min(indent, n)
    if indent:
        lines = [lines[0]] + [line[indent:] for line in lines[1:]]
    while lines and not lines[0].strip(" \t"):
        lines.pop(0)
    while lines and not lines[-1].strip(" \t"):
        lines.pop()
    return "\n".join(lines)


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(source)
    comma = False
    while i < n:
        ch = source[i]
        if ch in " \t\r\n﻿":
            i += 1
            continue
        if ch == ",":
            comma = True
            i += 1
            continue
        if ch == "#":
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        start = i
        if ch in PUNCTUATORS:
            tokens.append(Token("punct", ch, i, i + 1, comma))
            i += 1
        elif source.startswith("...", i):
            tokens.append(Token("spread", "...", i, i + 3, comma))
            i += 3
        elif source.startswith('"""', i):
            j = i + 3
            buf = []
            while True:
                if j >= n:
                    raise GraphQLSyntaxError("unterminated block string", start, source)
                if source.startswith('\\"""', j):
                    buf.append('"""')
                    j += 4
                elif source.startswith('"""', j):
                    j += 3
                    break
                else:
                    buf.append(source[j])
                    j += 1
            tokens.append(Token("string", _block_string_value("".join(buf)), start, j, comma))
            i = j
        elif ch == '"':
            j = i + 1
            buf = []
            while True:
                if j >= n or source[j] == "\n":
                    raise GraphQLSyntaxError("unterminated string", start, source)
                c = source[j]
                if c == '"':
                    j += 1
                    break
                if c == "\\":
                    esc = source[j + 1: j + 2]
                    if esc in _ESCAPES:
                        buf.append(_ESCAPES[esc])
                        j += 2
                    elif esc == "u":
                        if source[j + 2: j + 3] == "{":
                            k = source.find("}", j + 3)
                            buf.append(chr(int(source[j + 3:k], 16)))
                            j = k + 1
                        else:
                            buf.append(chr(int(source[j + 2: j + 6], 16)))
                            j += 6
                    else:
                        raise GraphQLSyntaxError(f"invalid escape \\{esc}", j, source)
                else:
                    buf.append(c)
                    j += 1
            tokens.append(Token("string", "".join(buf), start, j, comma))
            i = j
        elif ch == "-" or ch.isdigit():
            m = _NUMBER.match(source, i)
            if not m:
                raise GraphQLSyntaxError(f"invalid number near {source[i:i + 10]!r}", i, source)
            text = m.group(0)
            kind = "float" if (m.group(2) or m.group(3)) else "int"
            tokens.append(Token(kind, text, i, m.end(), comma))
            i = m.end()
        else:
            m = _NAME.match(source, i)
            if not m:
                raise GraphQLSyntaxError(f"unexpected character {ch!r}", i, source)
            tokens.append(Token("name", m.group(0), i, m.end(), comma))
            i = m.end()
        comma = False
    tokens.append(Token("eof", None, n, n))
    return tokens


# --------------------------------------------------------------------- values

# Values are tuples: ("var", name) ("int", text) ("float", text) ("string", s)
# ("bool", b) ("null", None) ("enum", name) ("list", [values]) ("object", {k: value})


def print_value(v: tuple) -> str:
    """Canonical text for a value, matching how servers report ``defaultValue``."""
    kind, val = v
    if kind == "var":
        return f"${val}"
    if kind in ("int", "float", "enum"):
        return val
    if kind == "string":
        out = ['"']
        for c in val:
            if c == '"':
                out.append('\\"')
            elif c == "\\":
                out.append("\\\\")
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            elif ord(c) < 0x20:
                out.append(f"\\u{ord(c):04X}")
            else:
                out.append(c)
        out.append('"')
        return "".join(out)
    if kind == "bool":
        return "true" if val else "false"
    if kind == "null":
        return "null"
    if kind == "list":
        return "[" + ", ".join(print_value(x) for x in val) + "]"
    if kind == "object":
        if not val:
            return "{}"
        return "{ " + ", ".join(f"{k}: {print_value(x)}" for k, x in val.items()) + " }"
    raise ValueError(kind)


# ------------------------------------------------------------------ AST (ops)


@dataclass
class Argument:
    name: str
    value: tuple
    start: int  # offset of the argument name
    value_start: int = 0


@dataclass
class FieldNode:
    name: str
    alias: str | None
    args: list[Argument]
    selections: list | None
    start: int  # offset of the field name
    alias_start: int | None = None


@dataclass
class FragmentSpread:
    name: str
    start: int


@dataclass
class InlineFragment:
    type_condition: str | None
    selections: list
    start: int


@dataclass
class VariableDef:
    name: str
    type: str
    default: tuple | None
    start: int


@dataclass
class OperationNode:
    kind: str  # query | mutation | subscription
    name: str | None
    variables: list[VariableDef]
    selections: list
    start: int


@dataclass
class FragmentNode:
    name: str
    type_condition: str
    selections: list
    start: int


@dataclass
class ExecutableDocument:
    operations: list[OperationNode] = field(default_factory=list)
    fragments: list[FragmentNode] = field(default_factory=list)


# --------------------------------------------------------------- SDL result


@dataclass
class Directive:
    name: str
    args: dict[str, tuple]


@dataclass
class InputValueDef:
    name: str
    type: str
    default: tuple | None
    directives: list[Directive]


@dataclass
class FieldDef:
    name: str
    type: str
    args: list[InputValueDef]
    directives: list[Directive]


@dataclass
class TypeDef:
    kind: str  # SCALAR OBJECT INTERFACE UNION ENUM INPUT_OBJECT
    name: str
    extend: bool = False
    fields: list[FieldDef] = field(default_factory=list)
    inputs: list[InputValueDef] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    values: list[tuple[str, list[Directive]]] = field(default_factory=list)


@dataclass
class TypeSystemDocument:
    types: list[TypeDef] = field(default_factory=list)
    roots: dict[str, str] = field(default_factory=dict)  # query/mutation/subscription -> type


KIND_FOR_KEYWORD = {"scalar": "SCALAR", "type": "OBJECT", "interface": "INTERFACE", "union": "UNION",
                    "enum": "ENUM", "input": "INPUT_OBJECT"}


# --------------------------------------------------------------------- parser


class Parser:
    def __init__(self, source: str, offset: int = 0):
        self.source = source
        self.offset = offset
        self.tokens = tokenize(source)
        self.i = 0

    # -- token helpers
    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def peek(self, k: int = 1) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def at(self, kind: str, value: Any = None) -> bool:
        t = self.tok
        return t.kind == kind and (value is None or t.value == value)

    def advance(self) -> Token:
        t = self.tok
        self.i += 1
        return t

    def expect(self, kind: str, value: Any = None) -> Token:
        if not self.at(kind, value):
            want = repr(value) if value is not None else kind
            got = repr(self.tok.value) if self.tok.kind != "eof" else "end of document"
            raise GraphQLSyntaxError(f"expected {want}, found {got}", self.tok.start, self.source)
        return self.advance()

    def skip(self, kind: str, value: Any = None) -> bool:
        if self.at(kind, value):
            self.advance()
            return True
        return False

    def name(self) -> str:
        return self.expect("name").value

    def pos(self, t: Token) -> int:
        return t.start + self.offset

    # -- shared pieces
    def type_ref(self) -> str:
        if self.skip("punct", "["):
            inner = self.type_ref()
            self.expect("punct", "]")
            out = f"[{inner}]"
        else:
            out = self.name()
        if self.skip("punct", "!"):
            out += "!"
        return out

    def value(self, const: bool = False) -> tuple:
        t = self.tok
        if t.kind == "punct" and t.value == "$" and not const:
            self.advance()
            return ("var", self.name())
        if t.kind == "int":
            self.advance()
            return ("int", t.value)
        if t.kind == "float":
            self.advance()
            return ("float", t.value)
        if t.kind == "string":
            self.advance()
            return ("string", t.value)
        if t.kind == "name":
            self.advance()
            if t.value in ("true", "false"):
                return ("bool", t.value == "true")
            if t.value == "null":
                return ("null", None)
            return ("enum", t.value)
        if self.skip("punct", "["):
            items = []
            while not self.skip("punct", "]"):
                items.append(self.value(const))
            return ("list", items)
        if self.skip("punct", "{"):
            obj = {}
            while not self.skip("punct", "}"):
                k = self.name()
                self.expect("punct", ":")
                obj[k] = self.value(const)
            return ("object", obj)
        raise GraphQLSyntaxError(f"unexpected {t.value!r} in value", t.start, self.source)

    def directives(self, const: bool = True) -> list[Directive]:
        out = []
        while self.skip("punct", "@"):
            name = self.name()
            args = {}
            if self.skip("punct", "("):
                while not self.skip("punct", ")"):
                    k = self.name()
                    self.expect("punct", ":")
                    args[k] = self.value(const)
            out.append(Directive(name, args))
        return out

    def description(self) -> None:
        if self.at("string"):
            self.advance()

    # -- type system
    def parse_type_system(self) -> TypeSystemDocument:
        doc = TypeSystemDocument()
        while not self.at("eof"):
            self.description()
            t = self.tok
            if t.kind != "name" or t.value not in TYPE_SYSTEM_KEYWORDS:
                if t.kind == "punct" and t.value == "{" or t.value in EXECUTABLE_KEYWORDS:
                    raise GraphQLSyntaxError("this document contains operations, not a schema", t.start, self.source)
                raise GraphQLSyntaxError(f"unexpected {t.value!r}", t.start, self.source)
            self.advance()
            extend = False
            keyword = t.value
            if keyword == "extend":
                extend = True
                keyword = self.name()
            if keyword == "schema":
                self.directives()
                if self.skip("punct", "{"):
                    while not self.skip("punct", "}"):
                        self.description()
                        role = self.name()
                        self.expect("punct", ":")
                        doc.roots[role] = self.name()
            elif keyword == "directive":
                self.expect("punct", "@")
                self.name()
                if self.at("punct", "("):
                    self.input_values("(", ")")
                self.skip("name", "repeatable")
                self.expect("name", "on")
                self.skip("punct", "|")
                self.name()
                while self.skip("punct", "|"):
                    self.name()
            elif keyword in KIND_FOR_KEYWORD:
                doc.types.append(self.type_definition(keyword, extend))
            else:
                raise GraphQLSyntaxError(f"unexpected {keyword!r}", t.start, self.source)
        return doc

    def type_definition(self, keyword: str, extend: bool) -> TypeDef:
        td = TypeDef(KIND_FOR_KEYWORD[keyword], self.name(), extend)
        if keyword in ("type", "interface") and self.skip("name", "implements"):
            self.skip("punct", "&")
            td.interfaces.append(self.name())
            while True:
                if self.skip("punct", "&"):
                    td.interfaces.append(self.name())
                elif self.at("name") and self.tok.after_comma:  # legacy "implements A, B"
                    td.interfaces.append(self.name())
                else:
                    break
        self.directives()
        if keyword in ("type", "interface") and self.skip("punct", "{"):
            while not self.skip("punct", "}"):
                self.description()
                fname = self.name()
                args = self.input_values("(", ")") if self.at("punct", "(") else []
                self.expect("punct", ":")
                ftype = self.type_ref()
                td.fields.append(FieldDef(fname, ftype, args, self.directives()))
        elif keyword == "input" and self.at("punct", "{"):
            td.inputs = self.input_values("{", "}")
        elif keyword == "enum" and self.skip("punct", "{"):
            while not self.skip("punct", "}"):
                self.description()
                td.values.append((self.name(), self.directives()))
        elif keyword == "union" and self.skip("punct", "="):
            self.skip("punct", "|")
            td.members.append(self.name())
            while self.skip("punct", "|"):
                td.members.append(self.name())
        return td

    def input_values(self, open_: str, close: str) -> list[InputValueDef]:
        self.expect("punct", open_)
        out = []
        while not self.skip("punct", close):
            self.description()
            name = self.name()
            self.expect("punct", ":")
            typ = self.type_ref()
            default = self.value(const=True) if self.skip("punct", "=") else None
            out.append(InputValueDef(name, typ, default, self.directives()))
        return out

    # -- executable
    def parse_executable(self) -> ExecutableDocument:
        doc = ExecutableDocument()
        while not self.at("eof"):
            t = self.tok
            if t.kind == "punct" and t.value == "{":
                doc.operations.append(OperationNode("query", None, [], self.selection_set(), self.pos(t)))
            elif t.kind == "name" and t.value in ("query", "mutation", "subscription"):
                self.advance()
                name = self.name() if self.at("name") else None
                variables = self.variable_defs() if self.at("punct", "(") else []
                self.directives(const=False)
                doc.operations.append(OperationNode(t.value, name, variables, self.selection_set(), self.pos(t)))
            elif t.kind == "name" and t.value == "fragment":
                self.advance()
                name = self.name()
                self.expect("name", "on")
                cond = self.name()
                self.directives(const=False)
                doc.fragments.append(FragmentNode(name, cond, self.selection_set(), self.pos(t)))
            elif t.kind == "string" or (t.kind == "name" and t.value in TYPE_SYSTEM_KEYWORDS):
                raise GraphQLSyntaxError("this document contains schema definitions, not operations",
                                         t.start, self.source)
            else:
                raise GraphQLSyntaxError(f"unexpected {t.value!r}", t.start, self.source)
        return doc

    def variable_defs(self) -> list[VariableDef]:
        self.expect("punct", "(")
        out = []
        while not self.skip("punct", ")"):
            t = self.expect("punct", "$")
            name = self.name()
            self.expect("punct", ":")
            typ = self.type_ref()
            default = self.value(const=True) if self.skip("punct", "=") else None
            self.directives()
            out.append(VariableDef(name, typ, default, self.pos(t)))
        return out

    def selection_set(self) -> list:
        self.expect("punct", "{")
        out: list = []
        while not self.skip("punct", "}"):
            if self.at("spread"):
                t = self.advance()
                if self.at("name") and self.tok.value != "on":
                    out.append(FragmentSpread(self.name(), self.pos(t)))
                    self.directives(const=False)
                else:
                    cond = None
                    if self.skip("name", "on"):
                        cond = self.name()
                    self.directives(const=False)
                    out.append(InlineFragment(cond, self.selection_set(), self.pos(t)))
                continue
            first = self.expect("name")
            alias, alias_start = None, None
            name_tok = first
            if self.skip("punct", ":"):
                alias, alias_start = first.value, self.pos(first)
                name_tok = self.expect("name")
            args: list[Argument] = []
            if self.skip("punct", "("):
                while not self.skip("punct", ")"):
                    at = self.expect("name")
                    self.expect("punct", ":")
                    vstart = self.pos(self.tok)
                    args.append(Argument(at.value, self.value(), self.pos(at), vstart))
            self.directives(const=False)
            sel = self.selection_set() if self.at("punct", "{") else None
            out.append(FieldNode(name_tok.value, alias, args, sel, self.pos(name_tok), alias_start))
        return out


def parse_schema(source: str) -> TypeSystemDocument:
    return Parser(source).parse_type_system()


def parse_operations(source: str, offset: int = 0) -> ExecutableDocument:
    return Parser(source, offset).parse_executable()


def document_kind(source: str) -> str | None:
    """'schema', 'operations' or None, from the first definition in the text."""
    try:
        tokens = tokenize(source)
    except GraphQLSyntaxError:
        return None
    for t in tokens:
        if t.kind == "string":
            continue  # a description
        if t.kind == "name" and t.value in TYPE_SYSTEM_KEYWORDS:
            return "schema"
        if (t.kind == "name" and t.value in EXECUTABLE_KEYWORDS) or (t.kind == "punct" and t.value == "{"):
            return "operations"
        return None
    return None
