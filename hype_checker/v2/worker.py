"""Turns queued shares into bundles and DM replies (design-v2 §4·§5, ops-spec §4).

One item at a time: measure, reply, send, then the next item, so a backlog never
delays an earlier reply. Daily limits are checked before measuring; the over-limit
notice goes to each sender once a day, later shares are finished silently. "정정" is
stored as a dispute and gets no reply. HC_VERDICT=on adds Claude's verdict; when
Claude is missing or down the reply falls back to the measurement-only message.
Send state lives in sending.py.
"""

import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from hype_checker.bundle import write_bundle
from hype_checker.ingest import classify_remote
from hype_checker.llm import ClaudeUnavailable
from hype_checker import claims, grade, score
from hype_checker.pipeline import check, sweep_video_leftovers
from hype_checker.v2.inbox import _write_private
from hype_checker.v2.limits import Limits
from hype_checker.v2.media import fetch_reel, is_reel_page
from hype_checker.v2.reply import EXTRACTION_FAILED, format_fallback_reply, format_reply, format_verdict_replies, video_problem
from hype_checker.v2.sending import has_unsent, new_messages, send_messages
from hype_checker.v2.webhook import UNSUPPORTED, is_allowed, is_public, within_reply_window
from hype_checker.verdict import Budget, estimate_cost, judge_bundle

NO_CAPTION = "(캡션 없음)"
LIMIT_REACHED = "오늘 확인 한도에 도달했어요. 한국 시간 오전 9시 이후에 다시 보내 주세요."
SHARE_GUIDE = ("확인할 릴스나 게시물을 이 DM으로 공유해 주세요. GitHub·npm·PyPI 링크도 괜찮아요. "
               "계정당 하루 2개까지 확인해 드려요.")
UNSUPPORTED_GUIDE = "이 형식은 확인하지 못해요. 릴스나 게시물을 공유해 주세요. GitHub·npm·PyPI 링크도 괜찮아요."
ACK = {"ig_reel": "릴스 잘 받았어요! 🔎 영상 속 도구를 찾아 확인하는 중이에요. 1~2분 안에 결과를 보내 드릴게요.",
       "ig_post": "게시물 잘 받았어요! 🔎 소개된 도구를 찾아 확인하는 중이에요. 1~2분 안에 결과를 보내 드릴게요."}
ITEM_DEADLINE_S = 180
INTERVAL_S = 30   # upkeep and unsent retries
POLL_S = 1        # a new share starts (and is acknowledged) this soon after it arrives
SAFE_MID = re.compile(r"[A-Za-z0-9_=-]{1,200}")


@dataclass(frozen=True)
class Runtime:
    limits: object
    chat: object = None              # Claude chat for HC_VERDICT=on; chat, model or prices missing → measurement only
    model: str | None = None
    prices: tuple | None = None      # (in, out) USD per million tokens, for the daily $ cap
    owner_lines_for: object = None   # (case_dir, item budget) -> list[str] | None, owner mode only
    sleep: object = time.sleep
    fetch: object = fetch_reel      # (url, dest_dir) -> local mp4 path; HC_VIDEO=on only
    acknowledge: bool = False       # send "받았어요, 확인 중" before measuring (work --send)
    before_pass: object = None      # () -> None, e.g. token refresh; failures are logged, never fatal
    store: object = None            # v2 Store: same reel → saved reply (7 d), same tool → saved research (3 d)


def _runtime(inbox, runtime):
    return runtime or Runtime(limits=Limits(inbox.root / "limits"))


def _mode(share, config):
    owner = config.owner_sender
    return "owner" if owner and share.get("sender") == owner else "guest"


def _today(now_s):
    return time.strftime("%Y-%m-%d", time.gmtime(now_s))


def _video_note(bundle, fetch_error):
    if fetch_error:
        return f"not fetched ({fetch_error})"
    status = bundle["evidence"].get("status")
    if video_problem(bundle):
        return f"not read ({video_problem(bundle)})"
    return "caption only" if status == "not_run" else f"read, then deleted (watch {status})"


def _attach_dm(case_dir, share, fetch_error=None):
    bundle = json.loads((case_dir / "bundle.json").read_text())
    bundle["dm"] = {"kind": share["kind"], "media_id": share.get("media_id"),
                    "has_media_url": share.get("has_media_url", False), "shared_at_ms": share.get("timestamp_ms"),
                    "video": _video_note(bundle, fetch_error)}
    write_bundle(case_dir, bundle)
    return bundle


