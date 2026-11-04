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
WORKFLOW_PATH = Path.home() / ".config" / "hype-checker" / "workflow.yaml"
WATCH_SCRIPT = os.environ.get("HC_WATCH_SCRIPT") or str(
    Path.home() / ".claude/plugins/cache/claude-video/watch/0.3.2/skills/watch/scripts/watch.py")


def extraction_chat(settings, claude):
    """Tool-name extraction backend: local LM Studio (Mac) or Claude (server, HC_EXTRACT=claude)."""
    if settings["extract"] != "claude":
        return lm_studio_chat
    if claude is None:
        raise SystemExit("HC_EXTRACT=claude 인데 HC_CLAUDE_MODEL·HC_PRICE_IN/OUT_PER_MTOK·ANTHROPIC_API_KEY 중 빠진 것이 있다")
    from hype_checker.llm import make_chat
    return make_chat(claude["model"], claude["api_key"])


def live_context():
    from hype_checker.v2.config import load_claude_settings, load_server_settings
    settings = load_server_settings()
    claude = load_claude_settings() if settings["extract"] == "claude" else None
    runner = Runner(watch_script=WATCH_SCRIPT)
    chat = extraction_chat(settings, claude)
    return Context(runner=runner, sources=LiveSources(runner), chat=chat,
                   out_root=PROJECT_ROOT / "out", now=time.time(), watch_script=WATCH_SCRIPT,
                   home=Path.home(), env=dict(os.environ), vision=chat if claude else None)


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
    verdict_eval = commands.add_parser("eval-verdict", help="판정 정답셋 평가(Claude 크레딧 사용)")
    verdict_eval.add_argument("--model", default="", help="필수. 기본 모델 없음")
    verdict_eval.add_argument("--split", default="dev", choices=["dev", "holdout"])
    serve = commands.add_parser("serve", help="v2 웹훅 수신(127.0.0.1에만 바인딩)")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--host", default="127.0.0.1", help="컨테이너 안에서는 0.0.0.0 (호스트 포트는 열지 않는다)")
    work = commands.add_parser("work", help="v2 큐를 v1 묶음으로 처리")
    work.add_argument("--send", action="store_true", help="DM 답장을 실제로 보낸다(기본: 초안만)")
    work.add_argument("--loop", action="store_true", help="Ctrl-C까지 30초마다 큐를 처리한다(기본: 1회)")
    commands.add_parser("snapshot", help="설치 목록 중 workflow.yaml에 없는 이름만 제안(파일은 안 바꾼다)")
    return parser


V2_INBOX = PROJECT_ROOT / "out" / "v2"
V2_RETENTION_DAYS = 7


def require_secret_path_for_public(host, path):
    """A non-loopback bind (container behind the tunnel) refuses to start on the guessable default path."""
    if host not in ("127.0.0.1", "localhost") and path == "/webhook":
        raise SystemExit("--host 가 외부 바인딩이면 HC_WEBHOOK_PATH(/webhook/<무작위>)가 필요하다")


def _serve(port, host):
    from hype_checker.v2.config import load_config, load_server_settings
    from hype_checker.v2.inbox import Inbox
    from hype_checker.v2.server import BoundedServer, make_handler
    config, _ = load_config()
    path = load_server_settings()["webhook_path"]
    require_secret_path_for_public(host, path)
    shown = path if path == "/webhook" else "/webhook/…(HC_WEBHOOK_PATH)"
    print(f"listening on http://{host}:{port}{shown}")
    BoundedServer((host, port), make_handler(config, Inbox(V2_INBOX), path=path)).serve_forever()


def _verdict_chat(config):
    """Runtime fields for HC_VERDICT=on, or None → measurement-only replies.

    Fails closed on cost: model, both HC_PRICE_*_PER_MTOK and the API key must all be set (v2.env; HC_* env overrides)."""
    from hype_checker.llm import make_chat
    from hype_checker.v2.config import load_claude_settings
    if config.verdict != "on":
        return None
    settings = load_claude_settings()
    if settings is None:
        print("HC_VERDICT=on 이지만 HC_CLAUDE_MODEL·HC_PRICE_IN/OUT_PER_MTOK·ANTHROPIC_API_KEY 중 빠진 것이 있다"
              " — 측정만 보낸다", file=sys.stderr)
        return None
    return {"chat": make_chat(settings["model"], settings["api_key"]), "model": settings["model"],
            "prices": settings["prices"]}


def _runtime_fields(config):
    """Claude fields for the worker; owner_lines_for only when an owner sender is configured (worker gates the rest)."""
    fields = dict(_verdict_chat(config) or {})
    if fields and config.owner_sender:
        from hype_checker.owner import make_owner_lines_for
        fields["owner_lines_for"] = make_owner_lines_for(fields["chat"], workflow_path=WORKFLOW_PATH,
                                                         model=fields["model"])
    return fields


