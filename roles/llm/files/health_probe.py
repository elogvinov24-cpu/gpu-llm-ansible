#!/usr/bin/env python3
"""Read-only host/container/API probes; atomic JSON and Prometheus snapshots. No auto-restarts."""

import argparse
import csv
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def command(argv):
    result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def parse_gpus(output):
    gpus = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 5:
            raise ValueError("Unexpected nvidia-smi GPU telemetry format")
        gpu = {"uuid": row[0].strip()}
        for key, value in zip(("memory_used_mib", "memory_total_mib", "utilization_percent", "temperature_c"), row[1:]):
            try:
                gpu[key] = float(value)
            except ValueError:
                gpu[key] = None  # Unsupported metrics are omitted, not represented as zero.
        gpus.append(gpu)
    if not gpus:
        raise ValueError("nvidia-smi returned no GPUs")
    return gpus


def collect(config, api, run=command):
    report = {"timestamp": time.time(), "ok": True, "errors": [], "gpus": [], "model_loaded": False, "model_vram_bytes": 0}

    def probe(name, operation):
        try:
            operation()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.SubprocessError) as exc:
            report["ok"] = False
            report["errors"].append({"probe": name, "error": str(exc)})

    def host_gpu():
        report["gpus"] = parse_gpus(run([
            "nvidia-smi", "--query-gpu=uuid,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]))

    def container():
        compose = ["docker", "compose", "--project-directory", config["install_dir"]]
        container_id = run(compose + ["ps", "--all", "--quiet", "ollama"])
        if not container_id or "\n" in container_id:
            raise ValueError("Expected one Ollama container")
        state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", container_id]))
        if state.get("Status") != "running" or state.get("Health", {}).get("Status") != "healthy":
            raise ValueError(f"Container is not healthy: {state.get('Status')}/{state.get('Health', {}).get('Status')}")
        if "GPU" not in run(compose + ["exec", "-T", "ollama", "nvidia-smi", "-L"]):
            raise ValueError("No NVIDIA GPU is visible inside Ollama")

    def model_api():
        if not api("/api/version", timeout=10).get("version"):
            raise ValueError("Missing API version")
        installed = next((m for m in api("/api/tags", timeout=10).get("models", []) if m.get("name") == config["model"]), None)
        if installed is None:
            raise ValueError("Configured model is not installed")
        if not installed.get("digest"):
            raise ValueError("Installed model has no digest")
        if config.get("expected_digest") and installed.get("digest") != config["expected_digest"]:
            raise ValueError("Installed model digest mismatch")
        for model in api("/api/ps", timeout=10).get("models", []):
            if config["model"] not in (model.get("name"), model.get("model")):
                continue
            report["model_loaded"] = True
            report["model_vram_bytes"] = int(model.get("size_vram", 0))
            if model.get("digest") != installed.get("digest"):
                raise ValueError("Loaded model digest mismatch")
            if report["model_vram_bytes"] <= 0:
                raise ValueError("CPU fallback detected for the loaded model")

    probe("host_gpu", host_gpu)
    probe("container_gpu", container)
    probe("api", model_api)
    return report


def prometheus(report):
    lines = [
        "# TYPE gpu_llm_probe_success gauge",
        f"gpu_llm_probe_success {int(report['ok'])}",
        "# TYPE gpu_llm_probe_timestamp_seconds gauge",
        f"gpu_llm_probe_timestamp_seconds {report['timestamp']:.3f}",
        "# TYPE gpu_llm_model_loaded gauge",
        f"gpu_llm_model_loaded {int(report['model_loaded'])}",
        "# TYPE gpu_llm_model_vram_bytes gauge",
        f"gpu_llm_model_vram_bytes {report['model_vram_bytes']}",
    ]
    for key in ("memory_used_mib", "memory_total_mib", "utilization_percent", "temperature_c"):
        lines.append(f"# TYPE gpu_llm_gpu_{key} gauge")
        for gpu in report["gpus"]:
            if gpu.get(key) is not None:
                label = json.dumps(gpu["uuid"], ensure_ascii=True)
                lines.append(f"gpu_llm_gpu_{key}{{uuid={label}}} {gpu[key]}")
    return "\n".join(lines) + "\n"


def atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    loader = importlib.machinery.SourceFileLoader("gpu_llm_verifier", "/usr/local/bin/gpu-llm-check")
    spec = importlib.util.spec_from_loader(loader.name, loader)
    verifier = importlib.util.module_from_spec(spec)
    loader.exec_module(verifier)
    report = collect(config, verifier.APIClient(config["url"]))
    state_dir = Path(config["state_dir"])
    atomic_write(state_dir / "health.json", json.dumps(report, indent=2) + "\n")
    atomic_write(state_dir / "health.prom", prometheus(report))
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
