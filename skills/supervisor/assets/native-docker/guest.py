from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def wait_for_docker(daemon: subprocess.Popen[bytes], environment: dict[str, str]) -> None:
    deadline = time.monotonic() + 60
    while daemon.poll() is None and time.monotonic() < deadline:
        try:
            result = subprocess.run(["docker", "info"], env=environment, capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Native Docker did not become ready")


def main() -> None:
    control = Path("/native/control")
    request = json.loads((control / "request.json").read_text())
    (control / "request.json").unlink()
    environment = dict(request["environment"])
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(key, None)
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
            wait_for_docker(daemon, environment)
            code = subprocess.run(request["command"], env=environment, cwd="/workspace").returncode
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Native Docker guest failed: {error}", flush=True)
    finally:
        (control / "exit-code").write_text(str(code if code >= 0 else 128 - code))
        os.sync()
        subprocess.run(["/bin/busybox", "poweroff", "-f"], check=True)


if __name__ == "__main__":
    main()
