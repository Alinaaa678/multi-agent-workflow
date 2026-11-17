"""Rules evaluate one version of one proposal; model votes are not proof."""
import math
from .validation import ContractError, obj, text, sequence, integer, number, unique_strings, enum

def weighted_score(ratings, weights):
    obj(ratings, "ratings"); obj(weights, "weights")
    if not weights or set(ratings) != set(weights):
        raise ContractError("ratings and weights must have the same nonempty dimensions")
    for key, weight in weights.items():
        text(key, "dimension")
        if not 0 <= number(weight, key) <= 1:
            raise ContractError("weights must be within 0..1")
        if not 1 <= number(ratings[key], key) <= 5:
            raise ContractError("ratings must be within 1..5")
    if not math.isclose(math.fsum(weights.values()), 1, abs_tol=1e-9, rel_tol=0):
        raise ContractError("weights must sum to 1")
    return math.fsum(ratings[k] * weights[k] for k in weights)

def evaluate_consensus(data):
    """Input is authoritative B03 state; authentication belongs at that boundary.

    A passed result is a proposal decision, never a code-test acceptance event.
    The caller must supply the configured six humans, not model-selected voters.
    """
    obj(data, "consensus")
    version = text(data.get("proposal_version"), "proposal_version")
    roles = unique_strings(data.get("required_roles"), "required_roles")
    humans = unique_strings(data.get("required_reviewers"), "required_reviewers")
    if len(humans) != 6:
        raise ContractError("exactly six configured human reviewers required")
    rounds = integer(data.get("round", 0), "round")
    maximum = integer(data.get("max_rounds", 2), "max_rounds", 1)
    issues, pending, conditions = [], [], []
    seen = set()
    for item in sequence(data.get("stances", []), "stances"):
        obj(item, "stance")
        role = text(item.get("role_id"), "role_id")
        if role not in roles or role in seen:
            raise ContractError("unexpected or duplicate role stance")
        seen.add(role)
        stance = enum(item.get("stance"), "stance", {"Agree", "Conditional", "Disagree", "Pending"})
        if item.get("proposal_version") != version:
            pending.append(f"stale stance: {role}"); continue
        if stance == "Disagree":
            issues.append(f"opposition: {role}")
        elif stance == "Pending":
            pending.append(f"pending role: {role}")
        elif stance == "Conditional":
            condition = text(item.get("condition"), "condition")
            conditions.append(f"{role}: {condition}")
    pending.extend(f"missing role: {r}" for r in roles if r not in seen)
    approved = set(); seen_humans = set()
    for item in sequence(data.get("human_reviews", []), "human_reviews"):
        obj(item, "human review")
        human = text(item.get("reviewer_id"), "reviewer_id")
        if human not in humans or human in seen_humans:
            raise ContractError("unexpected or duplicate human review")
        seen_humans.add(human)
        state = enum(item.get("decision"), "decision", {"APPROVE", "CONDITIONAL", "REJECT", "PENDING"})
        if item.get("proposal_version") != version:
            pending.append(f"stale human review: {human}"); continue
        if state == "APPROVE":
            approved.add(human)
        elif state == "CONDITIONAL":
            conditions.append(f"{human}: {text(item.get('condition'), 'condition')}")
        elif state == "REJECT":
            issues.append(f"human rejection: {human}")
        else:
            pending.append(f"pending human: {human}")
    pending.extend(f"missing human: {h}" for h in humans if h not in seen_humans)
    unverified = []; seen_objections = set()
    for item in sequence(data.get("objections", []), "objections"):
        obj(item, "objection")
        oid = text(item.get("objection_id"), "objection_id")
        if oid in seen_objections:
            raise ContractError("duplicate objection")
        seen_objections.add(oid)
        state = enum(item.get("status"), "objection status", {"open", "responded", "resolved", "deferred"})
        severity = enum(item.get("severity"), "severity", {"blocking", "nonblocking"})
        kind = enum(item.get("kind"), "kind", {"fact", "value", "tradeoff"})
        if item.get("proposal_version") != version:
            pending.append(f"stale objection: {oid}"); continue
        if state == "resolved":
            text(item.get("resolution_ref"), "resolution_ref")
            continue
        if state == "deferred":
            text(item.get("reason"), "deferral reason")
            signoffs = unique_strings(item.get("approved_by"), "approved_by")
            if severity == "blocking" or set(signoffs) != set(humans):
                issues.append(f"unapproved or blocking deferral: {oid}")
            continue
        if severity == "blocking":
            issues.append(f"blocking objection: {oid}")
        elif kind == "fact":
            unverified.append(f"fact needs evidence: {oid}")
        else:
            pending.append(f"open objection: {oid}")
    checks = obj(data.get("verification"), "verification")
    for key in ("required_checks_passed", "evidence_current"):
        if type(checks.get(key)) is not bool:
            raise ContractError(f"verification.{key} must be boolean")
        if not checks[key]:
            unverified.append(key)
    if issues:
        status = "NO_CONSENSUS"
    elif unverified:
        status = "NEEDS_VERIFICATION"
    elif pending:
        status = "NO_CONSENSUS"
    elif conditions:
        status = "CONDITIONAL"
    else:
        status = "PASSED"
    can_publish = status == "PASSED" and set(humans) == approved
    stop = "CONSENSUS" if can_publish else ("ROUND_LIMIT" if rounds >= maximum else None)
    return {"status": status, "can_publish": can_publish, "proposal_version": version,
            "reasons": issues + unverified + pending, "conditions": conditions,
            "stop_reason": stop, "action": "PUBLISH_BASELINE" if can_publish else
            ("HUMAN_REVIEW" if stop else "REVISE_OR_VERIFY")}
