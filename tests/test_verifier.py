"""Acceptance logic must reject HTTP-success responses that did not use the intended GPU model."""

import copy
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "roles/llm/files/verify_api.py"
SPEC = importlib.util.spec_from_file_location("verify_api", MODULE_PATH)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
MODEL = "qwen2.5:7b-instruct-q4_K_M"
DIGEST = "a" * 64


class FakeAPI:
    def __init__(self):
        self.responses = {
            "/api/version": {"version": "test-version"},
            "/api/tags": {"models": [{"name": MODEL, "digest": DIGEST}]},
            "/api/generate": {"done": True, "response": "Hello", "eval_count": 2},
            "/api/ps": {"models": [{"name": MODEL, "digest": DIGEST, "size_vram": 4096}]},
        }
        self.calls = []

    def __call__(self, path, body=None, timeout=30):
        self.calls.append((path, body, timeout))
        return copy.deepcopy(self.responses[path])


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def test_gpu_inference_passes_and_sends_bounded_nonstreaming_request(self):
        result = VERIFIER.verify(self.api, MODEL, context_length=1024, timeout=123, expected_digest=DIGEST)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["gpu_resident_bytes"], 4096)
        request = self.api.calls[2]
        self.assertEqual(request[2], 123)
        self.assertIs(request[1]["stream"], False)
        self.assertEqual(request[1]["keep_alive"], "2m")
        self.assertEqual(request[1]["options"]["num_ctx"], 1024)
        self.assertEqual(request[1]["options"]["num_predict"], 32)

    def test_cpu_fallback_is_not_success(self):
        self.api.responses["/api/ps"]["models"][0]["size_vram"] = 0
        with self.assertRaisesRegex(VERIFIER.VerificationError, "CPU fallback"):
            VERIFIER.verify(self.api, MODEL)

    def test_missing_vram_field_is_not_success(self):
        del self.api.responses["/api/ps"]["models"][0]["size_vram"]
        with self.assertRaisesRegex(VERIFIER.VerificationError, "CPU fallback"):
            VERIFIER.verify(self.api, MODEL)

    def test_unrelated_gpu_model_does_not_hide_cpu_fallback(self):
        self.api.responses["/api/ps"]["models"] = [
            {"name": "another:7b", "digest": DIGEST, "size_vram": 4096},
            {"name": MODEL, "digest": DIGEST, "size_vram": 0},
        ]
        with self.assertRaisesRegex(VERIFIER.VerificationError, "CPU fallback"):
            VERIFIER.verify(self.api, MODEL)

    def test_missing_model_prevents_generation(self):
        self.api.responses["/api/tags"]["models"] = []
        with self.assertRaisesRegex(VERIFIER.VerificationError, "missing"):
            VERIFIER.verify(self.api, MODEL)
        self.assertEqual(len(self.api.calls), 2)

    def test_wrong_expected_digest_prevents_generation(self):
        with self.assertRaisesRegex(VERIFIER.VerificationError, "digest"):
            VERIFIER.verify(self.api, MODEL, expected_digest="b" * 64)
        self.assertEqual(len(self.api.calls), 2)

    def test_changed_loaded_model_is_rejected(self):
        self.api.responses["/api/ps"]["models"][0]["digest"] = "b" * 64
        with self.assertRaisesRegex(VERIFIER.VerificationError, "differs"):
            VERIFIER.verify(self.api, MODEL)

    def test_empty_response_is_rejected(self):
        self.api.responses["/api/generate"]["response"] = "  "
        with self.assertRaisesRegex(VERIFIER.VerificationError, "Empty"):
            VERIFIER.verify(self.api, MODEL)

    def test_unfinished_generation_is_rejected(self):
        self.api.responses["/api/generate"]["done"] = False
        with self.assertRaisesRegex(VERIFIER.VerificationError, "finish"):
            VERIFIER.verify(self.api, MODEL)

    def test_zero_generated_tokens_is_rejected(self):
        self.api.responses["/api/generate"]["eval_count"] = 0
        with self.assertRaisesRegex(VERIFIER.VerificationError, "tokens"):
            VERIFIER.verify(self.api, MODEL)

    def test_unloaded_model_is_rejected(self):
        self.api.responses["/api/ps"]["models"] = []
        with self.assertRaisesRegex(VERIFIER.VerificationError, "resident"):
            VERIFIER.verify(self.api, MODEL)

    def test_cli_returns_failure_when_transport_fails(self):
        with patch.object(VERIFIER, "APIClient", side_effect=OSError("connection refused")):
            with patch("sys.argv", ["gpu-llm-check"]), patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(VERIFIER.main(), 1)
                self.assertIn("connection refused", error.getvalue())

    def test_boot_readiness_retries_transient_connection_failure(self):
        with patch.object(VERIFIER.time, "sleep"), patch.object(VERIFIER.time, "monotonic", side_effect=[0, 1, 1]):
            with patch.object(VERIFIER, "APIClient") as client:
                client.side_effect = None
                api = client.return_value
                api.side_effect = [OSError("booting"), {"version": "ready"}]
                VERIFIER.wait_ready(api, 10)
                self.assertEqual(api.call_count, 2)

    def test_boot_readiness_has_a_deadline(self):
        with patch.object(VERIFIER.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(VERIFIER.VerificationError, "did not become ready"):
                VERIFIER.wait_ready(lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down")), 10)


if __name__ == "__main__":
    unittest.main()
