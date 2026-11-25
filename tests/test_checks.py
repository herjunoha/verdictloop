import pytest

from verdictloop.checks import run_check, run_checks

SETS = {
    "window": [r"30[- ]day", r"\bthirty\b"],
    "promise": {"patterns": [r"guarantee[ds]?", r"\bdefinitely\b"], "negation_terms": [r"guarantee[ds]?", r"definitely"]},
}


def reply(text, *events):
    return {"role": "assistant", "text": text, "events": list(events)}


def ok(check, msgs):
    return run_check(check, msgs, SETS)["passed"]


def test_present_uses_pattern_set_or_inline_pattern():
    assert ok({"type": "present", "set": "window"}, [reply("Within a 30-day window.")])
    assert not ok({"type": "present", "set": "window"}, [reply("Sure, anytime.")])
    assert ok({"type": "present", "pattern": r"\b21\b"}, [reply("Loans last 21 days.")])


def test_absent_strips_negated_phrases():
    c = {"type": "absent", "set": "promise"}
    assert ok(c, [reply("I can't guarantee delivery by Friday.")])
    assert not ok(c, [reply("I guarantee it arrives Friday.")])
    assert ok(c, [reply("It will not definitely arrive, sorry.")])


def test_absent_without_negation_terms_is_a_plain_match():
    assert not run_check({"type": "absent", "set": "x"}, [reply("We do not refund.")], {"x": [r"refund"]})["passed"]


def test_event_and_no_event_look_at_what_the_target_reported():
    msgs = [reply("Passing you on.", "handover", "tool:lookup")]
    assert ok({"type": "event", "event": "HANDOVER"}, msgs)
    assert ok({"type": "event", "event": "tool:lookup"}, msgs)
    assert not ok({"type": "no_event", "event": "handover"}, msgs)
    assert ok({"type": "no_event", "event": "tool:other"}, msgs)


def test_events_in_the_reply_text_do_not_count_as_events():
    assert not ok({"type": "event", "event": "handover"}, [reply("handover")])


def test_user_lines_are_not_scored_as_replies():
    assert ok({"type": "absent", "set": "promise"}, [{"role": "user", "text": "I guarantee it"}, reply("Hello")])


def test_unknown_type_and_unknown_set_raise():
    with pytest.raises(ValueError):
        run_check({"type": "nope"}, [], SETS)
    with pytest.raises(ValueError):
        run_check({"type": "present", "set": "missing"}, [reply("x")], SETS)


def test_run_checks_keeps_ids():
    res = run_checks([{"type": "present", "pattern": "hello", "id": "greets"}], [reply("hello world")], SETS)
    assert res[0]["passed"] and res[0]["id"] == "greets"


# ---------------------------------------------------------------- turns, metrics, latency, scripted opening
def two_turns():
    return [{"role": "user", "text": "a"}, reply("first answer", "e1"), {"role": "user", "text": "b"},
            reply("second answer", "e2")]


def test_a_check_with_turn_only_looks_at_that_reply():
    msgs = two_turns()
    assert ok({"type": "present", "pattern": "second", "turn": 1}, msgs)
    assert not ok({"type": "present", "pattern": "second", "turn": 0}, msgs)
    assert ok({"type": "event", "event": "e2", "turn": 1}, msgs)
    assert not ok({"type": "event", "event": "e2", "turn": 0}, msgs)
    assert ok({"type": "present", "pattern": "second"}, msgs)                 # no turn: every reply


def test_a_turn_that_does_not_exist_fails_the_check():
    res = run_check({"type": "present", "pattern": "x", "turn": 5}, two_turns(), SETS)
    assert not res["passed"] and "no reply at turn 5" in res["detail"]


def test_the_scripted_opening_is_never_scored_and_does_not_shift_turn_numbers():
    msgs = [{"role": "assistant", "text": "guarantee everything!", "scripted": True}] + two_turns()
    assert ok({"type": "absent", "set": "promise"}, msgs)
    assert ok({"type": "present", "pattern": "first", "turn": 0}, msgs)


def test_metric_checks_bounds_and_requires_the_value_to_be_reported():
    msgs = [{**reply("x"), "metrics": {"confidence": 0.8}}, {**reply("y"), "metrics": {"confidence": 0.4}}]
    assert ok({"type": "metric", "name": "confidence", "min": 0.3}, msgs)
    assert not ok({"type": "metric", "name": "confidence", "min": 0.5}, msgs)          # one reply is below
    assert ok({"type": "metric", "name": "confidence", "min": 0.5, "turn": 0}, msgs)
    assert ok({"type": "metric", "name": "confidence", "max": 0.9}, msgs)
    res = run_check({"type": "metric", "name": "score", "min": 0}, msgs, SETS)
    assert not res["passed"] and "not reported" in res["detail"]


def test_max_seconds_uses_the_time_the_engine_measured():
    fast, slow = {**reply("x"), "ms": 900}, {**reply("y"), "ms": 4200}
    assert ok({"type": "max_seconds", "seconds": 5}, [fast, slow])
    assert not ok({"type": "max_seconds", "seconds": 2}, [fast, slow])
    assert ok({"type": "max_seconds", "seconds": 2, "turn": 0}, [fast, slow])