def _owner_lines(case_dir, mode, runtime, budget):
    """Owner extras share the item's budget; any failure drops them (type name only on stderr) but keeps the reply."""
    if mode != "owner" or runtime.owner_lines_for is None:
        return None
    try:
        return runtime.owner_lines_for(case_dir, budget)
    except Exception as error:  # owner extras must never block the verdict reply
        print(f"owner mode skipped: {type(error).__name__}", file=sys.stderr)
        return None


def _claude_ready(runtime):
    return runtime.chat is not None and bool(runtime.model) and runtime.prices is not None


def _verdict_replies(bundle, case_dir, ctx, mode, runtime, budget):
    """Claude not configured (incl. prices: fail closed on cost), down, or every tool failed → one fallback message."""
    fallback = format_fallback_reply(bundle, case_dir.name)
    if not _claude_ready(runtime):
        return fallback
    try:
        verdict = judge_bundle(bundle, ctx.sources, runtime.chat, budget, ctx.now,
                               model=runtime.model, prices=runtime.prices, cache=runtime.store)
    except ClaudeUnavailable:
        return fallback
    bundle["verdict"] = verdict
    write_bundle(case_dir, bundle)
    if verdict.get("status") == "failed":
        return fallback
    return format_verdict_replies(bundle, case_dir.name, owner_lines=_owner_lines(case_dir, mode, runtime, budget))


def _measure_share(share, ctx, config, runtime, media_dir):
    """Returns (case_dir, fetch error name or None). The reel file is gone when this returns."""
    text = share.get("caption") or NO_CAPTION
    source = classify_remote(text)
    if config.video != "on" or not share.get("media_url") or source.kind != "text":
        return Path(check(text, ctx, source=source, tag=share["mid"])), None
    if is_reel_page(share["media_url"]):   # watch downloads it once, anonymously (preflight blocks cookies)
        return Path(check(text, ctx, source=source, tag=share["mid"], video=share["media_url"])), None
    work = Path(media_dir) / share["mid"].replace("/", "_")[:64]
    try:
        try:
            video = runtime.fetch(share["media_url"], work)
        except Exception as error:  # expired link, not a video, too large: fall back to the caption
            return Path(check(text, ctx, source=source, tag=share["mid"])), type(error).__name__
        return Path(check(text, ctx, source=source, tag=share["mid"], video=video)), None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build(share, ctx, config, mode, runtime, budget, media_dir):
    case_dir, fetch_error = _measure_share(share, ctx, config, runtime, media_dir)
    bundle = _attach_dm(case_dir, share, fetch_error)
    if config.verdict != "on":
        return case_dir, format_reply(bundle, case_dir.name)
    return case_dir, _verdict_replies(bundle, case_dir, ctx, mode, runtime, budget)


def _acknowledge(share, send, now_s):
    """Tells the sender the share arrived before the slow part; best effort, never blocks the check."""
    text = ACK.get(share.get("kind"))
    if not (send and text and within_reply_window(share.get("timestamp_ms", 0), now_s)):
        return False
    try:
        send(share["sender"], text)
        return True
    except Exception as error:  # the result reply still goes out through the normal send path
        print(f"ack not sent: {type(error).__name__}", file=sys.stderr)
        return False


REPLY_VERSION = "4.6"   # bump when the reply text or what feeds it changes, so old saved replies are not reused
RULES = f"{grade.RULES_VERSION}.{score.SCORE_VERSION}.{claims.CLAIMS_PROMPT_SHA[:8]}.{REPLY_VERSION}"
CACHED_NOTE = "같은 게시물을 {}에 확인한 결과를 다시 보내 드려요. 스타 수 등은 그때 기준이에요."


def _reel_key(share, mode, config):
    """Only shared posts with a media id are cached; the caption hash catches edits, RULES catches new code."""
    if not share.get("media_id"):
        return None
    caption = hashlib.sha256((share.get("caption") or "").encode()).hexdigest()[:16]
    return f"{RULES}:{mode}:{config.verdict}:{config.video}:{share.get('kind')}:{share['media_id']}:{caption}"


