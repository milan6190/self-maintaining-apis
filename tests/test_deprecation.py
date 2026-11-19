"""Seeing breakage coming: deprecations in specs and Deprecation/Sunset headers on live calls."""

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from apimend.cli import main
from apimend.diff import diff_specs
from apimend.signals import keep_headers, signal_changes


def spec(op_extra=None, param_extra=None, prop_extra=None):
    return {
        "openapi": "3.0.3",
        "paths": {"/orders": {"get": {
            **(op_extra or {}),
            "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}, **(param_extra or {})}],
            "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"id": {"type": "string"},
                                                 "legacy_ref": {"type": "string", **(prop_extra or {})}}}}}}},
        }}},
    }


def kinds(changes):
    return {(c.kind, c.severity) for c in changes}


def test_newly_deprecated_operation_param_and_field_are_warnings():
    ch = diff_specs(spec(), spec({"deprecated": True, "x-sunset": "2027-03-01"},
                                 {"deprecated": True}, {"deprecated": True}))
    assert {("operation-deprecated", "warning"), ("param-deprecated", "warning"),
            ("field-deprecated", "warning")} <= kinds(ch)
    op = next(c for c in ch if c.kind == "operation-deprecated")
    assert "2027-03-01" in op.message
    assert next(c for c in ch if c.kind == "field-deprecated").path == "$.legacy_ref"


def test_already_deprecated_is_not_reported_again():
    s = spec({"deprecated": True}, {"deprecated": True}, {"deprecated": True})
    assert diff_specs(s, s) == []


def test_removing_a_deprecated_operation_says_so():
    old = spec({"deprecated": True})
    new = {"openapi": "3.0.3", "paths": {}}
    msg = next(c.message for c in diff_specs(old, new) if c.kind == "operation-removed")
    assert "already deprecated" in msg


def test_sunset_added_later_is_reported():
    ch = diff_specs(spec({"deprecated": True}), spec({"deprecated": True, "x-sunset": "2027-01-15"}))
    assert [c.kind for c in ch] == ["sunset-scheduled"]


def test_keep_headers_filters_to_deprecation_signals():
    kept = keep_headers([("Content-Type", "application/json"), ("Deprecation", "@1767225600"),
                         ("Link", '<https://x.dev/next>; rel="next"'),
                         ("Link", '<https://x.dev/deprecation>; rel="deprecation"'),
                         ("Warning", '110 - "Response is stale"'), ("Sunset", "Wed, 30 Jun 2027 23:59:59 GMT")])
    assert kept == {"deprecation": "@1767225600", "link": '<https://x.dev/deprecation>; rel="deprecation"',
                    "sunset": "Wed, 30 Jun 2027 23:59:59 GMT"}


TODAY = date(2026, 11, 19)


def test_structured_deprecation_date_and_link():
    ch = signal_changes("orders", {"deprecation": "@1767225600",
                                   "link": '<https://x.dev/dep>; rel="deprecation"'}, {}, today=TODAY)
    assert [(c.kind, c.severity) for c in ch] == [("endpoint-deprecated", "warning")]
    assert "2026-01-01" in ch[0].message and "https://x.dev/dep" in ch[0].message
    assert "new since the baseline" in ch[0].message
    again = signal_changes("orders", {"deprecation": "@1767225600"}, {"deprecation": "@1767225600"}, today=TODAY)
    assert "new since" not in again[0].message


@pytest.mark.parametrize("sunset,kind,severity,text", [
    ("Wed, 30 Jun 2027 23:59:59 GMT", "sunset-scheduled", "warning", "223 day(s) left"),
    ("Fri, 04 Dec 2026 00:00:00 GMT", "sunset-scheduled", "warning", "soon"),
    ("Sun, 01 Nov 2026 00:00:00 GMT", "sunset-passed", "breaking", "18 day(s) ago"),
])
def test_sunset_dates(sunset, kind, severity, text):
    (c,) = signal_changes("orders", {"sunset": sunset}, {}, today=TODAY)
    assert (c.kind, c.severity) == (kind, severity) and text in c.message


def test_warning_299_is_a_notice():
    (c,) = signal_changes("orders", {"warning": '299 api.x.dev "v1 is deprecated, use v2"'}, {}, today=TODAY)
    assert c.kind == "deprecation-notice" and "v1 is deprecated, use v2" in c.message


def test_no_signals_no_changes():
    assert signal_changes("orders", {}, {}) == []
    assert signal_changes("orders", {"deprecation": "false"}, {}) == []


class _Api(BaseHTTPRequestHandler):
    deprecated = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if type(self).deprecated:
            self.send_header("Deprecation", "true")
            self.send_header("Sunset", "Sun, 01 Nov 2026 00:00:00 GMT")
            self.send_header("Link", '<https://api.example.dev/v2/orders>; rel="successor-version"')
        self.end_headers()
        self.wfile.write(json.dumps([{"id": "o1"}]).encode())


def test_live_endpoint_headers_end_to_end(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        (tmp_path / "apimend.yaml").write_text(f"""apis:
  - name: orders
    endpoints:
      - name: list
        url: http://127.0.0.1:{srv.server_port}/v1/orders
""")
        monkeypatch.chdir(tmp_path)
        _Api.deprecated = False
        assert main(["snapshot"]) == 0
        assert not (tmp_path / ".apimend/baseline/orders/signals").exists()
        assert main(["check"]) == 0
        _Api.deprecated = True
        assert main(["check", "--fail-on", "warning"]) == 1
        report = json.loads((tmp_path / ".apimend/report.json").read_text())
        found = {c["kind"]: c for c in report["apis"][0]["changes"]}
        assert "endpoint-deprecated" in found
        # the sunset in the header is before the test's real "today" only if it has passed; either way it's reported
        sunset = found.get("sunset-passed") or found.get("sunset-scheduled")
        assert sunset and "successor: https://api.example.dev/v2/orders" in sunset["message"]
    finally:
        srv.shutdown()
