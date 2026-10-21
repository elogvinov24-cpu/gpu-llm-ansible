"""Check security and device-selection invariants for both template branches."""

import json
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(device_ids):
    defaults = yaml.safe_load((ROOT / "roles/llm/defaults/main.yml").read_text())
    defaults["llm_gpu_device_ids"] = device_ids
    environment = Environment(undefined=StrictUndefined)
    environment.filters["to_json"] = json.dumps
    template = environment.from_string((ROOT / "roles/llm/templates/compose.yml.j2").read_text())
    return yaml.safe_load(template.render(**defaults))["services"]["ollama"]


class ComposeTests(unittest.TestCase):
    def test_only_loopback_is_published(self):
        self.assertEqual(render([])["ports"], ["127.0.0.1:11434:11434"])

    def test_all_gpu_and_selected_gpu_modes_are_exclusive(self):
        for device_ids in ([], ["GPU-example-uuid"], ["0", "1"]):
            with self.subTest(device_ids=device_ids):
                device = render(device_ids)["deploy"]["resources"]["reservations"]["devices"][0]
                self.assertEqual(device["driver"], "nvidia")
                self.assertEqual(device["capabilities"], ["gpu"])
                if device_ids:
                    self.assertNotIn("count", device)
                    self.assertEqual(device["device_ids"], device_ids)
                else:
                    self.assertEqual(device["count"], "all")
                    self.assertNotIn("device_ids", device)

    def test_restart_persistence_health_and_security(self):
        service = render([])
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertIn("/var/lib/ollama:/root/.ollama", service["volumes"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["environment"]["OLLAMA_NO_CLOUD"], "1")
        self.assertTrue(service["healthcheck"]["test"])
        self.assertFalse(service.get("privileged", False))
        self.assertNotIn("docker.sock", str(service))


if __name__ == "__main__":
    unittest.main()
