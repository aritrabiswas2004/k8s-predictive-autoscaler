import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from forecaster.prom import PromError, Prometheus, parse


def body(*series):
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": s} for s in series]}}


def test_parse():
    assert parse(body([[1, "1.5"], [16, "NaN"], [31, "2"]])) == [(1.0, 1.5), (31.0, 2.0)]
    assert parse(body()) == []


def test_parse_rejects_errors_and_multiple_series():
    with pytest.raises(PromError):
        parse({"status": "error", "errorType": "bad_data", "error": "parse error"})
    with pytest.raises(PromError):
        parse(body([[1, "1"]], [[1, "2"]]))


@pytest.fixture
def server():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            out = json.dumps(body([[100, "42"]])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_query_range(server):
    url, seen = server
    assert Prometheus(url).query_range("sum(x)", 10, 100, 15) == [(100.0, 42.0)]
    assert seen["path"].startswith("/api/v1/query_range?query=sum%28x%29&start=10&end=100")


def test_unreachable():
    with pytest.raises(PromError):
        Prometheus("http://127.0.0.1:9", timeout=1).query_range("x", 0, 1, 1)
