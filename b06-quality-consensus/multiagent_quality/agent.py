"""Integration facade. B02 supplies context; B03 supplies proposal state."""
from copy import deepcopy
from .consensus import evaluate_consensus
from .evidence import check_evidence
from .validation import unique_strings, obj, ContractError

class QualityConsensusAgent:
    role_id = "QualityConsensusAgent"
    def __init__(self, *, required_roles, required_reviewers):
        self.roles = unique_strings(required_roles, "required_roles")
        self.reviewers = unique_strings(required_reviewers, "required_reviewers")
        if len(self.reviewers) != 6: raise ContractError("six human reviewers required")

    def review(self, proposal, envelope, *, evidence_policy):
        obj(proposal, "proposal")
        checked = check_evidence(envelope, **evidence_policy)
        data = deepcopy(proposal)
        # Ignore model-supplied voter rosters and verification flags.
        data["required_roles"] = self.roles; data["required_reviewers"] = self.reviewers
        data["verification"] = {"required_checks_passed": checked["accepted"],
                                "evidence_current": checked["accepted"]}
        result = evaluate_consensus(data)
        return {"role_id": self.role_id, "decision": result, "evidence": checked}
