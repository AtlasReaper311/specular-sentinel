"""Tests for specular-sentinel.

Everything network-shaped is patched at the two HTTP seams
(`http_get_json`, `http_post_json`), so the suite runs anywhere Python
does; CI included. The interesting coverage is the drift state machine:
the baseline only advances after a delivered report, which is what turns
"drift while the edge was down" into a report on recovery instead of a
silently swallowed event.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sentinel

GOOD_HEALTH = {
    "ok": True,
    "service": "atlas-corpus",
    "version": "1.0.0",
    "chroma_ok": True,
    "ollama_ok": True,
    "documents": 12,
    "chunks": 340,
    "refreshing": False,
}

GOOD_SEARCH = {
    "query": "specular sentinel canary",
    "hits": [
        {
            "text": "WSL2 IP changes on every reboot",
            "score": 0.91,
            "source_repo": "atlas-infra",
            "file_path": "decisions.md",
            "doc_type": "md",
            "last_updated": "2026-07-01",
            "chunk_index": 3,
        }
    ],
    "took_ms": 41,
}

OK_CHECK = {"ok": True, "latency_ms": 5, "detail": "test"}


class ValidatorTests(unittest.TestCase):
    def test_health_shape_accepts_the_documented_document(self):
        self.assertIsNone(sentinel.validate_corpus_health(GOOD_HEALTH))

    def test_health_shape_rejects_missing_chunks(self):
        broken = {k: v for k, v in GOOD_HEALTH.items() if k != "chunks"}
        self.assertEqual(
            sentinel.validate_corpus_health(broken),
            "missing or mistyped field: chunks",
        )

    def test_health_shape_rejects_non_object(self):
        self.assertEqual(
            sentinel.validate_corpus_health([1, 2]), "body is not a JSON object"
        )

    def test_search_shape_accepts_the_documented_document(self):
        self.assertIsNone(sentinel.validate_search_response(GOOD_SEARCH))

    def test_search_shape_accepts_zero_hits(self):
        empty = dict(GOOD_SEARCH, hits=[])
        self.assertIsNone(sentinel.validate_search_response(empty))

    def test_search_shape_rejects_missing_took_ms(self):
        broken = {k: v for k, v in GOOD_SEARCH.items() if k != "took_ms"}
        self.assertEqual(
            sentinel.validate_search_response(broken),
            "missing or mistyped field: took_ms",
        )


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.cfg = sentinel.load_config()

    def test_ollama_ok_counts_models(self):
        with mock.patch.object(
            sentinel,
            "http_get_json",
            return_value=(200, {"models": [{}, {}]}, 12),
        ):
            result = sentinel.check_ollama(self.cfg)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "2 models available")

    def test_ollama_rejects_wrong_shape(self):
        with mock.patch.object(
            sentinel, "http_get_json", return_value=(200, {"nope": 1}, 8)
        ):
            result = sentinel.check_ollama(self.cfg)
        self.assertFalse(result["ok"])
        self.assertIn("shape", result["detail"])

    def test_ollama_connection_refused_never_raises(self):
        with mock.patch.object(
            sentinel, "http_get_json", side_effect=OSError("connection refused")
        ):
            result = sentinel.check_ollama(self.cfg)
        self.assertFalse(result["ok"])
        self.assertTrue(result["detail"].startswith("OSError"))

    def test_corpus_health_surfaces_degraded_flags(self):
        degraded = dict(GOOD_HEALTH, ok=False, chroma_ok=False)
        with mock.patch.object(
            sentinel, "http_get_json", return_value=(200, degraded, 20)
        ):
            result = sentinel.check_corpus_health(self.cfg)
        self.assertFalse(result["ok"])
        self.assertIn("chroma_ok=False", result["detail"])

    def test_search_canary_sends_the_internal_header(self):
        captured = {}

        def fake_get(url, timeout, headers=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return (200, GOOD_SEARCH, 30)

        with mock.patch.object(sentinel, "http_get_json", side_effect=fake_get):
            result = sentinel.check_corpus_search(self.cfg)
        self.assertTrue(result["ok"])
        self.assertEqual(
            captured["headers"].get(sentinel.INTERNAL_HEADER), sentinel.SENTINEL_NAME
        )
        self.assertIn("/search?q=", captured["url"])


class DriftAndDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = str(Path(self.tmp.name) / "state.json")
        self.env = {
            "STATE_FILE": self.state_file,
            "INFRA_REPORT_KEY": "test-key",
            "REPORT_URL": "https://api.atlas-systems.uk/v1/infra/report",
        }
        self.checks_patchers = [
            mock.patch.object(sentinel, name, return_value=dict(OK_CHECK))
            for name in ("check_ollama", "check_corpus_health", "check_corpus_search")
        ]
        for p in self.checks_patchers:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.checks_patchers:
            self.addCleanup(p.stop)

    def test_first_run_never_flags_drift(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            sentinel, "current_wsl_ip", return_value="172.20.1.5"
        ):
            report = sentinel.build_report(sentinel.load_config(), {})
        self.assertFalse(report["ip_changed"])
        self.assertIsNone(report["previous_wsl_ip"])

    def test_drift_detected_against_saved_state(self):
        sentinel.save_state(self.state_file, {"wsl_ip": "172.20.1.5"})
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            sentinel, "current_wsl_ip", return_value="172.20.9.2"
        ):
            cfg = sentinel.load_config()
            report = sentinel.build_report(cfg, sentinel.load_state(cfg["state_file"]))
        self.assertTrue(report["ip_changed"])
        self.assertEqual(report["previous_wsl_ip"], "172.20.1.5")

    def test_baseline_advances_only_after_delivered_report(self):
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(
                sentinel, "current_wsl_ip", return_value="172.20.1.5"
            ), mock.patch.object(sentinel, "http_post_json", return_value=200):
                sentinel.main([])
            self.assertEqual(
                sentinel.load_state(self.state_file)["wsl_ip"], "172.20.1.5"
            )

            # Edge goes dark; the observed IP moves; the baseline must not.
            with mock.patch.object(
                sentinel, "current_wsl_ip", return_value="172.20.9.2"
            ), mock.patch.object(
                sentinel, "http_post_json", side_effect=OSError("edge down")
            ):
                self.assertEqual(sentinel.main([]), 0)
            self.assertEqual(
                sentinel.load_state(self.state_file)["wsl_ip"], "172.20.1.5"
            )

    def test_dry_run_never_posts_or_touches_state(self):
        post = mock.MagicMock()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            sentinel, "current_wsl_ip", return_value="172.20.1.5"
        ), mock.patch.object(sentinel, "http_post_json", post):
            self.assertEqual(sentinel.main(["--dry-run"]), 0)
        post.assert_not_called()
        self.assertFalse(Path(self.state_file).exists())

    def test_missing_key_refuses_to_post(self):
        env = dict(self.env)
        env.pop("INFRA_REPORT_KEY")
        post = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(
            os.environ, {"INFRA_REPORT_KEY": ""}
        ), mock.patch.object(
            sentinel, "current_wsl_ip", return_value="172.20.1.5"
        ), mock.patch.object(sentinel, "http_post_json", post):
            self.assertEqual(sentinel.main([]), 0)
        post.assert_not_called()

    def test_report_payload_shape_matches_the_edge_contract(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            sentinel, "current_wsl_ip", return_value="172.20.1.5"
        ):
            report = sentinel.build_report(sentinel.load_config(), {})
        for key in ("sentinel", "machine", "ts", "wsl_ip", "ip_changed", "checks"):
            self.assertIn(key, report)
        for name in ("ollama", "corpus_health", "corpus_search"):
            self.assertIn(name, report["checks"])
            self.assertIsInstance(report["checks"][name]["ok"], bool)
        # The contract survives a JSON round trip untouched.
        self.assertEqual(json.loads(json.dumps(report)), report)


if __name__ == "__main__":
    unittest.main()
