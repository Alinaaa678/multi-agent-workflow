"""Read-only T09 plan quality checks, not task generation or scheduling."""
from .validation import obj, text, sequence, unique_strings, verdict, ContractError

def validate_plan(plan, required_requirements):
    obj(plan, "plan")
    required = set(unique_strings(required_requirements, "required_requirements"))
    text(plan.get("spec_version"), "spec_version")
    text(plan.get("contract_version"), "contract_version")
    tasks = sequence(plan.get("tasks"), "tasks")
    if not tasks:
        return verdict(False, ["empty task plan"], topological_order=[])
    registry = {}; outputs = {}; covered = set(); reasons = []
    for task in tasks:
        obj(task, "task"); tid = text(task.get("task_id"), "task_id")
        if tid in registry:
            raise ContractError(f"duplicate task_id: {tid}")
        registry[tid] = task
        text(task.get("goal"), "goal")
        refs = unique_strings(task.get("req_refs"), "req_refs")
        if set(refs) - required:
            reasons.append(f"{tid}: unknown requirements")
        covered.update(refs)
        unique_strings(task.get("depends_on"), "depends_on", nonempty=False)
        unique_strings(task.get("acceptance"), "acceptance")
        unique_strings(task.get("required_test_ids"), "required_test_ids")
        for output in unique_strings(task.get("outputs"), "outputs"):
            if output in outputs:
                reasons.append(f"output has two owners: {output}")
            outputs[output] = tid
    reasons.extend(f"uncovered requirement: {r}" for r in sorted(required - covered))
    # Kahn's algorithm avoids recursion depth errors on generated plans.
    indegree = {tid: 0 for tid in registry}; successors = {tid: [] for tid in registry}
    for tid, task in registry.items():
        for dep in task["depends_on"]:
            if dep not in registry:
                reasons.append(f"{tid}: missing dependency {dep}")
            else:
                indegree[tid] += 1; successors[dep].append(tid)
    import heapq
    ready = [tid for tid in registry if indegree[tid] == 0]; heapq.heapify(ready)
    order = []
    while ready:
        tid = heapq.heappop(ready); order.append(tid)
        for child in successors[tid]:
            indegree[child] -= 1
            if indegree[child] == 0: heapq.heappush(ready, child)
    if len(order) != len(registry):
        reasons.append("dependency cycle")
    return verdict(not reasons, reasons, topological_order=order)
