from __future__ import annotations

import fcntl
import json
import os
import sys
import tarfile
import time
from decimal import Decimal
from pathlib import Path

root = Path(os.environ["HEAVY_FIXTURE"])
root.mkdir(exist_ok=True)
args = sys.argv[1:]
endpoint = args[1] if args[:1] == ["--host"] else ""
if args[:1] in (["--host"], ["--config"]):
    args = args[2:]
with (root / "lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with (root / "calls").open("a") as calls:
        calls.write(json.dumps(args) + "\n")
    if args[0] == "context":
        print(os.environ["HEAVY_ENDPOINT"])
    elif args[0] == "create":
        cpus = Decimal(args[args.index("--cpus") + 1]) * 10**9
        if cpus != int(cpus):
            sys.exit("value is too precise")
        name = args[args.index("--name") + 1]
        data = {"name": name, "job": args[-1], "label": args[args.index("--label") + 1], "endpoint": endpoint}
        if "--env-file" in args:
            env_file = Path(args[args.index("--env-file") + 1])
            (root / f"env-mode-{args[-1]}").write_text(str(env_file.stat().st_mode & 0o777))
        (root / f"container-{name}").write_text(json.dumps(data))
        print(name)
    elif args[0] == "ps":
        label = args[-1].removeprefix("label=")
        print(
            "\n".join(
                json.loads(p.read_text())["name"]
                for p in root.glob("container-*")
                if json.loads(p.read_text())["label"] == label and json.loads(p.read_text())["endpoint"] == endpoint
            )
        )
    elif args[0] in {"start", "inspect", "logs", "rm"}:
        data = json.loads((root / f"container-{args[-1]}").read_text())
        job = data["job"]
        if args[0] == "start":
            (root / f"running-{job}").touch()
            (root / f"started-{job}").touch()
        elif args[0] == "inspect":
            running = not (root / f"release-{job}").exists()
            print(str(running).lower() if "Running" in args[2] else os.environ["HEAVY_EXIT_CODE"])
        elif args[0] == "logs":
            print(f"private-test-output:{job}")
        elif os.environ.get("HEAVY_FAIL_CLEANUP") == "1":
            sys.exit(1)
        else:
            (root / f"running-{job}").unlink(missing_ok=True)
            (root / f"container-{args[-1]}").unlink()
    elif args[:2] == ["cp", "-"]:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
            members = [{"name": entry.name, "mode": entry.mode} for entry in archive]
        (root / f"copy-{args[2].split(':')[0]}").write_text(json.dumps(members))
    elif args[0] == "cp" and ":" in args[1] and os.environ.get("HEAVY_FAIL_OUTPUT") == "1":
        sys.exit(1)
    elif args[0] == "network":
        if args[1] == "create":
            (root / f"network-{args[-1]}").write_text(json.dumps({"endpoint": endpoint, "label": args[args.index("--label") + 1]}))
            print(args[-1])
        elif args[1] == "ls":
            print(
                "\n".join(
                    p.name.removeprefix("network-")
                    for p in root.glob("network-*")
                    if json.loads(p.read_text())["endpoint"] == endpoint
                )
            )
        elif args[1] == "rm":
            (root / f"network-{args[-1]}").unlink()

if args[0] == "create" and os.environ.get("HEAVY_HOLD_CREATE") == "1":
    (root / f"creating-{args[-1]}").touch()
    deadline = time.monotonic() + 15
    while not (root / "finish-creating").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
