#!/usr/bin/env python3
"""One entry point for setup, deployment, acceptance, verification and local validation."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".llmctl-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(argv, env):
    subprocess.run([str(part) for part in argv], cwd=ROOT, env=env, check=True)


def environment(use_current=False):
    env = os.environ.copy()
    if not use_current:
        env["ANSIBLE_HOME"] = str(ROOT / ".local/ansible")
        env["ANSIBLE_LOCAL_TEMP"] = str(ROOT / ".local/ansible/tmp")
        env["ANSIBLE_COLLECTIONS_PATH"] = str(ROOT / ".local/ansible/collections")
        env["XDG_CACHE_HOME"] = str(ROOT / ".local/cache")
        env["PIP_CACHE_DIR"] = str(ROOT / ".local/pip-cache")
    env["ANSIBLE_CONFIG"] = str(ROOT / "ansible.cfg")
    env["ANSIBLE_CALLBACK_PLUGINS"] = str(ROOT / "callback_plugins")
    env["ANSIBLE_CALLBACKS_ENABLED"] = "llm_report"
    return env


def bootstrap(env, development=False, use_current=False):
    if use_current:
        executable = shutil.which("ansible-playbook")
        if not executable:
            raise RuntimeError("ansible-playbook was not found in PATH")
        return Path(executable).parent
    virtualenv = ROOT / ".venv"
    binary = virtualenv / "bin"
    if not (binary / "python").exists():
        run([sys.executable, "-m", "venv", virtualenv], env)
    env["PATH"] = str(binary) + os.pathsep + env.get("PATH", "")
    dependency_files = ["requirements.txt", "requirements.yml"]
    if development:
        dependency_files.append("requirements-dev.txt")
    digest = hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in dependency_files)).hexdigest()
    marker = ROOT / (".local/development.ready" if development else ".local/runtime.ready")
    if not marker.exists() or marker.read_text() != digest:
        requirement = "requirements-dev.txt" if development else "requirements.txt"
        run([binary / "python", "-m", "pip", "install", "-r", requirement], env)
        run([binary / "ansible-galaxy", "collection", "install", "-r", "requirements.yml"], env)
        write_private(marker, digest)
    return binary


def make_inventory(args):
    if args.host:
        if not re.fullmatch(r"[A-Za-z0-9:.][A-Za-z0-9:._-]*", args.host):
            raise ValueError("--host must be an IP address or hostname, without SSH options")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", args.user):
            raise ValueError("Invalid SSH user")
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid SSH port")
        connection = {
            "ansible_host": args.host, "ansible_user": args.user, "ansible_port": args.port,
            "ansible_python_interpreter": "/usr/bin/python3",
        }
        if args.key:
            key = args.key.expanduser().resolve()
            if not key.is_file():
                raise ValueError(f"SSH key does not exist: {key}")
            connection["ansible_ssh_private_key_file"] = str(key)
        path = ROOT / "inventory/generated.yml"
        write_private(path, json.dumps({"all": {"children": {"gpu_servers": {"hosts": {"gpu01": connection}}}}}, indent=2))
        return path
    path = (args.inventory or ROOT / "inventory/hosts.yml").expanduser().resolve()
    if not path.is_file():
        raise ValueError("Provide --host or an existing --inventory file")
    return path


def assert_report(report, unchanged=False, inference=True):
    hosts = report.get("hosts", {})
    if not hosts:
        raise RuntimeError("No hosts were processed; check the inventory and --limit")
    for host, stats in hosts.items():
        if stats.get("failures", 0) or stats.get("unreachable", 0):
            raise RuntimeError(f"Ansible failed on {host}")
        if unchanged and stats.get("changed", 0) != 0:
            raise RuntimeError(f"Idempotency check failed on {host}: changed={stats.get('changed')}")
        if inference:
            check = report.get("verifications", {}).get(host, {})
            if check.get("status") != "ok" or check.get("gpu_resident_bytes", 0) <= 0:
                raise RuntimeError(f"Missing successful GPU inference evidence for {host}")


def playbook(binary, env, args, inventory, output, name, *, unchanged=False, extra=None):
    output.unlink(missing_ok=True)  # A stale successful report must never satisfy this run.
    stage_env = dict(env, LLM_REPORT_FILE=str(output))
    command = [binary / "ansible-playbook", "-i", inventory, name, "--limit", args.limit]
    if args.ask_become_pass:
        command.append("--ask-become-pass")
    for variables in args.vars_file:
        command.extend(["--extra-vars", "@" + str(variables.expanduser().resolve())])
    if extra:
        command.extend(["--extra-vars", json.dumps(extra)])
    run(command, stage_env)
    if not output.exists():
        raise RuntimeError("The Ansible callback did not produce a fresh report")
    report = json.loads(output.read_text())
    assert_report(report, unchanged=unchanged)
    return report


def check(binary, env):
    for name in ("site.yml", "verify.yml", "reboot-test.yml"):
        run([binary / "ansible-playbook", "-i", "inventory/hosts.example.yml", name, "--syntax-check"], env)
    run([binary / "ansible-lint", "site.yml", "verify.yml", "reboot-test.yml", "tests/render.yml"], env)
    run([binary / "python", "-m", "unittest", "discover", "-s", "tests", "-v"], env)
    for ids in ([], ["0"]):
        run([binary / "ansible-playbook", "-i", "localhost,", "tests/render.yml", "-e", json.dumps({"llm_gpu_device_ids": ids})], env)
        run(["docker", "compose", "-f", ".cache/compose.yml", "config", "--quiet"], env)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    for action in ("setup", "check", "deploy", "verify", "accept"):
        sub = commands.add_parser(action)
        sub.add_argument("--use-current-env", action="store_true", help="Use already installed Ansible tools (CI/offline validation)")
        if action in ("setup", "check"):
            continue
        selection = sub.add_mutually_exclusive_group()
        selection.add_argument("--host", help="Generate an inventory for one server")
        selection.add_argument("--inventory", type=Path)
        sub.add_argument("--user", default="ubuntu")
        sub.add_argument("--port", type=int, default=22)
        sub.add_argument("--key", type=Path)
        sub.add_argument("--limit", default="gpu_servers")
        sub.add_argument("--vars-file", type=Path, action="append", default=[])
        sub.add_argument("--ask-become-pass", action="store_true")
        if action == "accept":
            sub.add_argument("--reboot-test", action="store_true", help="Also reboot the selected test server and verify recovery")
    return cli


def main():
    args = parser().parse_args()
    if sys.version_info < (3, 11):
        raise RuntimeError("Controller requires Python 3.11 or newer")
    local = ROOT / ".local"
    local.mkdir(exist_ok=True, mode=0o700)
    # Keep SSH control socket paths below the Unix socket path limit.
    with (local / "operation.lock").open("w") as lock, tempfile.TemporaryDirectory(prefix="llmctl-ssh-", dir="/tmp") as control_dir:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another llmctl operation is already running in this checkout") from exc
        env = environment(args.use_current_env)
        env.setdefault("ANSIBLE_SSH_CONTROL_PATH_DIR", control_dir)
        binary = bootstrap(env, development=args.action == "check", use_current=args.use_current_env)
        if args.action == "setup":
            print("Controller dependencies are ready.")
            return 0
        if args.action == "check":
            check(binary, env)
            return 0
        inventory = make_inventory(args)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        reports = local / "reports" / run_id
        reports.mkdir(parents=True, mode=0o700)
        print(f"Reports: {reports}", flush=True)
        stages = []
        if args.action in ("deploy", "accept"):
            playbook(binary, env, args, inventory, reports / "deploy.json", "site.yml")
            stages.append("deploy")
        if args.action == "accept":
            playbook(binary, env, args, inventory, reports / "idempotency.json", "site.yml", unchanged=True,
                     extra={"llm_model_update": False})
            stages.append("idempotency")
            if args.reboot_test:
                playbook(binary, env, args, inventory, reports / "reboot.json", "reboot-test.yml",
                         extra={"llm_accept_reboot_test": True})
                stages.append("reboot")
        if args.action in ("verify", "accept"):
            playbook(binary, env, args, inventory, reports / "verify.json", "verify.yml")
            stages.append("verify")
        write_private(reports / "result.json", json.dumps({"status": "passed", "stages": stages}, indent=2) + "\n")
        print(f"PASS: {', '.join(stages)}. Reports: {reports}")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"llmctl failed: {error}", file=sys.stderr)
        sys.exit(1)
