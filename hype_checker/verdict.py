"""Ties official text, facts, claim check and grade into the bundle's `verdict` block (design-v3 §2 ②–⑤, §4).

Grades come from code (grade.py); Claude only runs the claim check. Every reason
shown to a reader is one flattened line of at most 120 chars. Nothing is written
to disk here: the caller stores the result as bundle["verdict"].

Model name in `repro`: the `model` argument wins, else `chat.model` if the chat has one.
"""

import math
import os
import time
from dataclasses import dataclass, field

from hype_checker import claims, grade, samename, score
from hype_checker.alternatives import MIN_FACTOR as ALTERNATIVE_FACTOR, MIN_STARS as MIN_ALTERNATIVE_STARS, find_alternatives
from hype_checker.claims import check_claims
from hype_checker.facts import build_facts
from hype_checker.grade import grade_tool
from hype_checker.labels import CLAIM_RESULT
from hype_checker.llm import ClaudeUnavailable
from hype_checker.official import collect_official
from hype_checker.resolve import normalize

MAX_REASON_CHARS = 120
CHARS_PER_TOKEN = 3  # conservative: Korean text runs close to one token per 1–3 chars
CLAIM_IN_REASON = 30
CLAIM_IN_ENTRY = 60
IMPERSONATION_TEXT = "같은 이름의 원본으로 보이는 다른 레포가 있어요"
ERROR_TEXT = "확인 중 오류가 났어요"
NOT_RUN = {"status": "not_run", "source": None, "paragraphs": [], "fetched_at": None, "note": "예산 초과로 수집 안 함"}


@dataclass
class Budget:
    max_tools: int = 6
    max_calls: int = 8
    max_input_tokens: int = 40000
    deadline_s: float = 180
    clock: object = time.monotonic
    started_at: float = None
    calls: int = field(default=0, init=False)
    tokens_in: int = field(default=0, init=False)
    tokens_out: int = field(default=0, init=False)

    def __post_init__(self):
        if self.started_at is None:
            self.started_at = self.clock()

    def spend(self, calls, input_tokens, output_tokens):
        self.calls += calls
        self.tokens_in += input_tokens
        self.tokens_out += output_tokens

    def time_left(self):
        return self.deadline_s - (self.clock() - self.started_at)

    def exhausted(self):
        return (self.calls >= self.max_calls or self.tokens_in >= self.max_input_tokens
                or self.clock() - self.started_at >= self.deadline_s)


def _flat(text, limit):
    return " ".join(str(text).split())[:limit]


def _tool_key(entity):
    """Identified tools dedupe on the exact identifier (a/b-c and a/bc are different repos);
    only unidentified ones fall back to the loose name match."""
    if entity.get("identifier"):
        from hype_checker.v2.store import tool_key
        return "id:" + tool_key(entity["identifier"])
    key = normalize(entity.get("name_latin") or entity.get("name") or "")
    return key or _flat(entity.get("name") or "", 200).lower()


def _unique_primaries(entities):
    """One entry per tool; spellings that resolved to the same tool pool all their claims. No cap here:
    if the pooled check does not fit the budget, _affordable cuts it and the verdict says so (partial)."""
    seen = {}
    for entity in entities:
        if entity.get("role") != "primary":
            continue
        key = _tool_key(entity)
        if key not in seen:
            seen[key] = {**entity, "claims": list(entity.get("claims") or [])}
            continue
        pooled = seen[key]["claims"]
        pooled += [c for c in entity.get("claims") or [] if c not in pooled]
    return list(seen.values())


def _claim_texts(entity):
    return {f"c{n}": text for n, text in enumerate(entity.get("claims") or [], 1)}


def _reason_text(reason_id, fact_texts, claim_texts, results):
    if reason_id == "identity.impersonation":
        return IMPERSONATION_TEXT
    if reason_id.startswith("claim."):
        claim_id = reason_id.removeprefix("claim.")
        shown = CLAIM_RESULT.get(results.get(claim_id), results.get(claim_id))
        return f"캡션의 '{_flat(claim_texts.get(claim_id, ''), CLAIM_IN_REASON)}' → {shown}"
    return fact_texts.get(reason_id, reason_id)


