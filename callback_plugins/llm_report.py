"""Write minimal machine-readable acceptance evidence, without task arguments or secrets."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "llm_report"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self.verifications = {}
        self.failures = []

    def v2_runner_on_ok(self, result):
        if result._result.get("_ansible_no_log") or result._task.no_log:
            return
        if result._task.register == "llm_api_check":
            try:
                self.verifications[result._host.get_name()] = json.loads(result._result["stdout"])
            except (KeyError, ValueError):
                pass

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self.failures.append({"host": result._host.get_name(), "task": result._task.get_name(), "ignored": bool(ignore_errors)})

    def v2_runner_on_unreachable(self, result):
        self.failures.append({"host": result._host.get_name(), "task": "SSH connection unreachable", "ignored": False})

    def v2_playbook_on_stats(self, stats):
        destination = os.environ.get("LLM_REPORT_FILE")
        if not destination:
            return
        document = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "hosts": {host: stats.summarize(host) for host in sorted(stats.processed)},
            "verifications": self.verifications,
            "failures": self.failures,
        }
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(document, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
