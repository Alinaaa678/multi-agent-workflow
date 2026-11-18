"""Trusted process runner. Suites are operator configuration, never model input."""
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .evidence import artifact_path, sha256_file, sign_report
from .validation import obj, text, integer, unique_strings, sequence, ContractError

def _kill_tree(process):
    if process.poll() is not None: return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if process.poll() is None: process.kill()

def run_case(command, *, cwd, log_file, timeout_seconds, max_output_bytes=1048576, env_extra=None):
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ContractError("command must be a nonempty argv array")
    integer(timeout_seconds, "timeout_seconds", 1); integer(max_output_bytes, "max_output_bytes", 1)
    # Do not inherit model credentials or the attestation signing key.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "COMSPEC", "PATHEXT"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    if env_extra: env.update(env_extra)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic(); limit = threading.Event()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **options)
    except OSError as exc:
        Path(log_file).write_text(str(exc), encoding="utf-8")
        return {"status": "ERROR", "exit_code": None, "duration_seconds": time.monotonic() - started}
    def drain():
        count = 0
        with Path(log_file).open("wb") as log:
            while chunk := process.stdout.read(4096):
                remaining = max_output_bytes - count
                if remaining > 0: log.write(chunk[:remaining])
                count += len(chunk)
                if count > max_output_bytes:
                    limit.set(); _kill_tree(process); break
    reader = threading.Thread(target=drain, daemon=True); reader.start()
    status = None
    try: process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        status = "TIMEOUT"; _kill_tree(process); process.wait(timeout=5)
    reader.join(timeout=5)
    # A child inheriting stdout can outlive its parent; do not report success.
    if reader.is_alive():
        status = "ERROR"
    if limit.is_set(): status = "OUTPUT_LIMIT"
    if status is None: status = "PASS" if process.returncode == 0 else "FAIL"
    return {"status": status, "exit_code": process.returncode, "duration_seconds": time.monotonic() - started}

def run_suite(suite, *, context, root, key):
    obj(suite, "suite"); obj(context, "context")
    base = Path(root).resolve(strict=True)
    cases = sequence(suite.get("cases"), "suite.cases")
    ids = [obj(c, "case").get("test_id") for c in cases]
    unique_strings(ids, "suite test IDs")
    artifacts = unique_strings(suite.get("required_artifacts"), "required_artifacts")
    # Fail before execution when prescribed input artifacts are missing.
    before = {name: sha256_file(artifact_path(base, name)) for name in artifacts}
    run_id = uuid4().hex
    log_dir = base / "reports" / run_id; log_dir.mkdir(parents=True)
    started = datetime.now(timezone.utc).isoformat(); results = []
    for case in cases:
        cid = text(case["test_id"], "test_id")
        # Filenames are generated independently of test identifiers.
        logfile = log_dir / f"case-{len(results)}.log"
        result = run_case(case.get("command"), cwd=base, log_file=logfile,
                          timeout_seconds=case.get("timeout_seconds", 30),
                          max_output_bytes=case.get("max_output_bytes", 1048576),
                          env_extra=suite.get("environment"))
        results.append({"test_id": cid, **result, "log_path": logfile.relative_to(base).as_posix(),
                        "log_sha256": sha256_file(logfile)})
    files = []
    for name, digest in before.items():
        current = sha256_file(artifact_path(base, name))
        if current != digest: raise ContractError(f"test changed candidate artifact: {name}")
        files.append({"path": name, "sha256": current})
    report = {"schema_version": 1, "context": context, "started_at": started,
              "finished_at": datetime.now(timezone.utc).isoformat(), "cases": results, "artifacts": files}
    return sign_report(report, key)
