"""Commands, retry, budget, scenarios and passes. The model side is faked; the command tests run tiny scripts."""
import json
import sys
from types import SimpleNamespace

import pytest

from verdictloop import runner
from verdictloop.runner import BudgetExceeded, Ctx, RunPaused


def project(tmp_path, **over):
    base = dict(root=tmp_path, eval_dir=tmp_path / "eval", runs_log=tmp_path / "logs" / "runs.jsonl",
                target_command=["x"], judge_command=["y"], judge_model="judge-m", max_tokens=0, timeout=20, workers=1)
    base.update(over)
    return SimpleNamespace(**base)


def reply(text="ok", events=(), tokens=10):
    return ("OK", "", {"reply": text, "events": list(events), "tokens": tokens})


def make_ctx(tmp_path, fake, scenarios=(), sha="s1", **over):
    (tmp_path / "eval").mkdir(exist_ok=True)
    return Ctx(project(tmp_path, **over), list(scenarios), {}, sha, call=fake, sleep=lambda s: None)


SC = {"id": "S1", "set": "tuning", "turns": ["one", "two"], "checks": [{"type": "present", "pattern": "ok"}]}


# ---------------------------------------------------------------- scenarios
def test_each_turn_sends_the_conversation_so_far_and_events_are_recorded(tmp_path):
    seen = []

    def fake(kind, payload):
        seen.append(payload)
        return reply("ok", ["tool:x"])

    rec = runner.run_scenario(SC, make_ctx(tmp_path, fake), "p1")
    assert rec["passed"] is True and len(seen) == 2
    assert [m["role"] for m in seen[1]["messages"]] == ["user", "assistant", "user"]
    assert seen[1]["turn"] == 1 and seen[1]["conversation_id"] == "p1-S1"
    assert rec["messages"][1]["events"] == ["tool:x"]


def test_failing_rule_marks_the_scenario_failed(tmp_path):
    sc = {**SC, "checks": [{"type": "present", "pattern": "needle"}]}
    assert runner.run_scenario(sc, make_ctx(tmp_path, lambda k, p: reply("hay")), "p1")["passed"] is False


def test_empty_reply_or_command_failure_is_infra_fail(tmp_path):
    assert runner.run_scenario(SC, make_ctx(tmp_path, lambda k, p: reply("  ")), "p1")["status"] == "INFRA_FAIL"
    rec = runner.run_scenario(SC, make_ctx(tmp_path, lambda k, p: ("INFRA_FAIL", "boom", {})), "p1")
    assert rec["status"] == "INFRA_FAIL" and rec["passed"] is None


def judged(**over):
    return {**SC, "turns": ["hi"], "checks": [], "judge": {"criterion": "polite"}, **over}


def test_judge_scenario_passes_and_fails_with_the_judge(tmp_path):
    def fake(verdict):
        return lambda k, p: ("OK", "", {"text": json.dumps({"verdict": verdict, "reason": "r"}), "tokens": 5}) \
            if k == "judge" else reply("hello")

    assert runner.run_scenario(judged(), make_ctx(tmp_path, fake("PASS")), "p1")["passed"] is True
    assert runner.run_scenario(judged(), make_ctx(tmp_path, fake("FAIL")), "p2")["passed"] is False


def test_unparseable_judge_twice_is_infra_fail_not_a_failed_scenario(tmp_path):
    fake = lambda k, p: ("OK", "", {"text": "maybe?"}) if k == "judge" else reply("hello")
    rec = runner.run_scenario(judged(), make_ctx(tmp_path, fake), "p1")
    assert rec["status"] == "INFRA_FAIL" and rec["passed"] is None


def test_judge_gets_facts_and_criterion_but_the_target_never_sees_them(tmp_path):
    ctx = make_ctx(tmp_path, None)
    (tmp_path / "eval" / "facts.md").write_text("SECRET GROUND TRUTH", encoding="utf-8")
    seen = {"target": [], "judge": []}

    def fake(kind, payload):
        seen[kind].append(json.dumps(payload))
        return ("OK", "", {"text": '{"verdict": "PASS", "reason": "r"}'}) if kind == "judge" else reply("hello")

    ctx = make_ctx(tmp_path, fake)                       # reload so facts.md is read
    runner.run_scenario(judged(), ctx, "p1")
    assert all("SECRET GROUND TRUTH" not in t for t in seen["target"])
    assert all("SECRET GROUND TRUTH" in t and "polite" in t for t in seen["judge"])


