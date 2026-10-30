"""Run bounded pilot batches on the managed cloud Docker daemon, without host credentials.

This controller does not configure the platform, provision storage, or publish an ingress.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
GIB = 1024**3
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if not path.is_symlink() and path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # Temporary SQLite files may disappear while the batch is running.
            continue
    return total


def capacity(root: Path, max_bytes: int, reserve_bytes: int) -> dict[str, Any]:
    used = tree_bytes(root)
    free = shutil.disk_usage(root).free
    return {"ok": used < max_bytes and free >= reserve_bytes, "used_bytes": used, "free_bytes": free}


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def environment(root: Path, image: str) -> dict[str, str]:
    result = os.environ.copy()
    # Select the session's managed daemon, preserving Docker credentials and proxy defaults.
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        result.pop(name, None)
    result.update(
        WATT_PILOT_ROOT=str(root),
        WATT_PILOT_UID=str(os.getuid()),
        WATT_PILOT_GID=str(os.getgid()),
        WATT_PILOT_IMAGE=image,
        WATT_PILOT_PROJECT="watt-pilot-" + hashlib.sha256(str(root).encode()).hexdigest()[:12],
        BUILDX_CONFIG=str(root / "control" / "buildx"),
    )
    return result


def compose() -> list[str]:
    # Never read the repository's production .env, nor merge its services/secret bindings.
    return [*DOCKER, "compose", "--env-file", "/dev/null", "-f", str(REPO / "compose.cloud-pilot.yaml")]


def missing_hosts(policy: dict[str, Any], required: list[str]) -> list[str]:
    if policy.get("version") != 1 or policy.get("http_network_policy", {}).get("type") != "restricted":
        raise ValueError("Expected a version-1 restricted platform network policy")
    allowed = {rule.get("host") for rule in policy["http_network_policy"].get("egress_rules", [])}
    return sorted(set(required) - allowed)


def stop(env: dict[str, str]) -> None:
    found = subprocess.check_output(
        [*DOCKER, "ps", "-q", "--filter", f"label=com.docker.compose.project={env['WATT_PILOT_PROJECT']}"],
        env=env,
        text=True,
    ).split()
    if found:
        subprocess.run([*DOCKER, "stop", "--time", "10", *found], env=env, check=True, stdout=subprocess.DEVNULL)


def run_batch(args: argparse.Namespace, root: Path, env: dict[str, str]) -> dict[str, Any]:
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("Supply a command: run --mode offline -- radar tasks status")
    if args.mode == "collect":
        request = json.loads((REPO / ".codex/cloud-pilot.request.json").read_text())
        policy = json.loads(Path("/etc/codex/network-policy.json").read_text())
        missing = missing_hosts(policy, request["collector"]["explicit_hosts"])
        if missing:
            raise ValueError(f"Platform destinations missing: {', '.join(missing)}. Apply cloud configuration first.")
    tag = f"batch-{time.time_ns()}"
    report_path = root / "reports" / f"{tag}.json"
    report: dict[str, Any] = {"state": "running", "mode": args.mode, "command": command}
    write_json(report_path, report)
    before = capacity(root, args.max_bytes, args.reserve_bytes)
    if not before["ok"]:
        report.update(state="incomplete", reason="storage_budget", capacity=before)
        write_json(report_path, report)
        return {"report": str(report_path), **report}
    name = f"{env['WATT_PILOT_PROJECT']}-{tag}"
    service = "collector" if args.mode == "collect" else "offline"
    with report_path.with_suffix(".log").open("wb") as log:
        child = subprocess.Popen(
            [*compose(), "run", "--rm", "--no-deps", "--name", name, service, *command],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            while child.poll() is None:
                current = capacity(root, args.max_bytes, args.reserve_bytes)
                if not current["ok"]:
                    subprocess.run(
                        [*DOCKER, "stop", "--time", "10", name], env=env, check=False, stdout=log, stderr=log
                    )
                    report.update(state="incomplete", reason="storage_budget", capacity=current)
                    break
                time.sleep(1)
            code = child.wait()
            if report["state"] == "running":
                # Exit zero is process success; task/document completeness remains in radar's JSON log.
                report.update(state="process_exited", exit_code=code)
        except (KeyboardInterrupt, OSError) as exc:
            subprocess.run([*DOCKER, "stop", "--time", "10", name], env=env, check=False, stdout=log, stderr=log)
            child.wait()
            report.update(
                state="incomplete", reason="operator_interrupt" if isinstance(exc, KeyboardInterrupt) else str(exc)
            )
    report["capacity"] = capacity(root, args.max_bytes, args.reserve_bytes)
    if not report["capacity"]["ok"]:
        report.update(state="incomplete", reason="storage_budget")
    write_json(report_path, report)
    return {"report": str(report_path), **report}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Examples: python tools/cloud_pilot.py --root /workspace/watt-pilot-private/first init",
    )
    parser.add_argument("--root", type=Path, required=True, help="private pilot root outside the public repository")
    parser.add_argument("--image", default="watt-horizon-pilot:cloud")
    actions = parser.add_subparsers(dest="action", required=True)
    examples = {
        "init": "init",
        "check": "check",
        "build": "build",
        "stop": "stop",
        "review": "review",
        "run": "run --mode offline -- radar tasks status",
    }
    for action, example in examples.items():
        sub = actions.add_parser(
            action, epilog=f"Examples: python tools/cloud_pilot.py --root /workspace/watt-pilot-private/first {example}"
        )
        if action == "run":
            sub.add_argument("--mode", choices=("offline", "collect"), default="offline")
            sub.add_argument(
                "--max-bytes", type=int, default=6 * GIB, help="monitored total pilot bytes, including backups"
            )
            sub.add_argument(
                "--reserve-bytes", type=int, default=6 * GIB, help="minimum free space for snapshots and recovery"
            )
            sub.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    try:
        if root == REPO or root.is_relative_to(REPO) or REPO.is_relative_to(root):
            raise ValueError("Pilot root must be outside the public repository and must not contain it")
        if os.getuid() == 0:
            raise ValueError("Run the pilot controller as a non-root cloud user with managed Docker socket access")
        if args.action == "init":
            if root.exists() and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
                raise ValueError("Existing pilot root must belong to this user and have mode 0700")
            for folder in (
                root,
                root / "data/gold",
                root / "backups",
                root / "control",
                root / "reports",
                root / "inputs",
                root / "restores",
            ):
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            token = root / "control/review-token"
            if not token.exists():
                with token.open("x", encoding="utf-8") as stream:
                    stream.write(secrets.token_urlsafe(32))
            smoke = root / "inputs/network-smoke.json"
            if not smoke.exists():
                with smoke.open("x", encoding="utf-8") as stream:
                    stream.write((REPO / ".codex/pilot-network-smoke.json").read_text(encoding="utf-8"))
            pricing = root / "inputs/llm-pricing.json"
            if not pricing.exists():
                with pricing.open("x", encoding="utf-8") as stream:
                    stream.write("{}\n")
            result: Any = {
                "state": "local_directories_prepared",
                "root": str(root),
                "cloud_persistence_verified": False,
            }
        else:
            if not (root / "control/review-token").is_file():
                raise ValueError("Initialize this pilot first: --root PRIVATE_DIRECTORY init")
            env = environment(root, args.image)
            if args.action == "check":
                policy = json.loads(Path("/etc/codex/network-policy.json").read_text())
                request = json.loads((REPO / ".codex/cloud-pilot.request.json").read_text())
                result = {
                    "state": "configuration_incomplete",
                    "full_pilot_ready": False,
                    "offline_runtime": "not_checked_by_this_command",
                    "capacity": capacity(root, 6 * GIB, 6 * GIB),
                    "missing_hosts": missing_hosts(policy, request["collector"]["explicit_hosts"]),
                    "persistence": "unverified",
                    "private_ingress": "unconfigured",
                }
            elif args.action == "build":
                subprocess.run(
                    [
                        *DOCKER,
                        "build",
                        "--secret",
                        "id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt",
                        "-t",
                        args.image,
                        str(REPO),
                    ],
                    env=env,
                    check=True,
                    stdout=sys.stderr,
                )
                result = {"state": "image_built", "image": args.image}
            elif args.action == "stop":
                stop(env)
                result = {"state": "containers_stopped", "data_preserved": True}
            elif args.action == "review":
                subprocess.run(
                    [*compose(), "up", "-d", "--no-build", "review"],
                    env=env,
                    check=True,
                    stdout=sys.stderr,
                )
                result = {"state": "loopback_only", "url": "http://127.0.0.1:8766", "hosted_browser_access": False}
            else:
                if args.max_bytes < 1 or args.reserve_bytes < 0:
                    raise ValueError("Storage limits must be positive (reserve may be zero)")
                result = run_batch(args, root, env)
        print(json.dumps(result, ensure_ascii=False))
        if isinstance(result, dict) and (result.get("state") == "incomplete" or result.get("exit_code", 0) != 0):
            return 2
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
