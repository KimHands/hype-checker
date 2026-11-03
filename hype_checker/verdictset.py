"""Loads and validates eval/verdict-goldset.yaml (design-v3 §7)."""

import datetime

import yaml

GRADES = {"👍", "🤔", "👎", "❔"}
VERDICT_B = {"채택", "이미보유", "후보", "불필요"}
SPLITS = {"dev", "holdout"}
KINDS = {"general", "adversarial"}
INPUT_TYPES = {"text", "url"}
MIN_ADVERSARIAL = 15


class VerdictsetError(ValueError):
    pass


def _in(value, allowed):
    return isinstance(value, str) and value in allowed


def _check_tool(case_id, tool):
    if not isinstance(tool, dict):
        raise VerdictsetError(f"{case_id}: tool entry must be a mapping")
    name = tool.get("name")
    if not _in(tool.get("grade"), GRADES):
        raise VerdictsetError(f"{case_id}: tool {name!r} has bad grade {tool.get('grade')!r}")
    if tool.get("b") is not None and not _in(tool["b"], VERDICT_B):
        raise VerdictsetError(f"{case_id}: tool {name!r} has bad b {tool['b']!r}")
    if not isinstance(tool.get("why"), str) or not tool["why"]:
        raise VerdictsetError(f"{case_id}: tool {name!r} needs a why")
    if "identifier" not in tool:
        raise VerdictsetError(f"{case_id}: tool {name!r} needs an identifier key (null allowed)")


def _is_iso_date(value):
    if isinstance(value, datetime.date):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def _check_case(case):
    if not isinstance(case, dict):
        raise VerdictsetError("case must be a mapping")
    case_id = case.get("id")
    if not _is_iso_date(case.get("as_of")):
        raise VerdictsetError(f"{case_id}: as_of must be a date")
    if not isinstance(case.get("input"), dict):
        raise VerdictsetError(f"{case_id}: input must be a mapping")
    if not isinstance(case.get("tools"), list):
        raise VerdictsetError(f"{case_id}: tools must be a list")
    if not _in(case.get("split"), SPLITS) or not _in(case.get("kind"), KINDS):
        raise VerdictsetError(f"{case_id}: bad split or kind")
    if not _in(case["input"].get("type"), INPUT_TYPES) or not case["input"].get("value"):
        raise VerdictsetError(f"{case_id}: bad input")
    if case["kind"] == "general" and not case.get("source_card"):
        raise VerdictsetError(f"{case_id}: general case needs source_card")
    if not case.get("tools"):
        raise VerdictsetError(f"{case_id}: needs at least one tool")
    if not isinstance(case.get("live", True), bool):
        raise VerdictsetError(f"{case_id}: live must be true or false")
    for tool in case["tools"]:
        _check_tool(case_id, tool)


def _check_ids(cases):
    if not all(isinstance(case, dict) for case in cases):
        raise VerdictsetError("case must be a mapping")
    ids = [case.get("id") for case in cases]
    if not all(isinstance(case_id, str) and case_id for case_id in ids):
        raise VerdictsetError("case id must be a non-empty string")
    if len(ids) != len(set(ids)):
        raise VerdictsetError("duplicate case ids")
    holdout_ids = [case["id"] for case in cases if case.get("split") == "holdout"]
    if len(holdout_ids) != len(set(holdout_ids)):
        raise VerdictsetError("duplicate holdout ids")


def _check_adversarial_quota(cases):
    count = sum(1 for case in cases if case.get("kind") == "adversarial")
    if count < MIN_ADVERSARIAL:
        raise VerdictsetError(f"need >= {MIN_ADVERSARIAL} adversarial cases, got {count}")


def validate(cases):
    if not isinstance(cases, list):
        raise VerdictsetError("cases must be a list")
    _check_ids(cases)
    for case in cases:
        _check_case(case)
    _check_adversarial_quota(cases)
    return cases


def load_verdictset(path):
    with open(path, encoding="utf-8") as handle:
        try:
            document = yaml.safe_load(handle)
        except yaml.YAMLError as error:
            raise VerdictsetError(f"invalid YAML: {error}") from error
    if not isinstance(document, dict) or "cases" not in document:
        raise VerdictsetError("top level must be a mapping with a cases key")
    return validate(document["cases"])