# ---------------------------------------------------------------- retry and budget
def test_infra_fail_is_retried_twice_then_returned(tmp_path):
    sleeps, calls = [], []
    ctx = make_ctx(tmp_path, lambda k, p: calls.append(1) or ("INFRA_FAIL", "flaky", {}))
    ctx.sleep = sleeps.append
    assert ctx.call("target", {})["status"] == "INFRA_FAIL"
    assert len(calls) == 3 and sleeps == [5, 20]


def test_rate_limit_waits_then_resumes_and_past_the_ceiling_pauses(tmp_path):
    seq = iter([("RATE_LIMITED", "429", {}), reply()])
    assert make_ctx(tmp_path, lambda k, p: next(seq)).call("target", {})["status"] == "OK"
    with pytest.raises(RunPaused):
        make_ctx(tmp_path, lambda k, p: ("RATE_LIMITED", "429", {})).call("target", {})


def test_token_cap_stops_before_the_next_call(tmp_path):
    ctx = make_ctx(tmp_path, lambda k, p: reply(tokens=60), max_tokens=100)
    ctx.call("target", {})
    ctx.call("target", {})                               # 120 used now
    with pytest.raises(BudgetExceeded):
        ctx.call("target", {})


def test_token_count_survives_a_restart(tmp_path):
    ctx = make_ctx(tmp_path, lambda k, p: reply(tokens=40))
    runner.run_scenario({**SC, "turns": ["a"]}, ctx, "p1")
    assert make_ctx(tmp_path, None).tokens == 40


# ---------------------------------------------------------------- passes
def scenarios():
    return [{"id": "T1", "set": "tuning", "turns": ["a"], "checks": [{"type": "present", "pattern": "yes"}]},
            {"id": "T2", "set": "tuning", "turns": ["b"], "checks": [{"type": "present", "pattern": "yes"}]},
            {"id": "H1", "set": "heldout", "turns": ["c"], "checks": [{"type": "present", "pattern": "yes"}]}]


def test_pass_summary_scores_each_set_and_shows_only_tuning_failures(tmp_path):
    fake = lambda k, p: reply("no" if p["scenario"] in ("T2", "H1") else "yes")
    ctx = make_ctx(tmp_path, fake, scenarios())
    s = runner.pass_summary(runner.run_pass("p1", ctx), scenarios())
    assert s["tuning"]["score"] == 0.5 and s["heldout"]["score"] == 0.0
    assert list(s["failures"]) == ["T2"]


def test_a_set_with_an_infra_failure_is_not_scored(tmp_path):
    fake = lambda k, p: ("INFRA_FAIL", "x", {}) if p["scenario"] == "H1" else reply("yes")
    ctx = make_ctx(tmp_path, fake, scenarios())
    s = runner.pass_summary(runner.run_pass("p1", ctx), scenarios())
    assert s["heldout"] == {"status": "INFRA_FAIL", "score": None} and s["tuning"]["status"] == "OK"


def test_infra_failures_get_one_more_sweep(tmp_path):
    calls = {"n": 0}

    def fake(k, p):
        calls["n"] += 1
        return ("INFRA_FAIL", "flaky", {}) if calls["n"] <= 3 else reply("yes")      # first scenario burns 3 tries

    final = runner.run_pass("p1", make_ctx(tmp_path, fake, scenarios()))
    assert all(r["status"] == "OK" for r in final.values())


def test_a_resumed_pass_skips_finished_scenarios_of_the_same_target_version_only(tmp_path):
    runner.run_pass("p1", make_ctx(tmp_path, lambda k, p: reply("yes"), scenarios(), sha="v1"))
    calls = []
    runner.run_pass("p1", make_ctx(tmp_path, lambda k, p: calls.append(1) or reply("yes"), scenarios(), sha="v1"))
    assert calls == []
    runner.run_pass("p1", make_ctx(tmp_path, lambda k, p: calls.append(1) or reply("yes"), scenarios(), sha="v2"))
    assert len(calls) == 3


