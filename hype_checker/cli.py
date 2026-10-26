"""hype-check check | resume | clean (design-v1 §2)."""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from hype_checker.bundle import write_bundle
from hype_checker.extract import lm_studio_chat
from hype_checker.pipeline import Context, _identify, check
from hype_checker.resolve import LiveSources, normalize
from hype_checker.runner import Runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCH_SCRIPT = str(Path.home() / ".claude/plugins/cache/claude-video/watch/0.3.2/skills/watch/scripts/watch.py")


def live_context():
    runner = Runner(watch_script=WATCH_SCRIPT)
    return Context(runner=runner, sources=LiveSources(runner), chat=lm_studio_chat,
                   out_root=PROJECT_ROOT / "out", now=time.time(), watch_script=WATCH_SCRIPT,
                   home=Path.home(), env=dict(os.environ))


def _log_intervention(bundle, action, **details):
    bundle.setdefault("interventions", []).append(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **details})


def _supersede(entities, name):
    """A judged entity replaces live candidates of the same name instead of sitting next to them."""
    key = normalize(name)
    replaced = [e for e in entities if e.get("role") != "dropped"
                and key in {normalize(e.get("name") or ""), normalize(e.get("name_latin") or "")}]
    for entity in replaced:
        entity.update(role="dropped", dropped_reason=f"superseded by resume: {name}")
    return len(replaced)


def resume(case_dir, ctx, name, kind, identifier=None, evidence=""):
    if not evidence.strip():
        raise ValueError("resume needs --evidence: which frame/line showed this name")
    case_dir = Path(case_dir)
    bundle = json.loads((case_dir / "bundle.json").read_text())
    log_start = len(ctx.runner.log)
    entity = {"name": name, "name_latin": name, "kind": kind, "role": "primary",
              "evidence_quote": evidence, "origin": "human"}
    if identifier:
        entity.update(identifier=identifier, origin="url", resolution={"status": "given", "candidates": []})
    entity = _identify(entity, ctx)
    entity["origin"] = "human"
    superseded = _supersede(bundle["entities"], name)
    bundle["entities"].append(entity)
    calls = [argv for argv in ctx.runner.log[log_start:] if argv[:2] == ["gh", "api"]]
    bundle["budget"] = {"gh_calls": bundle.get("budget", {}).get("gh_calls", 0) + len(calls)}
    _log_intervention(bundle, "resume", name=name, kind=kind, identifier=identifier, evidence=evidence,
                      gh_calls=len(calls), superseded=superseded)
    write_bundle(case_dir, bundle)
    return entity


def drop(case_dir, name, reason):
    """Judging session rejects a candidate; the record stays for audit."""
    case_dir = Path(case_dir)
    bundle = json.loads((case_dir / "bundle.json").read_text())
    matches = [e for e in bundle["entities"] if name in (e["name"], e.get("name_latin"))]
    if not matches:
        raise KeyError(name)
    for entity in matches:
        entity.update(role="dropped", dropped_reason=reason)
    _log_intervention(bundle, "drop", name=name, reason=reason)
    write_bundle(case_dir, bundle)


def clean(case_dir, out_root=None):
    from hype_checker.evidence import guard_case_dir
    case_dir = Path(case_dir)
    guard_case_dir(case_dir, out_root or PROJECT_ROOT / "out")
    frames = case_dir / "frames"
    if frames.is_dir():
        shutil.rmtree(frames)


def _parser():
    parser = argparse.ArgumentParser(prog="hype-check")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("input")
    add = commands.add_parser("resume")
    add.add_argument("case_dir")
    add.add_argument("--entity", required=True)
    add.add_argument("--kind", default="repo", choices=["repo", "npm", "pypi", "mcp", "skill", "plugin", "product", "feature"])
    add.add_argument("--identifier")
    add.add_argument("--evidence", required=True, help="근거: 어느 프레임·시각·전사 줄에서 무엇을 읽었는지")
    remove = commands.add_parser("drop")
    remove.add_argument("case_dir")
    remove.add_argument("--entity", required=True)
    remove.add_argument("--reason", required=True)
    commands.add_parser("clean").add_argument("case_dir")
    evaluate = commands.add_parser("eval")
    evaluate.add_argument("--set", dest="evalset", default="goldset", choices=["goldset", "validation"])
    evaluate.add_argument("--group", action="append",
                          choices=["A-reel", "B-github", "C-youtube", "D-control", "V-text", "V-control"])
    evaluate.add_argument("--allow-download", action="store_true", help="유튜브 케이스를 익명으로 1회 내려받는다")
    evaluate.add_argument("--rescore", help="resume 뒤 같은 묶음을 다시 채점할 결과 파일")
    evaluate.add_argument("--label", default="with-claude")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "check":
        print(check(args.input, live_context()) / "bundle.md")
    elif args.command == "resume":
        entity = resume(args.case_dir, live_context(), args.entity, args.kind, args.identifier, args.evidence)
        print(f"{entity['name']}: {entity.get('identifier') or '미확정'}")
    elif args.command == "drop":
        drop(args.case_dir, args.entity, args.reason)
    elif args.command == "clean":
        clean(args.case_dir)
    elif args.command == "eval":
        from hype_checker.evalrun import rescore, run_eval
        if args.rescore:
            path, report = rescore(args.rescore, PROJECT_ROOT / "eval" / f"{args.evalset}.yaml", args.label)
            print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
            print(f"interventions: {report['interventions']} · {path}")
            return 0
        path, report = run_eval(PROJECT_ROOT / "eval" / f"{args.evalset}.yaml", live_context(), PROJECT_ROOT,
                                groups=args.group, allow_download=args.allow_download)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
        print(f"invariant violations: {len(report['invariant_violations'])} · {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
