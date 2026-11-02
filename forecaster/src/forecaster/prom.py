"""Minimal Prometheus HTTP API client (query_range only). Stdlib, so the container needs no
HTTP library."""

import json
import math
import urllib.error
import urllib.parse
import urllib.request


class PromError(Exception):
    """Prometheus could not be queried or returned something unusable."""


class Prometheus:
    def __init__(self, url, timeout=10):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def query_range(self, query, start, end, step):
        """Samples of a query that returns one series: a list of (unix time, value).

        An empty result (e.g. no web pods, so no series) is an empty list, not an error.
        """
        params = urllib.parse.urlencode({"query": query, "start": start, "end": end,
                                         "step": step})
        try:
            with urllib.request.urlopen(f"{self.url}/api/v1/query_range?{params}",
                                        timeout=self.timeout) as r:
                body = json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PromError(f"query_range failed: {e}") from e
        return parse(body)


def parse(body):
    if body.get("status") != "success":
        raise PromError(f"Prometheus error: {body.get('errorType')}: {body.get('error')}")
    result = body.get("data", {}).get("result", [])
    if len(result) > 1:
        raise PromError(f"query returned {len(result)} series, expected one (use sum())")
    if not result:
        return []
    out = []
    for t, v in result[0]["values"]:
        v = float(v)
        if math.isfinite(v):
            out.append((float(t), v))
    return out