def _when(saved_at, now_s):
    days = int((now_s - saved_at) // 86400)
    return "오늘" if days == 0 else f"{days}일 전"


def _measured_fully(value):
    """No measurement anywhere inside reports failed/partial (star history, contributors, advisories, …)."""
    if isinstance(value, dict):
        return value.get("status") not in ("failed", "partial") and all(_measured_fully(v) for v in value.values())
    return all(_measured_fully(v) for v in value) if isinstance(value, list) else True


def _complete(bundle):
    """Nothing transient went wrong, so the reply is worth repeating for a week: extraction finished, the video
    was fetched and read (when tried), every primary tool was measured fully and every lookup ended."""
    tools = (bundle.get("verdict") or {}).get("tools") or []
    evidence = bundle.get("evidence") or {}
    primaries = [e for e in bundle.get("entities") or [] if e.get("role") == "primary"]
    return ((bundle.get("extraction") or {}).get("status") == "ok"
            and evidence.get("status") in ("not_run", "ok", "no_speech") and "screen_error" not in evidence
            and not str((bundle.get("dm") or {}).get("video", "")).startswith("not fetched")
            and all(_measured_fully(e.get("measurements") or {}) for e in primaries)
            and all(t.get("alternatives_checked", True) for t in tools))


def _reusable(case_dir, config):
    """Only replies worth repeating for a week: complete, and a finished verdict when verdicts are on."""
    bundle = json.loads((Path(case_dir) / "bundle.json").read_text())
    verdict_ok = (bundle.get("verdict") or {}).get("status") == "ok" if config.verdict == "on" else True
    return verdict_ok and _complete(bundle)


def _remember(runtime, key, case_dir, texts, config, now_s, lookups_failed=False):
    if runtime.store is None or lookups_failed:   # a transient lookup failure must not be repeated for a week
        return
    try:
        if _reusable(case_dir, config):
            runtime.store.reel_put(key, texts, str(case_dir), now_s)
    except Exception as error:  # the cache is a convenience; the reply already exists
        print(f"cache not saved: {type(error).__name__}", file=sys.stderr)


def _saved_reply(runtime, key, now_s):
    try:
        saved = runtime.store.reel_get(key, now_s) if runtime.store is not None else None
        return [CACHED_NOTE.format(_when(saved[1], now_s)), *saved[0]] if saved else None
    except Exception as error:  # a broken cache means measuring again, never a lost share
        print(f"cache not read: {type(error).__name__}", file=sys.stderr)
        return None


def _is_small_talk(share, config):
    """Public mode: a plain text DM from a limited sender is not a check, so it spends neither Claude nor their limit."""
    return (is_public(config) and share.get("sender") not in config.unlimited_senders
            and share.get("kind") == "text" and classify_remote(share.get("caption") or "").kind == "text")


def _item_cost(budget, ctx, usage_before, prices):
    """USD for the whole item: verdict tokens plus extraction and on-screen reading (ctx.chat), or None without prices."""
    if not prices:
        return None
    tokens = getattr(ctx.chat, "usage", [])[usage_before:]
    reading = sum(t["input_tokens"] * prices[0] + t["output_tokens"] * prices[1] for t in tokens) / 1_000_000
    return round((estimate_cost(budget, prices) or 0) + reading, 6)


def _check_share(share, ctx, config, runtime, now_s, media_dir, send=None):
    mode, today, sender = _mode(share, config), _today(now_s), share["sender"]
    if share.get("kind") == UNSUPPORTED:
        guide = [UNSUPPORTED_GUIDE] if runtime.limits.first_notice(f"unsupported:{sender}", today) else []
        return {"mode": mode, "unsupported": True, "messages": new_messages(guide)}
    if _is_small_talk(share, config):
        guide = [SHARE_GUIDE] if runtime.limits.first_notice(f"guide:{sender}", today) else []
        return {"mode": mode, "small_talk": True, "messages": new_messages(guide)}
    unlimited = sender in config.unlimited_senders
    if not unlimited and not runtime.limits.allow(sender, today):
        notice = [LIMIT_REACHED] if runtime.limits.first_notice(sender, today) else []
        return {"mode": mode, "limited": True, "messages": new_messages(notice)}
    key = _reel_key(share, mode, config)
    saved = _saved_reply(runtime, key, now_s)
    if saved:
        if not unlimited:
            runtime.limits.record(sender, today, 0.0 if runtime.prices else None)
        return {"mode": mode, "cached": True, "cache_key": key, "messages": new_messages(saved)}
    acknowledged = runtime.acknowledge and _acknowledge(share, send, now_s)
    usage_before = len(getattr(ctx.chat, "usage", []))
    budget = Budget(deadline_s=ITEM_DEADLINE_S)   # started now: the deadline counts from measurement start
    if hasattr(ctx.sources, "deadline"):          # every GitHub/HTTP call and watch stop at the same moment
        ctx.sources.deadline = budget.started_at + ITEM_DEADLINE_S
    errors_before = len(getattr(ctx.sources, "errors", []))
    try:
        case_dir, texts = _build(share, ctx, config, mode, runtime, budget, media_dir)
        result = {"mode": mode, "case_dir": str(case_dir), "messages": new_messages(texts), "cache_key": key}
        failed_lookups = len(getattr(ctx.sources, "errors", [])) > errors_before
        _remember(runtime, key, case_dir, texts, config, now_s, failed_lookups)
    except Exception as error:  # one bad share must not block the queue; the sender still hears back
        result = {"mode": mode, "error": type(error).__name__, "messages": new_messages([EXTRACTION_FAILED])}
    finally:
        if hasattr(ctx.sources, "deadline"):
            ctx.sources.deadline = None
    result["acknowledged"] = acknowledged
    if not unlimited:
        runtime.limits.record(sender, today, _item_cost(budget, ctx, usage_before, runtime.prices))
    return result


def _previous_record_id(inbox, sender):
    mine = [(record["share"].get("timestamp_ms", 0), path.stem) for path, record in inbox.done()
            if record and (record.get("share") or {}).get("sender") == sender
            and record["share"].get("kind") not in ("dispute", UNSUPPORTED)]
    return max(mine)[1] if mine else None


def _forget_disputed(inbox, runtime, previous):
    if runtime.store is None or not previous:
        return
    record = inbox.read(inbox.root / "done" / f"{previous}.json") or {}
    try:
        if record.get("cache_key"):
            runtime.store.reel_forget(record["cache_key"])
    except Exception as error:  # the dispute itself is already saved
        print(f"cache not cleared: {type(error).__name__}", file=sys.stderr)


def _save_dispute(inbox, share, now_s):
    folder = inbox.root / "disputes"
    folder.mkdir(exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    name = share["mid"] if SAFE_MID.fullmatch(share["mid"]) else inbox.key(share["mid"])[:-len(".json")]
    _write_private(folder / f"{name}.json", {"mid": share["mid"], "sender": share["sender"], "at": now_s,
                                             "previous": _previous_record_id(inbox, share["sender"])})
    return name


def process_one(inbox, item, ctx, config, send, now_s, runtime=None, clock=None):
    """Processes one queue item and sends its messages right away (when `send` is given).

    `clock` gives the time for each send attempt (default: `now_s` frozen)."""
    runtime = _runtime(inbox, runtime)
    if inbox.already_done(item):
        return None
    share = inbox.read(item)
    if not share or not isinstance(share.get("mid"), str):
        return inbox.quarantine(item, "unreadable")
    if not is_allowed(share.get("sender"), config):
        return inbox.quarantine(item, "sender not allowed")
    if share.get("kind") == "dispute":
        result = {"mode": _mode(share, config), "dispute": _save_dispute(inbox, share, now_s), "messages": []}
        _forget_disputed(inbox, runtime, _previous_record_id(inbox, share["sender"]))
    else:
        result = _check_share(share, ctx, config, runtime, now_s, inbox.root / "media", send)
    inbox.finish(item, share, result)
    path = inbox.root / "done" / item.name
    record = inbox.read(path)
    if send and record:
        record = send_messages(inbox, path, record, send, config, clock or (lambda: now_s), runtime.sleep)
    return record


def _retry_unsent(inbox, send, config, runtime, clock):
    for path, record in inbox.done():
        if record and has_unsent(record):
            send_messages(inbox, path, record, send, config, clock, runtime.sleep)


def _before_pass(runtime):
    if runtime.before_pass is None:
        return
    try:
        runtime.before_pass()
    except Exception as error:  # upkeep must not stop replies
        print(f"before_pass failed: {type(error).__name__}", file=sys.stderr)


def _one_pass(inbox, ctx, config, send, runtime, clock):
    _before_pass(runtime)
    inbox.sweep_media()
    sweep_video_leftovers(ctx.out_root)
    if send:
        _retry_unsent(inbox, send, config, runtime, clock)
    for item in inbox.pending():
        process_one(inbox, item, ctx, config, send, clock(), runtime, clock)


def process_pending(inbox, ctx, config, send=None, now_s=None, runtime=None):
    """One pass under the worker lock; returns every done record."""
    runtime = _runtime(inbox, runtime)
    with inbox.worker_lock():
        _one_pass(inbox, ctx, config, send, runtime, lambda: time.time() if now_s is None else now_s)
        return [record for _, record in inbox.done()]


def run_loop(inbox, ctx, config, send, interval_s=INTERVAL_S, stop=None, runtime=None, clock=time.time,
             retention_days=None, poll_s=POLL_S):
    """Looks for new shares every `poll_s`; a full pass runs for those and at least every `interval_s`,
    until `stop()` is true (Ctrl-C in the CLI)."""
    runtime = _runtime(inbox, runtime)
    last_pass = None
    while not (stop and stop()):
        if last_pass is None or clock() - last_pass >= interval_s or inbox.pending():
            with inbox.worker_lock():
                if retention_days:
                    inbox.prune(days=retention_days)
                _one_pass(inbox, replace(ctx, now=clock()), config, send, runtime, clock)
            last_pass = clock()
        runtime.sleep(poll_s)
