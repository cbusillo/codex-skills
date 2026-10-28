from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["HEAVY_FIXTURE"])
root.mkdir(exist_ok=True)
args = sys.argv[1:]
if args[:1] == ["--host"]:
    args = args[2:]
with (root / "lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with (root / "calls").open("a") as calls:
        calls.write(json.dumps(args) + "\n")
    if args[0] == "context":
        print("unix:///fixture/docker.sock")
    elif args[0] == "create":
        name = args[args.index("--name") + 1]
        data = {"name": name, "job": args[-1], "label": args[args.index("--label") + 1]}
        (root / f"container-{name}").write_text(json.dumps(data))
        print(name)
    elif args[0] == "ps":
        label = args[-1].removeprefix("label=")
        print(
            "\n".join(
                json.loads(p.read_text())["name"] for p in root.glob("container-*") if json.loads(p.read_text())["label"] == label
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
            print(str(running).lower() if "Running" in args[2] else "0")
        elif args[0] == "logs":
            print("private-test-output")
        elif os.environ.get("HEAVY_FAIL_CLEANUP") == "1":
            sys.exit(1)
        else:
            (root / f"running-{job}").unlink(missing_ok=True)
            (root / f"container-{args[-1]}").unlink()
    elif args[0] == "network":
        if args[1] == "create":
            (root / f"network-{args[-1]}").touch()
            print(args[-1])
        elif args[1] == "ls":
            print("\n".join(p.name.removeprefix("network-") for p in root.glob("network-*")))
        elif args[1] == "rm":
            (root / f"network-{args[-1]}").unlink()

if args[0] == "create" and os.environ.get("HEAVY_HOLD_CREATE") == "1":
    (root / f"creating-{args[-1]}").touch()
    while not (root / "finish-creating").exists():
        time.sleep(0.02)
