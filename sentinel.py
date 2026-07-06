#!/usr/bin/env python3
"""specular-sentinel: local reachability facts for the Infra Health pipeline.

One run gathers three checks and one observation, POSTs them to the edge,
and exits 0 regardless. The division of labour is deliberate: this script
observes things only SPECULAR-CORE can see (raw Ollama on the LAN, the
corpus from localhost, the WSL2 eth0 address); the atlas-api-public Worker
owns state, transition detection, alerting, and the dead-man's switch for
when this machine goes silent. A sentinel that decided severity locally
could never report its own death, which is the failure mode the Infra
Health card exists to catch.

Checks per run:
  ollama          GET {OLLAMA_URL}/api/tags, shape-validated (models list)
  corpus_health   GET {CORPUS_URL}/health, shape-validated (HealthResponse)
  corpus_search   GET {CORPUS_URL}/search?q=<canary>, shape-validated
                  (SearchResponse); proves embed -> Chroma end to end,
                  which a 200 from /health does not

The search canary carries X-Atlas-Internal from a loopback source, and
atlas-corpus skips both query logging and rate limiting for exactly that
combination, so Part 1's probing never pollutes Part 2's query data.

WSL2 IP: read via the outbound-socket trick (no packet is sent for UDP
connect), falling back to `hostname -I`. The previous address persists in
STATE_FILE and is only advanced after a successful report, so a drift that
happens while the edge is unreachable is still delivered on recovery
(at-least-once, not at-most-once).

Caller context note (decisions.md :: "Ask who is making the request?"):
this process is a WSL-native systemd unit, so Ollama and the corpus are
both `127.0.0.1` from here; containers would want 172.17.0.1 and Workers
want the public tunnel. Do not copy these URLs into other contexts.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SENTINEL_NAME = "specular-sentinel"
SENTINEL_VERSION = "1.0.0"
INTERNAL_HEADER = "X-Atlas-Internal"


# --------------------------------------------------------------------- #
# Configuration (environment, with working defaults for SPECULAR-CORE)   #
# --------------------------------------------------------------------- #


def load_config() -> dict:
    """Environment-driven configuration; only the report key is secret."""
    return {
        "report_url": os.environ.get(
            "REPORT_URL", "https://api.atlas-systems.uk/v1/infra/report"
        ),
        "report_key": os.environ.get("INFRA_REPORT_KEY", ""),
        "ollama_url": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
        "corpus_url": os.environ.get("CORPUS_URL", "http://127.0.0.1:8092"),
        "state_file": os.environ.get(
            "STATE_FILE", "/var/lib/specular-sentinel/state.json"
        ),
        "timeout": float(os.environ.get("TIMEOUT_SECONDS", "6")),
        "canary_query": os.environ.get("CANARY_QUERY", "specular sentinel canary"),
        "machine": os.environ.get("MACHINE", socket.gethostname()),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- #
# HTTP seams (single points the unit tests patch)                        #
# --------------------------------------------------------------------- #


def http_get_json(url: str, timeout: float, headers: dict | None = None):
    """GET a URL, return (status, parsed_json_or_None, latency_ms)."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            return resp.status, json.loads(raw), latency_ms
        except (json.JSONDecodeError, UnicodeDecodeError):
            return resp.status, None, latency_ms


