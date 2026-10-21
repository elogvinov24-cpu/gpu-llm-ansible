"""Exercise the report callback with a real, harmless localhost Ansible play."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible is required for callback integration")
class CallbackIntegrationTests(unittest.TestCase):
    def test_real_ansible_stats_are_written_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            playbook = path / "fixture.yml"
            playbook.write_text("""---
- name: Harmless callback fixture
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Emit a fixture message without contacting any GPU host
      ansible.builtin.debug:
        msg: local callback integration test
""")
            env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"),
                       ANSIBLE_CALLBACK_PLUGINS=str(ROOT / "callback_plugins"),
                       ANSIBLE_CALLBACKS_ENABLED="llm_report", LLM_REPORT_FILE=str(path / "report.json"))
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook)],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads((path / "report.json").read_text())
            self.assertEqual(report["hosts"]["localhost"]["changed"], 0)
            self.assertEqual(report["hosts"]["localhost"]["ok"], 1)
            self.assertEqual(report["verifications"], {})
            self.assertNotIn("local callback integration test", json.dumps(report))
            self.assertEqual((path / "report.json").stat().st_mode & 0o777, 0o600)
