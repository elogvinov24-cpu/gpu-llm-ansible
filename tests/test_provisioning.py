"""Run real Ansible preflight conditions against cloud image/driver fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible is required for provisioning regression checks")
class ProvisioningTests(unittest.TestCase):
    def run_cases(self, tasks, cases, extra_assertions=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            inventory = {"all": {"hosts": {
                name: dict(values, ansible_connection="local") for name, values in cases.items()
            }}}
            play = [{
                "name": "Validate provisioning regression fixtures without modifying a host",
                "hosts": "all", "gather_facts": False,
                "vars": {"fixture_check_task": tasks[-1]["name"]},
                "tasks": [
                    {"name": "Initialize rejection state", "ansible.builtin.set_fact": {"fixture_rejected": False}},
                    {"name": "Load fixture package facts", "ansible.builtin.set_fact": {
                        "ansible_facts": "{{ fixture_facts | default({}) }}",
                    }},
                    {"name": "Evaluate actual project tasks", "block": tasks, "rescue": [
                        {"name": "Require an intentional validation rejection", "ansible.builtin.assert": {
                            "that": "ansible_failed_task.name == fixture_check_task",
                        }},
                        {"name": "Record rejected input", "ansible.builtin.set_fact": {"fixture_rejected": True}},
                    ]},
                    {"name": "Check the expected outcome", "ansible.builtin.assert": {
                        "that": ["fixture_rejected == expected_rejected"] + (extra_assertions or []),
                    }},
                ],
            }]
            (path / "inventory.json").write_text(json.dumps(inventory))
            (path / "fixture.yml").write_text(yaml.safe_dump(play, sort_keys=False))
            env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"))
            env.pop("LLM_REPORT_FILE", None)
            result = subprocess.run(
                ["ansible-playbook", "-i", str(path / "inventory.json"), str(path / "fixture.yml")],
                env=env, cwd=ROOT, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_headless_driver_families_and_ambiguity(self):
        source = yaml.safe_load((ROOT / "roles/llm/tasks/driver.yml").read_text())
        tasks = [task for task in source if task["name"] in {
            "Find matching utilities for installed NVIDIA driver families",
            "Require an unambiguous installed driver package",
        }]
        self.assertEqual(len(tasks), 2)
        fixtures = {
            "precompiled_headless": (["nvidia-headless-no-dkms-595-server-open"], ["nvidia-utils-595-server"], False),
            "dkms_headless": (["nvidia-headless-580-server"], ["nvidia-utils-580-server"], False),
            "full_driver": (["nvidia-driver-570-open"], ["nvidia-utils-570"], False),
            "same_family": (["nvidia-driver-595-server-open", "nvidia-headless-no-dkms-595-server-open"],
                            ["nvidia-utils-595-server"], False),
            "multiple_families": (["nvidia-driver-570", "nvidia-headless-595-server"], [], True),
            "no_driver": (["nvidia-settings"], [], True),
        }
        cases = {name: {
            "fixture_facts": {"packages": {package: [] for package in packages}},
            "llm_driver_install_needed": True, "llm_nvidia_driver_package": "",
            "expected_rejected": rejected, "expected_utilities": utilities,
        } for name, (packages, utilities, rejected) in fixtures.items()}
        self.run_cases(tasks, cases, ["expected_rejected or llm_installed_driver_utilities == expected_utilities"])

    def test_cloud_warnings_are_distinguished_from_errors(self):
        source = yaml.safe_load((ROOT / "site.yml").read_text())[0]["pre_tasks"]
        tasks = [task for task in source if task["name"] == "Require completed cloud provisioning without errors"]
        self.assertEqual(len(tasks), 1)
        fixtures = {
            "completed": ({"status": "done", "errors": []}, False),
            "disabled": ({"status": "disabled"}, False),
            "warnings": ({"status": "done", "errors": [], "recoverable_errors": {
                "WARNING": ["provider network schema warning"], "DEPRECATED": ["deprecated provider option"],
            }}, False),
            "unfinished": ({"status": "running", "errors": []}, True),
            "fatal": ({"status": "done", "errors": ["failed module"]}, True),
            "recoverable_error": ({"status": "done", "recoverable_errors": {"ERROR": ["failed configuration"]}}, True),
            "critical": ({"status": "done", "recoverable_errors": {"CRITICAL": ["critical configuration failure"]}}, True),
        }
        cases = {name: {
            "llm_cloud_init": {"stdout": json.dumps(status)}, "expected_rejected": rejected,
        } for name, (status, rejected) in fixtures.items()}
        self.run_cases(tasks, cases)
