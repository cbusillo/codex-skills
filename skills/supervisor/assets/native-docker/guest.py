from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def main() -> None:
    control = Path("/workspace/.heavy-vm")
    request = json.loads((control / "request.json").read_text())
    environment = {key: value for key, value in request["environment"].items() if not key.startswith("DOCKER_")}
    environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    code = 1
    try:
        daemon = subprocess.Popen(
            [
                "dockerd",
                "--host=unix:///var/run/docker.sock",
                "--storage-driver=overlay2",
                "--userland-proxy-path=/usr/bin/docker-proxy",
            ]
        )
        try:
            deadline = time.monotonic() + 60
            while subprocess.run(["docker", "info"], env=environment, capture_output=True, timeout=5).returncode:
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Native Docker did not become ready")
                time.sleep(0.2)
            code = subprocess.run(request["command"], env=environment, cwd="/workspace").returncode
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
    finally:
        (control / "exit-code").write_text(str(code if code >= 0 else 128 - code))
        os.sync()
        subprocess.run(["/bin/busybox", "poweroff", "-f"], check=True)


if __name__ == "__main__":
    main()
