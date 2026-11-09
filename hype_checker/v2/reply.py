"""DM reply: a short, neutral summary. Sending is opt-in (design-v2 §5)."""

import json
import urllib.error
import urllib.request

from hype_checker import score
from hype_checker.bundle import entity_marks
from hype_checker.labels import FOUND, GRADE, KIND, STATUS, label, located, status_of

SEND_URL = "https://graph.instagram.com/v26.0/me/messages"
MAX_BYTES = 1000
TITLE = "🔎 Hyper Checker 측정 결과"
NOTICE = "※ 판정 전 자료예요. 이름은 게시물 표기 그대로이고, 좋다·나쁘다는 평가가 아니에요."
NOTHING = "게시물에서 소개하는 도구 이름을 찾지 못했어요."
LINK_PLEASE = "GitHub·npm·PyPI 링크를 알고 계시면 보내 주세요."
NOTHING_IN_IMAGE_POST = f"캡션에서 도구 이름을 찾지 못했어요. 이미지 속 글자는 아직 읽지 못해요. {LINK_PLEASE}"
NOTHING_UNREAD_VIDEO = {"restricted": f"이 릴스는 공개 범위가 제한돼 영상을 읽지 못했고, 캡션에도 도구 이름이 없었어요. {LINK_PLEASE}",
                        "unread": "인스타그램에서 영상을 불러오지 못했고, 캡션에도 도구 이름이 없었어요. 잠시 뒤 다시 보내 주세요."}
CAPTION_ONLY = "영상을 읽지 못해 캡션 내용만 확인한 결과예요."
FALLBACK_NOTE = "이번엔 판정 없이 측정만 보내요."
VERDICT_TITLE = "🔎 Hyper Checker 자동 확인 (게시물 기준)"
EXTRACTION_FAILED = "지금은 처리하지 못했어요. 잠시 뒤 다시 보내 주세요."
REASONS_NEXT = "도구별 근거는 다음 메시지로 보낼게요."
SKIPPED_TOOLS = "…외 {}개{}는 판정하지 않았어요."
FOOTER = ["자동 확인 결과예요. 기능이 좋은지는 직접 써 봐야 알아요.", "설치 전 공식 페이지를 꼭 확인하세요.",
          "틀렸다면 '정정'이라고 답장해 주세요."]


def _flat(text, limit):
    return " ".join(str(text).split())[:limit]


def _where(entity):
    identifier = entity["identifier"]
    shown = identifier.removeprefix("https://") if identifier.startswith("https://") else f"github.com/{identifier}"
    stars = (((entity.get("measurements") or {}).get("github") or {}).get("meta") or {}).get("value") or {}
    star_text = f" · ⭐ {stars['stargazers_count']:,}" if "stargazers_count" in stars else ""
    return f"{FOUND} {_flat(shown, 80)}{star_text}"


def _caution(entity):
    warning = next((mark for mark in entity_marks(entity) if mark.startswith("⚠️")), None)
    return f"\n   {_flat(warning.split(':')[0], 60)}" if warning else ""


def _entry(number, entity):
    head = f"{number}. \u201c{_flat(entity['name'], 60)}\u201d · {label(KIND, entity.get('kind'))}"
    body = _where(entity) if located(entity) else label(STATUS, status_of(entity))
    return f"{head}\n   {body}{_caution(entity)}"


def _fits(parts):
    return len("\n".join(parts).encode()) <= MAX_BYTES


def _pack(parts, separator="\n"):
    """Parts fill messages in order and continue in the next message: nothing is ever left out."""
    messages, current = [], []
    for part in parts:
        if current and not _fits([separator.join(current + [part])]):
            messages.append(separator.join(current))
            current = []
        current.append(part)
    return messages + ([separator.join(current)] if current else [])


def format_reply(bundle, case_id=None, fallback=False):
    """Measurement reply as 1–3 messages. No internal ids: the reader cannot open a 'bundle'."""
    primary = [e for e in bundle.get("entities", []) if e.get("role") == "primary"]
    intro = f"게시물에서 소개하는 도구 {len(primary)}개의 공개 위치를 찾아봤어요." if primary else _nothing_found(bundle)
    head = [TITLE, *([FALLBACK_NOTE] if fallback else []), intro, NOTICE, ""]
    return _pack(["\n".join(head), *[_entry(number, entity) for number, entity in enumerate(primary, 1)]])


def _kind_by_name(bundle):
    primary = [e for e in bundle.get("entities", []) if e.get("role") == "primary"]
    return {e["name"]: e.get("kind") for e in primary}


SCORE_PART = {"stars": "스타 규모", "activity": "업데이트", "warnings": "주의 신호"}
RED_FLAG_NOTE = "보안·사칭 신호가 있어 최대 3점"


def _score_text(tool):
    return f"추천 {tool['score']}/10" if tool.get("score") is not None else "추천 판단 보류"


def _summary_line(number, tool, kinds):
    kind = label(KIND, kinds.get(tool["name"]))
    return f"{number}. \u201c{_flat(tool['name'], 60)}\u201d · {kind}\n   {_score_text(tool)} · {label(GRADE, tool['grade'])}"


def _summary(verdict, kinds, owner_lines):
    tools, skipped = verdict["tools"], verdict.get("skipped_tools", 0)
    owner = [_flat(line, 120) for line in owner_lines or []]
    lines = [_summary_line(number, tool, kinds) for number, tool in enumerate(tools, 1)]
    names = ", ".join(_flat(name, 40) for name in verdict.get("skipped_names") or [])
    lines += [SKIPPED_TOOLS.format(skipped, f"({names})" if names else "")] if skipped else []
    return _pack(["\n".join([VERDICT_TITLE, f"도구 {len(tools)}개를 확인했어요.", ""]), *lines,
                  *([""] + owner if owner else []), "\n" + REASONS_NEXT])


