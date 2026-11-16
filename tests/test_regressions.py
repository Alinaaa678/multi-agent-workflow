from pathlib import Path
from multiagent_quality.runner import run_suite


def test_smoke_temp_root_is_created(tmp_path):
    """The smoke harness must work in a clean checkout without pre-created dirs."""
    root = tmp_path / ".test-tmp"
    root.mkdir(parents=True)
    assert root.is_dir()


def test_runner_does_not_expose_process_environment(tmp_path, monkeypatch):
    # Regression guard: run_case builds a restricted environment, rather than
    # passing arbitrary parent variables to an untrusted candidate.
    from multiagent_quality.runner import run_case
    monkeypatch.setenv("B06_SECRET_SHOULD_NOT_LEAK", "secret")
    log = tmp_path / "env.log"
    result = run_case(
        ["python", "-c", "import os; print(os.getenv('B06_SECRET_SHOULD_NOT_LEAK', 'missing'))"],
        cwd=tmp_path,
        log_file=log,
        timeout_seconds=5,
    )
    assert result["status"] == "PASS"
    assert log.read_text(encoding="utf8").strip() == "missing"
