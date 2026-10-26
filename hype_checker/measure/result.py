import time


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ok(value, note=""):
    return {"value": value, "status": "ok", "note": note, "fetched_at": _now()}


def partial(value, note):
    return {"value": value, "status": "partial", "note": note, "fetched_at": _now()}


def failed(note):
    return {"value": None, "status": "failed", "note": note, "fetched_at": _now()}
