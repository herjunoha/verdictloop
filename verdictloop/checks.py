"""Rule checks: pure functions over a transcript. No model calls.

Transcript: a list of messages {"role": "user" | "assistant", "text": str}. Assistant messages from the target
also carry "events" (strings), "metrics" (numbers) and "ms" (time the turn took). A scenario's scripted
"opening" is an assistant message with "scripted": True; checks never score it.

Check types (a check is a dict with "type", an optional "id", and an optional "turn": 0 = the first reply):
  present / absent   "pattern": a regex, or "set": a name from eval/patterns.json; over the reply text.
                     A set with "negation_terms" is matched after negated phrases such as
                     "I can't guarantee" are stripped, so a refusal does not count as a promise.
  event / no_event   "event": a string the target must (not) have reported.
  metric             "name", and "min" and/or "max": a number the target reported (a confidence, a score).
  max_seconds        "seconds": every checked reply must have come back within that time (measured by the engine).
Without "turn" a check looks at every reply. Anything a rule cannot decide goes to the judge instead.
"""
import re

NEGATORS = (r"(?:can't|cannot|can not|couldn't|don't|do not|doesn't|does not|won't|will not|unable to|"
            r"not able to|isn't|is not|aren't|are not|no|never|not|without|rather than|instead of)")


def _replies(messages):
    return [m for m in messages if m["role"] == "assistant" and not m.get("scripted")]


def reply_text(replies) -> str:
    text = "\n".join(m["text"] for m in replies)
    return text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _patterns(check, sets):
    """Return (patterns, negation_terms). A set is a list of regexes, or {"patterns": [...], "negation_terms": [...]}."""
    if "pattern" in check:
        return [check["pattern"]], []
    val = sets.get(check.get("set"))
    if val is None:
        raise ValueError(f"unknown pattern set {check.get('set')!r}")
    if isinstance(val, dict):
        return list(val.get("patterns", [])), list(val.get("negation_terms", []))
    return list(val), []


def _strip_negated(text, terms):
    if not terms:
        return text
    return re.sub(rf"\b{NEGATORS}\s+(?:[\w$.,-]+\s+){{0,3}}?(?:{'|'.join(terms)})\b", " ", text, flags=re.I)


def _hits(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


def run_check(check: dict, messages, sets) -> dict:
    t = check["type"]
    replies = _replies(messages)
    if "turn" in check:
        replies = [replies[check["turn"]]] if 0 <= check["turn"] < len(replies) else None
    if replies is None:
        passed, detail = False, f"no reply at turn {check['turn']}"
    elif t == "present":
        pats, _ = _patterns(check, sets)
        hit = _hits(pats, reply_text(replies))
        passed, detail = bool(hit), f"hits={len(hit)}"
    elif t == "absent":
        pats, neg = _patterns(check, sets)
        hit = _hits(pats, _strip_negated(reply_text(replies), neg))
        passed, detail = not hit, f"hits={hit}"
    elif t in ("event", "no_event"):
        seen_all = {e.lower() for m in replies for e in m.get("events", [])}
        seen = check["event"].lower() in seen_all
        passed, detail = (seen if t == "event" else not seen), f"events={sorted(seen_all)}"
    elif t == "metric":
        values = [m["metrics"][check["name"]] for m in replies if check["name"] in m.get("metrics", {})]
        ok = bool(values) and all(v >= check.get("min", float("-inf")) and v <= check.get("max", float("inf"))
                                  for v in values)
        passed, detail = ok, f"{check['name']}={values}" if values else f"{check['name']} not reported"
    elif t == "max_seconds":
        times = [m.get("ms", 0) / 1000 for m in replies]
        passed, detail = bool(times) and max(times) <= check["seconds"], f"seconds={[round(x, 2) for x in times]}"
    else:
        raise ValueError(f"unknown check type {t!r}")
    return {"id": check.get("id", t), "type": t, "passed": bool(passed), "detail": detail}


def run_checks(checks, messages, sets):
    return [run_check(c, messages, sets) for c in checks]
