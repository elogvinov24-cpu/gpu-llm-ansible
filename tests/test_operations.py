import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEALTH = load("health_probe", ROOT / "roles/llm/files/health_probe.py")
MANAGER = load("manager", ROOT / "scripts/manage.py")
MODEL = "example:7b"
CONFIG = {"model": MODEL, "install_dir": "/opt/gpu-llm", "expected_digest": "a" * 64}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.loaded = []
        self.state = {"Status": "running", "Health": {"Status": "healthy"}}
        self.commands = []

    def api(self, path, **kwargs):
        return {
            "/api/version": {"version": "fixture"},
            "/api/tags": {"models": [{"name": MODEL, "digest": "a" * 64}]},
            "/api/ps": {"models": self.loaded},
        }[path]

    def command(self, argv):
        self.commands.append(argv)
        if argv[0] == "nvidia-smi":
            return "GPU-123, 2000, 24576, 12, 45"
        if "inspect" in argv:
            return json.dumps(self.state)
        if "ps" in argv:
            return "container123"
        return "GPU 0: NVIDIA fixture"

    def test_idle_model_is_healthy_without_unnecessary_inference(self):
        result = HEALTH.collect(CONFIG, self.api, self.command)
        self.assertTrue(result["ok"])
        self.assertFalse(result["model_loaded"])
        self.assertEqual(result["gpus"][0]["memory_total_mib"], 24576)

    def test_cpu_fallback_marks_health_failure(self):
        self.loaded = [{"name": MODEL, "digest": "a" * 64, "size_vram": 0}]
        result = HEALTH.collect(CONFIG, self.api, self.command)
        self.assertFalse(result["ok"])
        self.assertIn("CPU fallback", str(result["errors"]))
        self.assertIn("gpu_llm_probe_success 0", HEALTH.prometheus(result))

    def test_stopped_container_is_not_restarted(self):
        self.state = {"Status": "exited"}
        result = HEALTH.collect(CONFIG, self.api, self.command)
        self.assertFalse(result["ok"])
        self.assertNotIn("restart", str(self.commands))
        self.assertNotIn("up", str(self.commands))

    def test_host_failure_does_not_suppress_api_check(self):
        def fail(argv):
            raise subprocess.TimeoutExpired(argv, 15)
        result = HEALTH.collect(CONFIG, self.api, fail)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["errors"]), 2)

    def test_unsupported_metric_is_omitted_instead_of_reported_as_zero(self):
        gpus = HEALTH.parse_gpus("GPU-1, 100, 24000, [N/A], 40")
        result = {"ok": True, "timestamp": 1, "model_loaded": False, "model_vram_bytes": 0, "gpus": gpus}
        self.assertNotIn('gpu_llm_gpu_utilization_percent{', HEALTH.prometheus(result))

    def test_snapshot_writes_replace_previous_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            HEALTH.atomic_write(path, '{"ok":false}')
            HEALTH.atomic_write(path, '{"ok":true}')
            self.assertTrue(json.loads(path.read_text())["ok"])
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["health.json"])


class AcceptanceTests(unittest.TestCase):
    def report(self, changed=0):
        return {"hosts": {"gpu01": {"changed": changed, "failures": 0, "unreachable": 0}},
                "verifications": {"gpu01": {"status": "ok", "gpu_resident_bytes": 42}}}

    def test_accepts_gpu_evidence_and_zero_changes(self):
        MANAGER.assert_report(self.report(), unchanged=True)

    def test_rejects_changes_on_second_deployment(self):
        with self.assertRaisesRegex(RuntimeError, "Idempotency"):
            MANAGER.assert_report(self.report(changed=1), unchanged=True)

    def test_no_hosts_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "No hosts"):
            MANAGER.assert_report({"hosts": {}})

    def test_report_without_gpu_evidence_is_rejected(self):
        report = self.report()
        report["verifications"] = {}
        with self.assertRaisesRegex(RuntimeError, "Missing successful GPU"):
            MANAGER.assert_report(report)

    def test_stale_report_cannot_satisfy_a_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stage.json"
            output.write_text(json.dumps(self.report()))
            args = MANAGER.parser().parse_args(["verify", "--host", "192.0.2.10"])
            with patch.object(MANAGER, "run"):
                with self.assertRaisesRegex(RuntimeError, "fresh report"):
                    MANAGER.playbook(Path(directory), {}, args, Path("inventory"), output, "verify.yml")
            self.assertFalse(output.exists())

    def test_inventory_cannot_inject_ssh_arguments(self):
        args = MANAGER.parser().parse_args(["deploy", "--host=--proxy-command=bad"])
        with self.assertRaisesRegex(ValueError, "IP address or hostname"):
            MANAGER.make_inventory(args)

    def test_generated_inventory_has_private_permissions_and_quoted_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            args = MANAGER.parser().parse_args(["deploy", "--host", "192.0.2.10", "--user", "root", "--port", "2222"])
            with patch.object(MANAGER, "ROOT", Path(directory)):
                path = MANAGER.make_inventory(args)
                data = json.loads(path.read_text())
                self.assertEqual(data["all"]["children"]["gpu_servers"]["hosts"]["gpu01"]["ansible_port"], 2222)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
