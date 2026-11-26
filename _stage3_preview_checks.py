from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
LOG = ROOT / "_stage3_preview.log"


def wait_for(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(80):
        if process.poll() is not None:
            raise RuntimeError("预览服务提前退出")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"等待 {url} 超时")


def main() -> int:
    with LOG.open("wb") as log:
        process = subprocess.Popen(
            ["npm.cmd", "run", "preview", "--", "--host", "127.0.0.1", "--port", "4321"],
            cwd=FRONTEND,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            wait_for("http://127.0.0.1:4321", process)
            commands = [
                ["node.cmd", "tests/ui_smoke.mjs", "http://127.0.0.1:4321"],
                ["node.cmd", "tests/screenshots.mjs", "http://127.0.0.1:4321", str(ROOT / "stage3-shots")],
            ]
            for command in commands:
                result = subprocess.run(command, cwd=FRONTEND)
                if result.returncode != 0:
                    return result.returncode
            return 0
        finally:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