def test_run_paused_propagates_so_a_rerun_can_resume(tmp_path):
    def fake(k, p):
        raise RunPaused("limit")

    with pytest.raises(RunPaused):
        runner.run_pass("p1", make_ctx(tmp_path, fake, scenarios()))


# ---------------------------------------------------------------- real commands
def script(tmp_path, body):
    p = tmp_path / "cmd.py"
    p.write_text(body, encoding="utf-8")
    return [sys.executable, str(p)]


def test_run_command_round_trip_and_failure_modes(tmp_path):
    ok = script(tmp_path, "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'reply': r['x'].upper()}))\n")
    assert runner.run_command(ok, {"x": "hi"}, tmp_path, 20) == ("OK", "", {"reply": "HI"})
    assert runner.run_command(script(tmp_path, "import json\nprint(json.dumps({'rate_limited': True}))\n"),
                              {}, tmp_path, 20)[0] == "RATE_LIMITED"
    assert runner.run_command(script(tmp_path, "import sys\nprint('{}')\nsys.exit(3)\n"), {}, tmp_path, 20)[0] == "INFRA_FAIL"
    assert runner.run_command(script(tmp_path, "print('not json')\n"), {}, tmp_path, 20)[0] == "INFRA_FAIL"
    assert runner.run_command(script(tmp_path, "import sys\nsys.stderr.write('weekly limit reached')\n"),
                              {}, tmp_path, 20)[0] == "RATE_LIMITED"
    assert not runner.RATE_RE.search("The rate of return on the lamp is not in our policy.")


# ---------------------------------------------------------------- partial credit and richer target output
def test_a_scenario_scores_the_share_of_its_checks_that_passed(tmp_path):
    sc = {"id": "S1", "set": "tuning", "turns": ["hi"],
          "checks": [{"type": "present", "pattern": "yes"}, {"type": "present", "pattern": "no"}]}
    rec = runner.run_scenario(sc, make_ctx(tmp_path, lambda k, p: reply("yes only")), "p1")
    assert rec["score"] == 0.5 and rec["passed"] is False


def test_the_set_score_is_the_mean_of_scenario_scores_and_the_failures_list_the_failed_checks(tmp_path):
    scs = [{"id": "T1", "set": "tuning", "turns": ["a"], "checks": [{"type": "present", "pattern": "yes", "id": "c1"},
                                                                     {"type": "present", "pattern": "no", "id": "c2"}]},
           {"id": "H1", "set": "heldout", "turns": ["b"], "checks": [{"type": "present", "pattern": "yes"}]}]
    s = runner.pass_summary(runner.run_pass("p1", make_ctx(tmp_path, lambda k, p: reply("yes"), scs)), scs)
    assert s["tuning"]["score"] == 0.5 and s["heldout"]["score"] == 1.0
    assert s["failures"] == {"T1": ["c2"]} and s["outcomes"] == {"T1": False, "H1": True}


def test_judge_counts_as_one_check_in_the_score(tmp_path):
    sc = judged(checks=[{"type": "present", "pattern": "hello"}])
    fake = lambda k, p: ("OK", "", {"text": '{"verdict": "FAIL", "reason": "r"}'}) if k == "judge" else reply("hello")
    assert runner.run_scenario(sc, make_ctx(tmp_path, fake), "p1")["score"] == 0.5


def test_opening_context_metrics_trace_and_timing_flow_through(tmp_path):
    sc = {"id": "S1", "set": "tuning", "turns": ["INFO"], "opening": "Free reading week! Reply INFO.",
          "context": {"member": "gold"}, "checks": [{"type": "metric", "name": "confidence", "min": 0.5}]}
    seen = []

    def fake(kind, payload):
        seen.append(payload)
        return ("OK", "", {"reply": "ok", "metrics": {"confidence": 0.9}, "trace": "retrieved: doc 3", "tokens": 5})

    rec = runner.run_scenario(sc, make_ctx(tmp_path, fake), "p1")
    assert rec["passed"] is True
    assert seen[0]["context"] == {"member": "gold"}
    assert seen[0]["messages"][0] == {"role": "assistant", "text": "Free reading week! Reply INFO."}
    assert rec["messages"][0]["scripted"] is True
    reply_msg = rec["messages"][-1]
    assert reply_msg["trace"] == "retrieved: doc 3" and isinstance(reply_msg["ms"], int)