def _link(tool):
    identifier = tool.get("identifier")
    if not identifier:
        return None
    shown = identifier.removeprefix("https://") if identifier.startswith("https://") else f"github.com/{identifier}"
    return f"🔗 {_bytes(_flat(shown, 80), 120)}"


def _likely(tool):
    guess = tool.get("likely")
    return f"🔎 이 도구일 수 있어요(확정 아님): github.com/{_flat(guess['identifier'], 80)} ⭐ {guess['stars']:,}" if guess else None


def _updated(days):
    if days is None:
        return None
    if days <= 30:
        return "최근 30일 안에 업데이트"
    return f"약 {int(days)}일 전 업데이트" if days <= 365 else "1년 넘게 업데이트 없음"


def _stats(tool):
    parts = ([f"⭐ {tool['stars']:,}"] if isinstance(tool.get("stars"), int) else []) + \
            [p for p in [_updated(tool.get("pushed_days"))] if p]
    return " · ".join(parts) or None


def _score_reason(tool):
    if tool.get("score") is None:
        return None
    parts = [RED_FLAG_NOTE if key == "red_flag" else f"{SCORE_PART[key]} {points:+d}"
             for points, key in tool.get("score_parts") or []]
    return "📊 " + " · ".join([f"기본 {score.BASE}점"] + parts) + f" → {tool['score']}점"


UNCHECKED_CLAIMS = "게시물 주장 {}개는 시간·예산이 모자라 공식 문서와 대조하지 못했어요"


def _unchecked_notice(tool):
    """The claim check never ran (time/budget), so every claim of the post went unchecked."""
    total = tool.get("claims_total", 0)
    return UNCHECKED_CLAIMS.format(total) if total and tool.get("claims_status") == "not_run" else None


def _section(title, lines):
    return [title, *[f" · {_flat(line, 90)}" for line in lines]] if lines else []


def _detail_lines(number, tool):
    alternatives = [f"github.com/{a['identifier']} ⭐ {a['stars']:,}" for a in tool.get("alternatives") or []]
    lines = [f"{number}. {_flat(tool['name'], 40)} — {_score_text(tool)} · {tool['grade']}", _link(tool), _likely(tool),
             *_section("⚠️ 먼저 알아 두세요", [n for n in [_unchecked_notice(tool)] if n]), _stats(tool),
             *_section("⚠️ 살펴볼 점", (tool["watch"] if "watch" in tool else tool.get("reasons") or [])[:2]),   # warnings first
             *_section("👍 좋은 점", tool.get("good") or []),
             *_section("💡 같은 일을 하는 더 유명한 도구", alternatives), _score_reason(tool)]
    return [line for line in lines if line]


def _bytes(text, limit):
    """Cuts at the last whole character that fits; only a single line longer than a message reaches this."""
    return text.encode()[:limit].decode(errors="ignore")


def _detail_messages(tools):
    """A tool that does not fit whole continues line by line; the footer closes the last message or gets its own."""
    blocks = [block for number, tool in enumerate(tools, 1)
              for block in _pack([_bytes(line, MAX_BYTES) for line in _detail_lines(number, tool)])]
    return _pack([*blocks, "\n".join(FOOTER)], "\n\n")


def _extraction_failed(bundle):
    return (bundle.get("extraction") or {}).get("status") in ("failed", "skipped")


def video_problem(bundle):
    """Why a shared video went unread: "restricted", "unread", or None when it was read (even without speech)."""
    evidence = bundle.get("evidence") or {}
    errors = " ".join(str(error) for error in evidence.get("errors") or [])
    if "available to everyone" in errors:
        return "restricted"
    return "unread" if evidence.get("status") == "watch_failed" or "Media unavailable" in errors else None


def _nothing_found(bundle):
    problem = video_problem(bundle)
    if problem:
        return NOTHING_UNREAD_VIDEO[problem]
    return NOTHING_IN_IMAGE_POST if (bundle.get("dm") or {}).get("kind") == "ig_post" else NOTHING


def _caption_only_note(bundle):
    return [CAPTION_ONLY] if video_problem(bundle) and _kind_by_name(bundle) else []


def format_fallback_reply(bundle, case_id):
    """Measurement-only reply for HC_VERDICT=on without a usable verdict; a failed extraction keeps its own notice."""
    if _extraction_failed(bundle):
        return [EXTRACTION_FAILED]
    return _caption_only_note(bundle) + format_reply(bundle, case_id, fallback=True)


def format_verdict_replies(bundle, case_id, owner_lines=None):
    if _extraction_failed(bundle):
        return [EXTRACTION_FAILED]
    tools = (bundle.get("verdict") or {}).get("tools") or []
    if not tools:
        return format_fallback_reply(bundle, case_id) if _kind_by_name(bundle) else [_nothing_found(bundle)]
    return [*_caption_only_note(bundle), *_summary(bundle["verdict"], _kind_by_name(bundle), owner_lines),
            *_detail_messages(tools)]


class SendRejected(Exception):
    """Meta answered 4xx and refused: the message was not sent, so a retry is safe."""


class SendUnknown(Exception):
    """Meta answered 5xx: it may have delivered anyway, so the message is never resent automatically."""


def _post(url, body, headers):
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            raise SendUnknown(f"HTTP {error.code}") from None
        raise SendRejected(f"HTTP {error.code}") from None


def send_reply(recipient, text, access_token, post=_post):
    body = json.dumps({"recipient": {"id": recipient}, "message": {"text": text}}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    return post(SEND_URL, body, headers)