def _snapshot():
    """Names only: skill folders, ops/bin files and mcpServers keys of ~/.claude.json — no settings or secrets."""
    from hype_checker.owner import WorkflowError, installed_names, load_workflow, snapshot_suggestions
    try:
        workflow = load_workflow(WORKFLOW_PATH)
    except WorkflowError as error:
        raise SystemExit(f"workflow.yaml을 읽지 못했다: {error}")
    suggestions = snapshot_suggestions(workflow, installed_names(Path.home()))
    print(f"workflow.yaml에 없는 설치 이름 {len(suggestions)}개 (자동 반영 안 함):")
    for name in suggestions:
        print(f"- {name}")


def _print_record(record):
    from hype_checker.v2.sending import messages_of
    messages = [{k: m.get(k) for k in ("sent", "error")} for m in messages_of(record)]
    fields = {k: record.get(k) for k in ("case_dir", "mode", "error", "dispute", "limited")}
    print(json.dumps({**fields, "messages": messages}, ensure_ascii=False))


def _open_store():
    """The result cache is optional: if it cannot open, every share is measured fresh."""
    from hype_checker.v2.store import Store
    try:
        return Store(V2_INBOX / "hc.db")
    except Exception as error:
        print(f"result cache off: {type(error).__name__}", file=sys.stderr)
        return None


def _work(send, loop=False):
    from hype_checker.v2.config import load_config
    from hype_checker.v2.inbox import Inbox
    from hype_checker.v2.limits import Limits
    from hype_checker.v2 import token as ig_token
    from hype_checker.v2.reply import send_reply
    from hype_checker.v2.worker import Runtime, process_pending, run_loop
    config, seed = load_config()
    if send and not seed:
        raise SystemExit("IG_ACCESS_TOKEN 없음 — 답장을 보낼 수 없다")
    inbox = Inbox(V2_INBOX)
    sender = (lambda recipient, text: send_reply(recipient, text, ig_token.current(inbox.root, seed))) if send else None
    store = _open_store()

    def upkeep():
        """Each chore on its own: a cache problem must never stop the token refresh."""
        for chore in ((lambda: store.prune(time.time())) if store else None,
                      (lambda: ig_token.refresh_if_due(inbox.root, seed, time.time())) if send else None):
            try:
                chore and chore()
            except Exception as error:  # logged by type only; the next pass tries again
                print(f"upkeep skipped: {type(error).__name__}", file=sys.stderr)
    runtime = Runtime(limits=Limits(V2_INBOX / "limits"), acknowledge=bool(send), before_pass=upkeep, store=store,
                      **_runtime_fields(config))
    if loop:
        print("work --loop: Ctrl-C로 멈춘다")
        try:
            run_loop(inbox, live_context(), config, sender, runtime=runtime, retention_days=V2_RETENTION_DAYS)
        except KeyboardInterrupt:
            print("멈춤")
        return
    inbox.prune(days=V2_RETENTION_DAYS)
    for record in process_pending(inbox, live_context(), config, send=sender, runtime=runtime):
        _print_record(record)


def _eval_verdict(model, split):
    from hype_checker.llm import ClaudeUnavailable, make_chat
    from hype_checker.v2.config import load_anthropic_key
    from hype_checker.verdictset import load_verdictset
    from hype_checker.verdicteval import passes, run_verdict_eval
    if not model:
        raise SystemExit("--model 이 필요하다(기본 모델 없음)")
    key = load_anthropic_key()
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY 없음 — 판정 평가를 시작할 수 없다")
    goldset = PROJECT_ROOT / "eval" / "verdict-goldset.yaml"
    if not any(case["split"] == split for case in load_verdictset(goldset)):
        raise SystemExit(f"{split} split에 해당하는 사례가 없다")
    try:
        results, summary = run_verdict_eval(goldset, live_context(), make_chat(model, key), split, model)
    except ClaudeUnavailable as error:
        raise SystemExit(f"Claude를 쓸 수 없다(장애 또는 크레딧 문제): {error}")
    ran_at = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = PROJECT_ROOT / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"verdict-{ran_at}.json"
    report = {"ran_at": ran_at, "model": model, "split": split, "passes": passes(summary),
              "summary": summary, "results": results}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}, ensure_ascii=False, indent=1))
    print(f"passes: {report['passes']} · {path}")


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
    elif args.command == "eval-verdict":
        _eval_verdict(args.model, args.split)
    elif args.command == "serve":
        _serve(args.port, args.host)
    elif args.command == "work":
        _work(args.send, args.loop)
    elif args.command == "snapshot":
        _snapshot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
