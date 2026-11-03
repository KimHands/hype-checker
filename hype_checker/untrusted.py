"""Helpers for handling untrusted text in LLM prompts and replies (no I/O)."""

import re

_TAG = re.compile(r"<\s*(/?)\s*(untrusted)", re.IGNORECASE)


def untrusted_block(source, text):
    safe = _TAG.sub(r"<\\\1\2", text or "")
    return f'<untrusted source="{source}">\n{safe}\n</untrusted>'


def json_text(reply):
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", reply, flags=re.DOTALL)
    return (fenced.group(1) if fenced else reply).strip()


def collapse(text):
    return " ".join(text.split())
