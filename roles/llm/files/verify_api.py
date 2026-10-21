#!/usr/bin/env python3
"""Verify local Ollama inference and reject a silent CPU fallback. Standard library only."""

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request


class VerificationError(RuntimeError):
    """The server is reachable but does not meet deployment acceptance criteria."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


class APIClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        # Loopback/SSH tunnel calls must not be redirected through an environment proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(self, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=timeout) as response:
            result = json.load(response)
        require(isinstance(result, dict), f"{path}: expected a JSON object")
        require(not result.get("error"), f"{path}: {result.get('error')}")
        return result


def wait_ready(api, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            api("/api/version", timeout=min(5, max(1, seconds)))
            return
        except (OSError, ValueError, VerificationError) as exc:
            if time.monotonic() >= deadline:
                raise VerificationError(f"API did not become ready within {seconds}s: {exc}") from exc
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def verify(api, model, context_length=2048, timeout=300, expected_digest=""):
    version = api("/api/version").get("version")
    require(isinstance(version, str) and version, "Ollama did not return its version")
    catalogue = api("/api/tags").get("models", [])
    installed = next((entry for entry in catalogue if entry.get("name") == model), None)
    require(installed is not None, f"Model is missing: {model}")
    digest = installed.get("digest", "")
    require(isinstance(digest, str) and digest, "Installed model has no digest")
    if expected_digest:
        require(digest == expected_digest, "Model digest does not match llm_model_expected_digest")

    result = api(
        "/api/generate",
        {
            "model": model,
            "prompt": "Reply with a short greeting.",
            "stream": False,
            "keep_alive": "2m",
            "options": {"temperature": 0, "num_predict": 32, "num_ctx": context_length},
        },
        timeout=timeout,
    )
    require(result.get("done") is True, "Generation did not finish")
    require(isinstance(result.get("response"), str) and result["response"].strip(), "Empty model response")
    require(int(result.get("eval_count", 0)) > 0, "Model did not generate any tokens")

    running = api("/api/ps").get("models", [])
    loaded = next(
        (entry for entry in running if entry.get("name") == model or entry.get("model") == model),
        None,
    )
    require(loaded is not None, "Requested model is not resident after generation")
    require(loaded.get("digest") == digest, "Running model digest differs from the installed model")
    vram_bytes = int(loaded.get("size_vram", 0))
    require(vram_bytes > 0, "CPU fallback detected: size_vram=0. Check GPU access, driver and free VRAM")
    return {
        "status": "ok",
        "ollama_version": version,
        "model": model,
        "model_digest": digest,
        "generated_tokens": int(result["eval_count"]),
        "gpu_resident_bytes": vram_bytes,
        "note": "Positive VRAM proves GPU offload; it does not guarantee 100% GPU residency.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--model")
    parser.add_argument("--context-length", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--expected-digest")
    parser.add_argument("--wait-ready", type=int, default=0)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text()) if args.config else {}
        api = APIClient(args.url or config.get("url", "http://127.0.0.1:11434"))
        if args.wait_ready > 0:
            wait_ready(api, args.wait_ready)
        result = verify(
            api,
            args.model or config.get("model", "qwen2.5:7b-instruct-q4_K_M"),
            args.context_length or config.get("context_length", 2048),
            args.timeout or config.get("timeout", 300),
            args.expected_digest if args.expected_digest is not None else config.get("expected_digest", ""),
        )
    except (VerificationError, OSError, ValueError, TypeError, KeyError, AttributeError, urllib.error.URLError) as exc:
        print(f"LLM verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