def _reasons(reason_ids, facts, claim_texts, claim_results):
    fact_texts = {f["id"]: f["text"] for f in facts["facts"]}
    results = {c["claim_id"]: c["result"] for c in claim_results}
    return [_flat(_reason_text(rid, fact_texts, claim_texts, results), MAX_REASON_CHARS) for rid in reason_ids]


def _claim_entries(claim_texts, claim_results):
    return [{"claim": _flat(claim_texts.get(c["claim_id"], ""), CLAIM_IN_ENTRY), "result": c["result"],
             "quote": _flat(c.get("quote") or "", claims.MAX_QUOTE_CHARS)} for c in claim_results]


def _usage(chat):
    usage = getattr(chat, "usage", None)
    return usage if isinstance(usage, list) else None


def _charge(budget, chat, start, outcome):
    usage = _usage(chat)
    if usage is None:
        budget.spend(outcome["calls"] if outcome else 0, 0, 0)
        return
    used = usage[start:]
    budget.spend(len(used), sum(u.get("input_tokens", 0) for u in used), sum(u.get("output_tokens", 0) for u in used))


def _failed_check(entity):
    claim_ids = [f"c{n}" for n in range(1, len(entity.get("claims") or []) + 1)]
    return {"results": [{"claim_id": cid, "result": claims.UNVERIFIABLE, "paragraph": None, "quote": ""}
                        for cid in claim_ids], "calls": 1, "status": "failed"}


def _check(entity, official, chat, budget):
    """Any Claude error but ClaudeUnavailable leaves every claim 확인불가 (status failed), so code rules still grade.

    The budget is charged either way (usage delta when the chat tracks it, else the calls reported)."""
    start, outcome = len(_usage(chat) or []), None
    try:
        outcome = check_claims(entity, official, chat, time_left=budget.time_left)
    except ClaudeUnavailable:
        raise
    except Exception:  # a bad request or broken reply must not turn the tool into ❔
        outcome = _failed_check(entity)
    finally:
        _charge(budget, chat, start, outcome)
    return outcome


def _affordable(entity, official, budget):
    """Hard caps: the worst case (every retry) must fit in the calls and input tokens left."""
    chars = claims.estimate_input_chars(entity, official)
    if not chars:
        return True
    worst_tokens = math.ceil(chars / CHARS_PER_TOKEN) * claims.MAX_ATTEMPTS
    return (not budget.exhausted() and budget.time_left() >= claims.CLAUDE_CALL_S
            and budget.max_calls - budget.calls >= claims.MAX_ATTEMPTS
            and budget.tokens_in + worst_tokens <= budget.max_input_tokens)


MAX_POINTS = 2


def _alternatives(entity, sources, chat, budget, now):
    """Runs after the claim check, on what is left. None = unknown (no budget, failure): not cached, not scored."""
    if budget.exhausted() or budget.max_calls - budget.calls < 1:
        return None
    start = len(_usage(chat) or [])
    try:
        return find_alternatives(entity, sources, chat, now, time_left=budget.time_left)
    except Exception:  # SearchIncomplete, Claude down, anything: the finished claim check must still go out
        return None
    finally:
        _charge(budget, chat, start, {"calls": 1 if len(_usage(chat) or []) > start else 0})


def _cache_get(cache, identifier, now):
    try:
        return (cache.tool_get(identifier, now) if cache else None) or {}
    except Exception:  # a broken cache means researching again, never a failed tool
        return {}


def _cache_put(cache, identifier, payload, now):
    try:
        if cache:
            cache.tool_put(identifier, payload, now)
    except Exception:  # the verdict is already made
        pass


def _official(entity, sources, budget, now, saved):
    if saved.get("official"):
        return saved["official"]
    return NOT_RUN if budget.exhausted() else collect_official(entity, sources, now)


def _still_more_popular(alternatives, entity):
    """Cached picks are rechecked against today's stars; without them nothing can be compared (None)."""
    stars = (_github_value(entity) or {}).get("stargazers_count")
    if stars is None:
        return None
    floor = max(MIN_ALTERNATIVE_STARS, stars * ALTERNATIVE_FACTOR)
    return [a for a in alternatives if (a.get("stars") or 0) >= floor]


