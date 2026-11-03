"""One vocabulary for every output (bundle.md, DM reply): enum → what a reader sees.

Marks describe how far identification got, never whether a tool is good:
✅/❌ stay reserved for the human verdict card.
"""

FOUND = "🔗"
STATUS = {
    "resolved": f"{FOUND} 공개 위치 찾음",
    "from_url": f"{FOUND} 받은 링크의 위치",
    "given": f"{FOUND} 사람이 지정한 위치",
    "ambiguous": "🔀 후보가 여러 개라 아직 못 정함",
    "unresolved": "❓ 공개 위치를 못 찾음",
    "no_repo_expected": "🏷️ 상용 제품이라 공개 코드를 찾지 않음",
    "skipped_mentioned": "· 함께 언급만 됨",
}
LEGEND = "기호: 🔗 위치 찾음 · 🔀 후보 여럿 · ❓ 못 찾음 · 🏷️ 상용 제품 · ⚠️ 눈여겨볼 사실 · · 중립 사실"

KIND = {"repo": "오픈소스 레포", "npm": "npm 패키지", "pypi": "PyPI 패키지", "mcp": "MCP 서버", "skill": "스킬",
        "plugin": "플러그인", "product": "상용 제품", "feature": "기능", "unknown": "종류 모름"}
ROLE = {"primary": "소개 대상", "mentioned": "함께 언급", "dropped": "제외"}
ORIGIN = {"caption": "캡션", "transcript": "영상 음성", "title": "제목", "url": "받은 링크", "human": "사람이 추가"}
STEP = {"ok": "완료", "partial": "일부만", "failed": "실패", "not_run": "안 함", "skipped": "건너뜀",
        "empty_input": "입력 없음"}
GRADE = {"👍": "👍 공식 설명 기준 확인됨", "🤔": "🤔 주의해서", "👎": "👎 권하지 않아요", "❔": "❔ 판정 보류"}
CLAIM_RESULT = {"일치": "일치", "불일치": "불일치", "확인불가": "확인 못 함"}
DM_KIND = {"ig_reel": "릴스", "ig_post": "게시물", "text": "메시지"}
PLATFORM = {"pasted": "붙여 넣은 글", "local": "영상 파일", "instagram": "인스타그램 링크", "youtube": "유튜브 링크",
            "threads": "Threads 링크", "github": "GitHub 링크", "npm": "npm 링크", "pypi": "PyPI 링크",
            "web": "웹 링크"}


def label(table, key):
    return table.get(key, key or "-")


def status_of(entity):
    return (entity.get("resolution") or {}).get("status")


def located(entity):
    return bool(entity.get("identifier")) and status_of(entity) in ("resolved", "from_url", "given")


def source_label(bundle):
    dm = bundle.get("dm")
    if dm:
        return f"DM으로 받은 {label(DM_KIND, dm.get('kind'))} (캡션만 처리)"
    return label(PLATFORM, bundle["source"]["platform"])
