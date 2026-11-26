from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
LOG = ROOT / "_stage3_real.log"
DB = ROOT / "_stage3_real.db"


def wait_for(url: str, processes: list[subprocess.Popen[bytes]]) -> None:
    for _ in range(80):
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("联调服务提前退出")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"等待 {url} 超时")


def main() -> int:
    DB.unlink(missing_ok=True)
    with LOG.open("wb") as log:
        backend = subprocess.Popen(
            [sys.executable, "-m", "backend.app", "--host", "127.0.0.1", "--port", "7101", "--task-db", str(DB)],
            cwd=ROOT / "b02-workflow-agent", stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        frontend = subprocess.Popen(
            ["npm.cmd", "run", "preview", "--", "--host", "127.0.0.1", "--port", "4322"],
            cwd=FRONTEND, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        processes = [backend, frontend]
        try:
            wait_for("http://127.0.0.1:7101/api/health", processes)
            wait_for("http://127.0.0.1:4322", processes)
            env = dict(os.environ, B02_BASE="http://127.0.0.1:7101")
            result = subprocess.run(
                ["node.cmd", "tests/ui_controls.mjs", "http://127.0.0.1:4322", str(ROOT / "stage3-shots" / "real-controls.png")],
                cwd=FRONTEND, env=env,
            )
            return result.returncode
        finally:
            for process in processes:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            for process in processes:
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