def _found_alternatives(entity, sources, chat, budget, now, saved):
    if saved.get("alternatives") is not None:
        return _still_more_popular(saved["alternatives"], entity)
    return _alternatives(entity, sources, chat, budget, now)


def _remember_research(cache, entity, official, alternatives, saved, now):
    """Saves only what was newly learned: a fetched official text, alternatives searched to the end.
    Parts served from the cache are not saved again, so their age is never reset."""
    payload = {}
    if not saved.get("official") and official.get("status") == "ok":   # never cache a failed or partial fetch
        payload["official"] = official
    if saved.get("alternatives") is None and alternatives is not None:
        payload["alternatives"] = alternatives
    if payload:
        _cache_put(cache, entity.get("identifier"), payload, now)


def _github_value(entity):
    meta = ((entity.get("measurements") or {}).get("github") or {}).get("meta") or {}
    return meta.get("value") if meta.get("status") in ("ok", "partial") else None


def _days(stamp, now):
    from hype_checker.facts import _days_since
    return _days_since(stamp, now)


SHOWN_ELSEWHERE = {"identity.official_text", "trust.stars", "activity.pushed"}  # stars/updates have their own line
CLAIM_POINT = {claims.MATCH: "게시물의 '{}' 설명이 공식 문서와 맞아요",
               claims.MISMATCH: "게시물의 '{}' 설명이 공식 문서와 달라요"}


def _points(entity, facts, claim_texts, claim_results):
    """Friendly reasons split into good points and points to watch (each at most MAX_POINTS)."""
    shown = [f for f in facts["facts"] if f["id"] not in SHOWN_ELSEWHERE]
    good = [f["text"] for f in shown if f["kind"] in grade.CONFIRMED_KINDS]
    watch = [f["text"] for f in shown if f["kind"] in grade.CAUTION_KINDS]
    for result in claim_results:
        text = CLAIM_POINT.get(result["result"], "").format(_flat(claim_texts.get(result["claim_id"], ""), CLAIM_IN_REASON))
        if result["result"] == claims.MATCH:
            good.insert(0, text)
        elif result["result"] == claims.MISMATCH:
            watch.insert(0, text)
    if entity.get("identifier") and grade._is_impersonation(entity):
        watch.insert(0, IMPERSONATION_TEXT)
    trim = lambda items: [_flat(t, MAX_REASON_CHARS) for t in dict.fromkeys(items)][:MAX_POINTS]
    return trim(good), trim(watch)


def _judge_tool(entity, sources, chat, budget, now, cache=None):
    """Pass 1 (the core): official text, claim check, grade. Alternatives and score come in pass 2."""
    saved = _cache_get(cache, entity.get("identifier"), now)
    official = _official(entity, sources, budget, now, saved)
    facts = build_facts(entity, official, now)
    cut = budget.exhausted() or not _affordable(entity, official, budget)
    outcome = {"results": [], "status": "not_run"} if cut else _check(entity, official, chat, budget)
    cut = cut or (outcome["status"] == "not_run" and bool(entity.get("claims")))   # time ran out mid-check
    graded = grade_tool(entity, facts, outcome["results"])
    claim_texts = _claim_texts(entity)
    meta = _github_value(entity) or {}
    good, watch = _points(entity, facts, claim_texts, outcome["results"])
    tool = {"name": entity.get("name"), "identifier": entity.get("identifier"), "grade": graded["grade"],
            "reason_ids": graded["reason_ids"],
            "reasons": _reasons(graded["reason_ids"], facts, claim_texts, outcome["results"]),
            "claims": _claim_entries(claim_texts, outcome["results"]),
            "claims_status": outcome["status"], "budget_cut": cut, "claims_total": len(entity.get("claims") or []),
            "stars": meta.get("stargazers_count"), "pushed_days": _days(meta.get("pushed_at"), now),
            "good": good, "watch": watch, "cached": bool(saved), "official_status": official.get("status")}
    guess = samename.likely(entity)
    tool.update({"likely": guess} if guess else {})
    signals = {f["id"] for f in facts["facts"]} | ({"identity.impersonation"} if grade._is_impersonation(entity) else set())
    return tool, {"entity": entity, "official": official, "saved": saved, "signals": signals}


