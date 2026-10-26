"""Loads and validates eval/goldset.yaml (design-v1 §6)."""

import yaml

VERDICT_A = {"편입", "보류", "버림"}
VERDICT_B = {"채택", "이미보유", "후보", "불필요"}
IDENTIFIER_STATUSES = {"annotated", "unannotated", "none_exists", "unverifiable"}
ROLES = {"primary", "mentioned"}


class GoldsetError(ValueError):
    pass


def _check_entity(case_id, entity):
    if entity.get("role") not in ROLES:
        raise GoldsetError(f"{case_id}: entity {entity.get('name')!r} has bad role")
    status = entity.get("identifier_status")
    if status not in IDENTIFIER_STATUSES:
        raise GoldsetError(f"{case_id}: entity {entity.get('name')!r} has bad identifier_status {status!r}")
    if status == "annotated" and not entity.get("identifier"):
        raise GoldsetError(f"{case_id}: annotated entity {entity.get('name')!r} needs an identifier")


def _check_case(case):
    case_id = case.get("id")
    verdict = case["expected"].get("verdict")
    if verdict and (verdict.get("a") not in VERDICT_A or verdict.get("b") not in VERDICT_B):
        raise GoldsetError(f"{case_id}: verdict outside template vocabulary")
    for entity in case["expected"]["entities"]:
        _check_entity(case_id, entity)


def validate(cases):
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)):
        raise GoldsetError("duplicate case ids")
    for case in cases:
        _check_case(case)
    return cases


def load_goldset(path):
    with open(path, encoding="utf-8") as handle:
        return validate(yaml.safe_load(handle)["cases"])
