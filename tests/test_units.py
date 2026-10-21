"""Validate rendered systemd unit configuration."""

import configparser
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def unit(name):
    variables = yaml.safe_load((ROOT / "roles/llm/defaults/main.yml").read_text())
    template = Environment(undefined=StrictUndefined).from_string((ROOT / f"roles/llm/templates/{name}.j2").read_text())
    result = configparser.ConfigParser(interpolation=None)
    result.read_string(template.render(**variables))
    return result


class UnitTests(unittest.TestCase):
    def test_boot_warmup_is_unprivileged_and_has_bounded_retries(self):
        config = unit("gpu-llm-warmup.service")
        self.assertEqual(config["Service"]["User"], "nobody")
        self.assertEqual(config["Service"]["ProtectSystem"], "strict")
        self.assertIn("--wait-ready 240", config["Service"]["ExecStart"])
        self.assertEqual(config["Service"]["TimeoutStartSec"], "660")
        self.assertGreater(int(config["Unit"]["StartLimitIntervalSec"]),
                           int(config["Unit"]["StartLimitBurst"]) * (int(config["Service"]["TimeoutStartSec"]) + 30))

    def test_health_timer_repeats_after_a_completed_probe(self):
        timer = unit("gpu-llm-health.timer")
        service = unit("gpu-llm-health.service")
        self.assertEqual(timer["Timer"]["OnUnitInactiveSec"], "60")
        self.assertNotIn("RemainAfterExit", service["Service"])
        self.assertEqual(service["Service"]["ReadWritePaths"], "/var/lib/gpu-llm")
        self.assertNotIn("Restart", service["Service"])