def _finish_tool(tool, work, sources, chat, budget, now, cache):
    """Pass 2, on what pass 1 left: alternatives, then the score. Tool signals only, never the post's claims."""
    entity, saved = work["entity"], work["saved"]
    alternatives = _found_alternatives(entity, sources, chat, budget, now, saved)
    _remember_research(cache, entity, work["official"], alternatives, saved, now)
    recommend, parts = score.score_tool(tool["grade"], work["signals"], tool["stars"], tool["pushed_days"])
    tool.update(score=recommend, score_parts=parts, alternatives=alternatives or [],
                alternatives_checked=alternatives is not None)
    return tool


def _error_entry(entity, error):
    return {"name": entity.get("name"), "identifier": entity.get("identifier"), "grade": "❔",
            "reason_ids": ["error"], "reasons": [ERROR_TEXT], "claims": [], "claims_status": "not_run",
            "budget_cut": False, "error": type(error).__name__}


def _safe_judge(entity, sources, chat, budget, now, cache=None):
    try:
        return _judge_tool(entity, sources, chat, budget, now, cache)
    except ClaudeUnavailable:
        raise
    except Exception as error:  # one broken tool must not sink the others
        return _error_entry(entity, error), None


def _safe_finish(tool, work, sources, chat, budget, now, cache):
    if work is None:
        return tool
    try:
        return _finish_tool(tool, work, sources, chat, budget, now, cache)
    except ClaudeUnavailable:
        raise
    except Exception:  # the graded tool stands without a score
        return {**tool, "score": None, "score_parts": [], "alternatives": [], "alternatives_checked": False}


def _status(tools):
    if tools and all("error" in t for t in tools):
        return "failed"
    troubled = any("error" in t or t["budget_cut"] or t["claims_status"] == "failed"
                   or t.get("official_status") in ("failed", "partial") for t in tools)
    return "partial" if troubled else "ok"


def _price(name):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


def estimate_cost(budget, prices=None):
    """USD for the tokens spent so far. `prices` = (in, out) per million tokens; None → HC_PRICE_* env (eval CLI)."""
    price_in, price_out = prices or (_price("HC_PRICE_IN_PER_MTOK"), _price("HC_PRICE_OUT_PER_MTOK"))
    if price_in is None or price_out is None:
        return None
    return round((budget.tokens_in * price_in + budget.tokens_out * price_out) / 1_000_000, 6)


def _repro(budget, model, prices=None):
    return {"model": model, "rules_version": grade.RULES_VERSION, "score_version": score.SCORE_VERSION,
            "claims_prompt_sha": claims.CLAIMS_PROMPT_SHA,
            "tokens_in": budget.tokens_in, "tokens_out": budget.tokens_out, "cost_usd": estimate_cost(budget, prices),
            "calls": budget.calls}


def _within_budget(primaries, max_tools):
    """Only identified tools cost Claude calls, so a tool that could not be located never takes a place."""
    skipped = [e for e in primaries if e.get("identifier")][max_tools:]
    return [e for e in primaries if not any(e is s for s in skipped)], skipped


def judge_bundle(bundle, sources, chat, budget, now, model=None, prices=None, cache=None):
    """Returns the verdict block. Raises ClaudeUnavailable (caller decides how to reply).
    `cache` (v2 Store) reuses official text and alternatives per tool for 3 days."""
    primaries = _unique_primaries(bundle.get("entities") or [])
    judged, skipped = _within_budget(primaries, budget.max_tools)
    first = [_safe_judge(entity, sources, chat, budget, now, cache) for entity in judged]
    tools = [_safe_finish(tool, work, sources, chat, budget, now, cache) for tool, work in first]
    return {"tools": tools, "skipped_tools": len(skipped), "skipped_names": [e.get("name") for e in skipped],
            "status": _status(tools),
            "repro": _repro(budget, model or getattr(chat, "model", None), prices)}
