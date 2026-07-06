<div align="center">
  <img src="https://raw.githubusercontent.com/AtlasReaper311/AtlasReaper311/main/atlas-icon-dark-256.png" width="88" alt="Atlas Systems"/>
</div>

# specular-sentinel

```
┌─────────────────────────────────────────────┐
│  ATLAS SYSTEMS // specular-sentinel         │
│  local health facts for the infra           │
│  pipeline; observes what only this          │
│  machine can see                            │
└─────────────────────────────────────────────┘
```

[![CI](https://github.com/AtlasReaper311/specular-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/AtlasReaper311/specular-sentinel/actions)
![Python](https://img.shields.io/badge/python-3.10%2B_stdlib_only-f5a623?style=flat-square&labelColor=0a0a0f)
![Runtime](https://img.shields.io/badge/runtime-systemd_timer-aaa9a0?style=flat-square&labelColor=0a0a0f)
![Cost](https://img.shields.io/badge/cost-%C2%A30-aaa9a0?style=flat-square&labelColor=0a0a0f)

A five-minute systemd timer on `SPECULAR-CORE` that gathers the reachability facts no Cloudflare Worker can: raw Ollama on `127.0.0.1:11434`, the corpus RAG pipeline end to end, and the WSL2 `eth0` address that moves on every Windows reboot. It POSTs one JSON report per run to [`atlas-api-public`](https://github.com/AtlasReaper311/atlas-api-public) and deliberately decides nothing itself; the edge owns state, severity, alert routing, and the dead-man's switch for when this machine goes silent.

## Prerequisites

- WSL2 Ubuntu with systemd enabled (`systemd=true` in `/etc/wsl.conf`)
- Python 3.10 or later (stock `python3` on Ubuntu 24 is fine; the script has zero dependencies)
- The `atlas-api-public` Worker deployed, with `INFRA_REPORT_KEY` already set via `wrangler secret put`

## Setup

```bash
git clone https://github.com/AtlasReaper311/specular-sentinel.git
cd specular-sentinel
sudo bash install-wsl.sh
sudoedit /etc/specular-sentinel/env   # set INFRA_REPORT_KEY
```

The installer copies `sentinel.py` to `/opt/specular-sentinel`, the units into `/etc/systemd/system`, creates `/etc/specular-sentinel/env` (mode `600`) from `env.example` if it does not exist, and enables the timer. Re-running it after an edit is safe.

`INFRA_REPORT_KEY` lives in exactly two places: that env file, and the matching Worker secret. Anywhere else, ever, means rotate both.

## Usage

```bash
python3 sentinel.py --dry-run                      # print the report, POST nothing
sudo systemctl start specular-sentinel.service     # one real pass now
journalctl -u specular-sentinel.service -n 20      # what the last runs said
systemctl list-timers specular-sentinel.timer      # when the next pass fires
```

Each journal line is one run: `3/3 checks ok, ip=172.20.4.11 -> report 200`.

## The reachability model

Three checks per pass, each shape-validated rather than status-code-validated:

| check | what it proves |
|---|---|
| `ollama` | `GET /api/tags` returns the tags document; the model server is answering its own API, not just accepting TCP |
| `corpus_health` | `GET /health` on [`atlas-corpus`](https://github.com/AtlasReaper311/atlas-corpus) matches `HealthResponse` and reports `ok: true` |
| `corpus_search` | `GET /search` with a canary query matches `SearchResponse`; the embed-to-Chroma path works end to end, which a healthy `/health` does not prove |

The canary carries `X-Atlas-Internal` from a loopback address, and the corpus skips both query logging and rate limiting for exactly that combination, so monitoring never pollutes the RAG query stats it sits beside.

Caller context matters here (the estate rule: ask who is making the request). This process is a WSL-native systemd unit, so Ollama and the corpus are both `127.0.0.1`; containers would need `172.17.0.1` and Workers need the public tunnel. The URLs in `env.example` are correct for this context only.

Drift semantics: the previous IP persists in `/var/lib/specular-sentinel/state.json` and only advances after a delivered report. A drift that happens while the edge is unreachable is re-reported on recovery instead of being absorbed into local state; delivery is at-least-once by construction.

Failure semantics: every check catches its own exceptions, a failed POST logs and exits `0`, and the timer keeps firing. A sentinel that crashes on the exact failures it exists to report would be self-defeating; persistent silence is the edge cron's job to notice, not this script's job to prevent.

## How it fits into Atlas Systems

This is the local half of the Infra Health pipeline. It reports to [`atlas-api-public`](https://github.com/AtlasReaper311/atlas-api-public), which stores state in KV, detects transitions, and alerts `#infra-health` through [`atlas-notify`](https://github.com/AtlasReaper311/atlas-notify)'s envelope routing; the Live Systems card on [atlas-systems.uk/lab](https://atlas-systems.uk/lab/) reads the resulting status endpoint. It watches [`atlas-corpus`](https://github.com/AtlasReaper311/atlas-corpus) and the Ollama install that [`ramone-memory`](https://github.com/AtlasReaper311/ramone-memory) fronts, and it exists because of the WSL2 IP drift failure mode documented in [`atlas-bootstrap`](https://github.com/AtlasReaper311/atlas-bootstrap).

The transferable principle: put observation where the visibility is and judgement where the durability is, because a monitor that lives with the thing it watches can never report the failure that takes them both down.

---

Part of [atlas-systems.uk](https://atlas-systems.uk)
