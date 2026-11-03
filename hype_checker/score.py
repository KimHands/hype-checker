"""Code-only recommendation score 0-10 (design-v4 §1). No LLM.

The grade (👍🤔👎❔) says how well the post checks out; the score says how much to
recommend the tool itself, so it reads only the tool's own signals (adoption, upkeep,
warnings). What the post claimed does not move it, and neither does a more popular
alternative: that pick is Claude's and code cannot verify it, so it is shown, not scored. A tool-level red flag (security holder, impersonation) caps it at 3; ❔ and tools
with no code measurement (remote MCP, products) have none.
"""

SCORE_VERSION = "7"
BASE = 5
RED_FLAG_CAP = 3
RED_FLAGS = ("warning.security_holder", "identity.impersonation")
WARNING_IDS = ("warning.install_scripts", "warning.advisories", "warning.fork",
               "warning.repo_mismatch", "warning.archived")
MAX_WARNING_PENALTY = 2
IN_USE_STARS = 100


def _stars_points(stars):
    if stars is None:
        return 0
    if stars >= 50000:
        return 3
    if stars >= 10000:
        return 2
    if stars >= 1000:
        return 1
    if stars >= IN_USE_STARS:
        return 0
    return -2 if stars < 10 else -1


def _activity_points(days, stars):
    """Fresh pushes only say something once people use it: an owner can always commit to their own repo."""
    if days is None:
        return 0
    if days <= 30:
        return 1 if stars >= IN_USE_STARS else 0
    return -1 if days > 180 else 0


def score_tool(grade, signal_ids, stars=None, pushed_days=None):
    """Returns (score or None, [(points, reason_key)]). `signal_ids`: fact ids plus grade reason ids."""
    if grade == "❔" or grade is None or stars is None:   # nothing measured about the tool itself
        return None, []
    warnings = set(signal_ids) & set(WARNING_IDS)
    parts = [(_stars_points(stars), "stars"), (_activity_points(pushed_days, stars), "activity"),
             (-min(len(warnings), MAX_WARNING_PENALTY), "warnings")]
    total = max(0, min(10, BASE + sum(points for points, _ in parts)))
    if any(i in RED_FLAGS for i in signal_ids):
        return min(total, RED_FLAG_CAP), [p for p in parts if p[0]] + [(0, "red_flag")]
    return total, [p for p in parts if p[0]]
