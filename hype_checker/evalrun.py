"""hype-check eval: run the goldset, score, write eval/results/<time>.json (design-v1 §6)."""

import json
import time
from pathlib import Path

from hype_checker.evalset import load_goldset
from hype_checker.ingest import classify
from hype_checker.pipeline import check
from hype_checker.runner import ForbiddenCommand, check_argv
from hype_checker.scoring import score_case, summarize


def _input_of(case, project_root):
    spec = case["input"]
    return str(project_root / spec["path"]) if spec["type"] == "mp4" else spec.get("url") or spec.get("text")


def _run_case(case, ctx, project_root, allow_download):
    raw = _input_of(case, project_root)
    if classify(raw).needs_download and not allow_download:
        return {"id": case["id"], "group": case["group"], "status": "skipped", "reason": "download not allowed"}
    try:
        case_dir = check(raw, ctx)
    except Exception as error:  # one failing case must not hide the others
        return {"id": case["id"], "group": case["group"], "status": "failed", "reason": repr(error)[:300]}
    bundle = json.loads((case_dir / "bundle.json").read_text())
    return {**_scored(case, bundle, case_dir), "entities_at_run": bundle["entities"]}


def _degraded(bundle):
    evidence, extraction = bundle["evidence"]["status"], bundle["extraction"]["status"]
    return evidence not in ("ok", "not_run") or extraction not in ("ok", "not_run")


def _scored(case, bundle, case_dir):
    return {"id": case["id"], "group": case["group"], "status": "scored", "bundle": str(case_dir),
            "score": score_case(case, bundle), "gh_calls": bundle["budget"]["gh_calls"],
            "evidence": bundle["evidence"]["status"], "extraction": bundle["extraction"]["status"],
            "degraded": _degraded(bundle), "interventions": bundle.get("interventions", []),
            "versions": bundle.get("versions", {})}


def _invariant_violations(runner):
    violations = []
    for argv in runner.log:
        try:
            check_argv(argv, runner.watch_script)
        except ForbiddenCommand:
            violations.append(argv)
    return violations


def run_eval(goldset_path, ctx, project_root, groups=None, allow_download=False):
    cases = [c for c in load_goldset(goldset_path) if not groups or c["group"] in groups]
    results = [_run_case(case, ctx, project_root, allow_download) for case in cases]
    report = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "groups": groups or "all", "allow_download": allow_download,
        "summary": summarize(results),
        "invariant_violations": _invariant_violations(ctx.runner),
        "process_calls": len(ctx.runner.log),
        "review_queue": [q | {"case": r["id"]} for r in results if r["status"] == "scored"
                         for q in r["score"].get("review_queue", [])],
        "results": results,
    }
    out = project_root / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{report['ran_at'].replace(':', '')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return path, report


RESCORE_SCOPE = ("invariant_violations covers the original code run only; commands run by the judging "
                 "session (resume/drop) are listed per case in 'interventions', not re-checked here")


def rescore(results_path, goldset_path, label):
    """Re-scores the same bundles after a judging session ran resume/drop on them."""
    report = json.loads(Path(results_path).read_text())
    cases = {c["id"]: c for c in load_goldset(goldset_path)}
    report["results"] = [
        {**_scored(cases[r["id"]], json.loads((Path(r["bundle"]) / "bundle.json").read_text()), r["bundle"]),
         "score_at_run": r.get("score"), "entities_at_run": r.get("entities_at_run")}
        if r["status"] == "scored" else r for r in report["results"]]
    report.update(stage=label, rescored_from=str(results_path),
                  rescored_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  summary=summarize(report["results"]), scope_note=RESCORE_SCOPE,
                  interventions=sum(len(r.get("interventions", [])) for r in report["results"]),
                  review_queue=[q | {"case": r["id"]} for r in report["results"] if r["status"] == "scored"
                                for q in r["score"].get("review_queue", [])])
    path = Path(results_path).with_name(Path(results_path).stem + f"-{label}.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return path, report