def http_post_json(url: str, payload: dict, timeout: float, bearer: str) -> int:
    """POST JSON with a Bearer token, return the response status."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {bearer}",
            "user-agent": f"{SENTINEL_NAME}/{SENTINEL_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


# --------------------------------------------------------------------- #
# Checks: each returns {ok, latency_ms, detail}, never raises            #
# --------------------------------------------------------------------- #


def check_ollama(cfg: dict) -> dict:
    """Ollama is up and answering its own API, not just accepting TCP."""
    try:
        status, data, latency = http_get_json(
            f"{cfg['ollama_url']}/api/tags", cfg["timeout"]
        )
    except Exception as exc:  # noqa: BLE001 - a check must never crash the run
        return {"ok": False, "latency_ms": None, "detail": _err(exc)}
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "latency_ms": latency, "detail": f"http {status}"}
    models = data.get("models")
    if not isinstance(models, list):
        return {
            "ok": False,
            "latency_ms": latency,
            "detail": "200 but response shape is not the Ollama tags document",
        }
    return {
        "ok": True,
        "latency_ms": latency,
        "detail": f"{len(models)} models available",
    }


def validate_corpus_health(data) -> str | None:
    """None when the payload matches HealthResponse; else the reason."""
    if not isinstance(data, dict):
        return "body is not a JSON object"
    for key, kind in (
        ("ok", bool),
        ("chroma_ok", bool),
        ("ollama_ok", bool),
        ("documents", int),
        ("chunks", int),
    ):
        if not isinstance(data.get(key), kind):
            return f"missing or mistyped field: {key}"
    return None


def check_corpus_health(cfg: dict) -> dict:
    """The corpus reports itself healthy, in the exact documented shape."""
    try:
        status, data, latency = http_get_json(
            f"{cfg['corpus_url']}/health", cfg["timeout"]
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "detail": _err(exc)}
    if status != 200:
        return {"ok": False, "latency_ms": latency, "detail": f"http {status}"}
    problem = validate_corpus_health(data)
    if problem:
        return {"ok": False, "latency_ms": latency, "detail": problem}
    if not data["ok"]:
        return {
            "ok": False,
            "latency_ms": latency,
            "detail": (
                f"service reports degraded: chroma_ok={data['chroma_ok']} "
                f"ollama_ok={data['ollama_ok']}"
            ),
        }
    return {
        "ok": True,
        "latency_ms": latency,
        "detail": f"{data['documents']} docs, {data['chunks']} chunks",
    }


def validate_search_response(data) -> str | None:
    """None when the payload matches SearchResponse; else the reason."""
    if not isinstance(data, dict):
        return "body is not a JSON object"
    if not isinstance(data.get("query"), str):
        return "missing or mistyped field: query"
    if not isinstance(data.get("took_ms"), int):
        return "missing or mistyped field: took_ms"
    hits = data.get("hits")
    if not isinstance(hits, list):
        return "missing or mistyped field: hits"
    for hit in hits[:1]:
        for key in ("text", "score", "source_repo", "file_path"):
            if key not in hit:
                return f"hit missing field: {key}"
    return None


def check_corpus_search(cfg: dict) -> dict:
    """A real /search round trip: query embedding through Chroma and back.

    Zero hits is still ok; the contract under test is the response shape
    and the embed pipeline, not corpus contents (an empty corpus surfaces
    through corpus_health's chunk count instead).
    """
    q = urllib.parse.quote(cfg["canary_query"])
    try:
        status, data, latency = http_get_json(
            f"{cfg['corpus_url']}/search?q={q}&top_k=1",
            cfg["timeout"],
            headers={INTERNAL_HEADER: SENTINEL_NAME},
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "detail": _err(exc)}
    if status != 200:
        return {"ok": False, "latency_ms": latency, "detail": f"http {status}"}
    problem = validate_search_response(data)
    if problem:
        return {"ok": False, "latency_ms": latency, "detail": problem}
    return {
        "ok": True,
        "latency_ms": latency,
        "detail": f"{len(data['hits'])} hits in {data['took_ms']}ms",
    }


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------- #
# WSL2 IP observation and drift state                                     #
# --------------------------------------------------------------------- #


def current_wsl_ip() -> str | None:
    """The address WSL2 would use outbound; changes on Windows reboot."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # UDP connect sends nothing; it only binds the route decision.
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        ).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------- #
# Run                                                                     #
# --------------------------------------------------------------------- #


def build_report(cfg: dict, prev_state: dict) -> dict:
    ip = current_wsl_ip()
    prev_ip = prev_state.get("wsl_ip")
    ip_changed = bool(prev_ip and ip and prev_ip != ip)
    return {
        "sentinel": f"{SENTINEL_NAME}/{SENTINEL_VERSION}",
        "machine": cfg["machine"],
        "ts": now_iso(),
        "wsl_ip": ip,
        "previous_wsl_ip": prev_ip,
        "ip_changed": ip_changed,
        "checks": {
            "ollama": check_ollama(cfg),
            "corpus_health": check_corpus_health(cfg),
            "corpus_search": check_corpus_search(cfg),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report payload without POSTing or touching state",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    prev_state = load_state(cfg["state_file"])
    report = build_report(cfg, prev_state)

    ok_count = sum(1 for c in report["checks"].values() if c["ok"])
    summary = (
        f"{ok_count}/3 checks ok, ip={report['wsl_ip']}"
        f"{' (CHANGED from ' + str(report['previous_wsl_ip']) + ')' if report['ip_changed'] else ''}"
    )

    if args.dry_run:
        print(json.dumps(report, indent=2))
        print(f"[dry-run] {summary}", file=sys.stderr)
        return 0

    if not cfg["report_key"]:
        print(
            "INFRA_REPORT_KEY is not set; refusing to POST an unauthenticated "
            "report. Set it in /etc/specular-sentinel/env.",
            file=sys.stderr,
        )
        return 0  # the edge staleness cron is the alarm for a dead sentinel

    delivered = False
    try:
        status = http_post_json(
            cfg["report_url"], report, cfg["timeout"], cfg["report_key"]
        )
        delivered = 200 <= status < 300
        print(f"{summary} -> report {status}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never crash; the timer must keep firing
        print(f"{summary} -> report failed: {_err(exc)}", file=sys.stderr)

    # Advance the drift baseline only after a delivered report, so a drift
    # observed while the edge is down is re-reported on recovery instead
    # of being silently absorbed into local state.
    if delivered and report["wsl_ip"]:
        save_state(
            cfg["state_file"],
            {"wsl_ip": report["wsl_ip"], "updated_at": report["ts"]},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
