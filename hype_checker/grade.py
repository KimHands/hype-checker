"""Code-only grade rules (spec §1): facts + claim results -> 👍/🤔/👎/❔. No LLM."""

RULES_VERSION = "4"
MAX_REASONS = 3
CONFIRM_CHECKS = ("located", "measured", "active_1y", "no_warning")
CONFIRMED_KINDS = ("identity", "activity", "trust")
CAUTION_KINDS = ("warning", "missing", "notice")


def _ids(facts, kinds):
    return [f["id"] for f in facts if f["kind"] in kinds]


def _is_impersonation(entity):
    mine = (entity.get("identifier") or "").lower()
    if not mine:
        return False
    candidates = (entity.get("resolution") or {}).get("candidates") or []
    return any(c.get("identifier", "").lower() != mine
               and any(str(e).startswith("원본 추정") for e in c.get("evidence") or [])
               for c in candidates)


def _contradicted_ids(claim_results):
    return [f"claim.{c['claim_id']}" for c in claim_results if c["result"] == "불일치"]


def _claims_hold(entity, claim_results):
    """A bare shared link has no caption to check. Any caption needs at least one 일치 and no 불일치 —
    extraction can miss claims a caption makes, so 'no claims found' is not 'nothing claimed'."""
    if entity.get("origin") == "url":
        return not any(c["result"] == "불일치" for c in claim_results)
    results = [c["result"] for c in claim_results]
    return "일치" in results and "불일치" not in results


def _blocked_by(entity, checks, claim_results):
    blocked = [name for name in CONFIRM_CHECKS if not checks.get(name)]
    return blocked + ([] if _claims_hold(entity, claim_results) else ["claim_match"])


def _against_reasons(entity, facts, claim_results):
    holder = [f["id"] for f in facts if f["id"] == "warning.security_holder"]
    reasons = holder + _contradicted_ids(claim_results)
    if _is_impersonation(entity):
        reasons.append("identity.impersonation")
    return reasons


def _pick(preferred, facts):
    chosen = list(dict.fromkeys(preferred))
    padding = [i for i in _ids(facts, CONFIRMED_KINDS) if i not in chosen]
    chosen += padding[:max(0, 2 - len(chosen))]
    return chosen[:MAX_REASONS]


def grade_tool(entity, facts, claim_results):
    fact_list, checks = facts["facts"], facts["checks"]
    blocked = _blocked_by(entity, checks, claim_results)
    if not checks.get("located"):
        return {"grade": "❔", "reason_ids": _pick(_ids(fact_list, CAUTION_KINDS), fact_list), "blocked_by": blocked}
    against = _against_reasons(entity, fact_list, claim_results)
    if against:
        return {"grade": "👎", "reason_ids": _pick(against, fact_list), "blocked_by": blocked}
    if not blocked:
        return {"grade": "👍", "reason_ids": _pick(_ids(fact_list, CONFIRMED_KINDS), fact_list), "blocked_by": []}
    return {"grade": "🤔", "reason_ids": _pick(_ids(fact_list, CAUTION_KINDS), fact_list), "blocked_by": blocked}
