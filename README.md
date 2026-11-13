# B06 QualityConsensusAgent

This block evaluates proposals, plans, execution evidence, and defects. It does not write task state or dispatch work; B02 owns those transitions.

## Run without third-party test tools

```powershell
python -m tests.smoke
```

The module exposes `QualityConsensusAgent`, `evaluate_consensus`, `validate_plan`, `check_evidence`, `run_suite`, and `weighted_score`.

The trusted runner signs evidence in a process that must be kept separate from model workers. The signing key must never be passed in a task prompt or model environment. A report is accepted only when its context, timestamps, test IDs, exit codes, logs, artifact hashes, and prescribed evidence all match the frozen snapshot.

`attempt_id` identifies every dispatch and increases on revalidation. `retry_count` counts failure retries only; upstream invalidation uses `revalidation_id` and does not consume the retry budget until the revalidation itself fails.
