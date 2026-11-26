"""Generate the phase-one UI fixture with the real discussion controller.

The checked-in JSON is a historical snapshot produced by B02 + B03 + B04 + B06,
not hand-authored discussion content. Run from the repository root:

    python workflow_runtime/make_discussion_fixture.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "b02-workflow-agent"))

from backend.adapters import MEMBERS  # noqa: E402
from backend.controller import DiscussionController  # noqa: E402
from backend.store import BlackboardStore  # noqa: E402
from backend.workflow_definition import STAGES, TRANSITIONS  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="discussion-fixture-", dir=ROOT) as temporary:
        controller = DiscussionController(BlackboardStore(Path(temporary) / "data"))
        created = controller.create_run(
            "设计一个校园失物招领系统，支持发布、检索和认领",
            "首期保留少数意见；未经六人确认不得发布 R1；优先验证流程控制而非真实模型效果。",
            scenario="normal",
            max_rounds=2,
        )
        controller.run_until_control_point(created["run_id"])
        run = controller.confirm_requirements(created["run_id"], list(MEMBERS))
        replay = controller.replay(created["run_id"])
        run["run_id"] = "RUN-HW1-NORMAL"
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "由真实 discussion_controller 完成的正常收敛历史快照；模拟模式只读，不代表实时接口连接。",
            "workflow": {"stages": STAGES, "transitions": TRANSITIONS},
            "run": run,
            "replay": replay,
        }
        output = ROOT / "frontend" / "src" / "data" / "discussionRunFixture.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {output.relative_to(ROOT)} ({len(run['events'])} events, replay={replay['passed']})")


if __name__ == "__main__":
    main()
