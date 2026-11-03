"""Verdict evaluation (design-v3 §7): score judged bundles against eval/verdict-goldset.yaml."""

import json
from pathlib import Path

from hype_checker.llm import ClaudeUnavailable
from hype_checker.pipeline import check
from hype_checker.resolve import normalize
from hype_checker.verdict import Budget, judge_bundle
from hype_checker.verdictset import load_verdictset

PASS = {"confirmed_precision": 0.90, "fatal_errors": 0, "against_false_positive": 1, "hold_rate": 0.40}
CONFIRMED, CAUTION, AGAINST, HELD = "👍", "🤔", "👎", "❔"


def _match(gold_tools, predicted_tools):
    """One-to-one gold index -> prediction: normalized identifier first, then normalized name."""
    assigned, used = {}, set()
    for key in (lambda t: normalize(t.get("identifier") or ""), lambda t: normalize(t.get("name") or "")):
        for gi, gold in enumerate(gold_tools):
            wanted = key(gold)
            if gi in assigned or not wanted:
                continue
            for pi, pred in enumerate(predicted_tools):
                if pi not in used and key(pred) == wanted:
                    assigned[gi] = pi
                    used.add(pi)
                    break
    return {gi: predicted_tools[pi] for gi, pi in assigned.items()}


def _case_pairs(case, result):
    """(gold grade, predicted grade) per gold tool; an unmatched gold tool counts as predicted held."""
    predicted = ((result or {}).get("verdict") or {}).get("tools") or []
    matched = _match(case["tools"], predicted)
    return [(gold["grade"], matched[gi]["grade"] if gi in matched else HELD)
            for gi, gold in enumerate(case["tools"])]


def _within_cap(pairs):
    """Not 👍 when gold is not 👍, and not 👎 when gold is 👍."""
    return all(not (pred == CONFIRMED and gold != CONFIRMED) and not (pred == AGAINST and gold == CONFIRMED)
               for gold, pred in pairs)


def _cost(results):
    costs = [((r.get("verdict") or {}).get("repro") or {}).get("cost_usd") for r in results]
    return None if any(c is None for c in costs) else sum(costs)


def _hold_rate(pairs):
    """Unwarranted holds: predicted ❔ among tools whose gold is not ❔ (a correct ❔ is not a hold)."""
    judged = [p for g, p in pairs if g != HELD]
    return sum(p == HELD for p in judged) / len(judged) if judged else 0.0


def score_verdicts(cases, results):
    by_id = {r["id"]: r for r in results}
    per_case = [{"id": c["id"], "kind": c["kind"], "pairs": _case_pairs(c, by_id.get(c["id"]))} for c in cases]
    pairs = [p for entry in per_case for p in entry["pairs"]]
    confirmed = [gold for gold, pred in pairs if pred == CONFIRMED]
    adversarial = [entry for entry in per_case if entry["kind"] == "adversarial"]
    return {
        "scored_tools": len(pairs),
        "gold_confirmed": sum(g == CONFIRMED for g, _ in pairs),
        "predicted_confirmed": len(confirmed),
        "confirmed_precision": (sum(g in (CONFIRMED, CAUTION) for g in confirmed) / len(confirmed)) if confirmed else None,
        "fatal_errors": sum(g == AGAINST and p == CONFIRMED for g, p in pairs),
        "against_false_positive": sum(g in (CONFIRMED, CAUTION) and p == AGAINST for g, p in pairs),
        "hold_rate": _hold_rate(pairs),
        "adversarial_within_cap": (sum(_within_cap(e["pairs"]) for e in adversarial) / len(adversarial)
                                   if adversarial else 1.0),
        "cost_usd": _cost([by_id[c["id"]] for c in cases if c["id"] in by_id]),
        "per_case": per_case,
    }


def passes(summary):
    precision = summary["confirmed_precision"]
    precision_ok = precision >= PASS["confirmed_precision"] if precision is not None else summary["fatal_errors"] == 0
    if summary["scored_tools"] == 0:
        return False
    if summary["gold_confirmed"] > 0 and summary["predicted_confirmed"] == 0:
        return False
    return (precision_ok and summary["fatal_errors"] <= PASS["fatal_errors"]
            and summary["against_false_positive"] <= PASS["against_false_positive"]
            and summary["hold_rate"] <= PASS["hold_rate"]
            and summary["adversarial_within_cap"] == 1.0)


def _load_bundle(case_dir):
    return json.loads((Path(case_dir) / "bundle.json").read_text())


def _run_case(case, ctx, chat, model, budget_factory, now, check_fn, judge_fn):
    try:
        bundle = _load_bundle(check_fn(case["input"]["value"], ctx))
        verdict = judge_fn(bundle, ctx.sources, chat, budget_factory(), now, model=model)
    except ClaudeUnavailable:
        raise
    except Exception as error:  # one failing case must not hide the others
        return {"id": case["id"], "verdict": None, "error": repr(error)[:300]}
    return {"id": case["id"], "verdict": verdict}


def run_verdict_eval(path, ctx, chat, split, model, budget_factory=Budget, now=None,
                     check_fn=check, judge_fn=judge_bundle):
    cases = [c for c in load_verdictset(path) if c["split"] == split and c.get("live", True)]
    now = ctx.now if now is None else now
    results = [_run_case(c, ctx, chat, model, budget_factory, now, check_fn, judge_fn) for c in cases]
    return results, score_verdicts(cases, results)
