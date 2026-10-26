"""Scores one bundle against one goldset case, three tasks kept apart (design-v1 §6).

Matching is one-to-one (a prediction can satisfy one gold entity at most) and
dropped predictions never count. Identification uses a fixed denominator: every
annotated gold primary, whether or not extraction found it.
"""

from collections import defaultdict

from hype_checker.resolve import core_name, normalize

MIN_CONTAINED = 4
URL_GROUP = "B-github"
EXACT, CONTAINED = 2, 1


def _names(*values):
    names = set()
    for value in values:
        if value:
            names |= {normalize(value), normalize(core_name(value))}
    return {name for name in names if name}


def match_quality(gold, pred):
    best = 0
    for g in _names(gold["name"], *gold.get("aliases", [])):
        for p in _names(pred.get("name"), pred.get("name_latin")):
            short, long_ = sorted((g, p), key=len)
            if g == p:
                return EXACT
            if len(short) >= MIN_CONTAINED and short in long_:
                best = CONTAINED
    return best


def names_match(gold, pred):
    return match_quality(gold, pred) > 0


def _live(predictions):
    return [p for p in predictions if p.get("role") != "dropped"]


def assign(gold_entities, predictions):
    """Greedy one-to-one assignment, exact matches first. Returns {gold index: prediction}."""
    pairs = sorted(((match_quality(g, p), gi, pi) for gi, g in enumerate(gold_entities)
                    for pi, p in enumerate(predictions)), reverse=True)
    used_gold, used_pred, assigned = set(), set(), {}
    for quality, gi, pi in pairs:
        if quality and gi not in used_gold and pi not in used_pred:
            assigned[gi] = predictions[pi]
            used_gold.add(gi)
            used_pred.add(pi)
    return assigned


def _identifier(text):
    return (text or "").strip().rstrip("/").lower() or None


def _top(pred):
    candidates = (pred.get("resolution") or {}).get("candidates") or []
    return pred.get("identifier") or (candidates[0]["identifier"] if candidates else None)


def _url_score(gold_entities, predictions):
    expected = next((g for g in gold_entities if g["role"] == "primary"), None)
    got = next((p for p in predictions if p.get("origin") == "url"), None)
    correct = bool(expected and got and _identifier(got.get("identifier")) == _identifier(expected["identifier"]))
    return {"url_normalization": {"cases": 1, "correct": int(correct)}}


def _extraction(gold_entities, predictions, assigned):
    primary = [i for i, g in enumerate(gold_entities) if g["role"] == "primary"]
    predicted_primary = [p for p in predictions if p.get("role") == "primary"]
    matched_preds = {id(p) for p in assigned.values()}
    strict = [i for i in primary if i in assigned
              and assigned[i].get("role") == "primary" and assigned[i].get("kind") == gold_entities[i].get("kind")]
    return {
        "gold_primary": len(primary),
        "found": sum(1 for i in primary if i in assigned),
        "found_strict": len(strict),
        "predicted_primary": len(predicted_primary),
        "predicted_primary_matched": sum(1 for p in predicted_primary if id(p) in matched_preds),
        "role_correct": sum(1 for i, p in assigned.items() if p.get("role") == gold_entities[i]["role"]),
    }


def _identification(gold_entities, assigned):
    annotated = [i for i, g in enumerate(gold_entities)
                 if g["role"] == "primary" and g["identifier_status"] == "annotated"]
    correct = sum(1 for i in annotated if i in assigned
                  and _identifier(assigned[i].get("identifier")) == _identifier(gold_entities[i]["identifier"]))
    return {"annotated": len(annotated), "correct": correct}


def _resolution(gold_entities, predictions, assigned):
    counts = {"annotated": 0, "top1_correct": 0, "resolved": 0, "resolved_correct": 0,
              "resolved_unmatched": 0}
    matched_preds = {id(p) for p in assigned.values()}
    counts["resolved_unmatched"] = sum(
        1 for p in predictions if p.get("role") == "primary" and id(p) not in matched_preds
        and (p.get("resolution") or {}).get("status") == "resolved")
    for i, pred in assigned.items():
        gold = gold_entities[i]
        if gold["role"] != "primary" or gold["identifier_status"] != "annotated":
            continue
        correct = _identifier(_top(pred)) == _identifier(gold["identifier"])
        counts["annotated"] += 1
        counts["top1_correct"] += int(correct)
        if (pred.get("resolution") or {}).get("status") == "resolved":
            counts["resolved"] += 1
            counts["resolved_correct"] += int(correct)
    return counts


NO_IDENTIFIER_EXPECTED = {"none_exists", "unverifiable"}


def _none_exists(gold_entities, assigned):
    """Gold says no identifier can be confirmed: any confirmed identifier is wrong."""
    matched = [p for i, p in assigned.items() if gold_entities[i]["identifier_status"] in NO_IDENTIFIER_EXPECTED]
    return {"matched": len(matched), "wrongly_resolved": sum(1 for p in matched if p.get("identifier"))}


def _review_queue(gold_entities, assigned):
    return [{"gold": gold_entities[i]["name"], "top": _top(p), "status": (p.get("resolution") or {}).get("status")}
            for i, p in assigned.items()
            if gold_entities[i]["role"] == "primary" and gold_entities[i]["identifier_status"] == "unannotated"]


def score_case(case, bundle):
    gold_entities = case["expected"]["entities"]
    predictions = _live(bundle.get("entities", []))
    if case["group"] == URL_GROUP:
        return _url_score(gold_entities, predictions)
    assigned = assign(gold_entities, predictions)
    return {
        "extraction": _extraction(gold_entities, predictions, assigned),
        "identification": _identification(gold_entities, assigned),
        "resolution": _resolution(gold_entities, predictions, assigned),
        "none_exists": _none_exists(gold_entities, assigned),
        "review_queue": _review_queue(gold_entities, assigned),
    }


def _rate(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None


def _add(totals, section, values):
    for key, value in values.items():
        totals[f"{section}.{key}"] += value


def _group_rates(totals):
    t = lambda key: totals.get(key, 0)
    return {
        "cases": t("cases"), "skipped": t("skipped"), "failed": t("failed"), "degraded": t("degraded"),
        "url_normalization": _rate(t("url_normalization.correct"), t("url_normalization.cases")),
        "extraction_recall": _rate(t("extraction.found"), t("extraction.gold_primary")),
        "extraction_recall_strict": _rate(t("extraction.found_strict"), t("extraction.gold_primary")),
        "extraction_precision": _rate(t("extraction.predicted_primary_matched"), t("extraction.predicted_primary")),
        "identification": _rate(t("identification.correct"), t("identification.annotated")),
        "resolution_top1_given_found": _rate(t("resolution.top1_correct"), t("resolution.annotated")),
        "resolved_precision": _rate(t("resolution.resolved_correct"), t("resolution.resolved")),
        "resolved_unmatched": t("resolution.resolved_unmatched"),
        "none_exists_wrongly_resolved": t("none_exists.wrongly_resolved"),
        "counts": dict(totals),
    }


SECTIONS = ("url_normalization", "extraction", "identification", "resolution", "none_exists")


def summarize(results):
    groups = defaultdict(lambda: defaultdict(int))
    for result in results:
        totals = groups[result["group"]]
        totals["cases"] += 1
        if result["status"] != "scored":
            totals[result["status"]] += 1
            continue
        totals["degraded"] += int(bool(result.get("degraded")))
        for section in SECTIONS:
            if section in result["score"]:
                _add(totals, section, result["score"][section])
    return {group: _group_rates(totals) for group, totals in sorted(groups.items())}
