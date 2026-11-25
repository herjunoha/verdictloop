"""The whole loop on the offline demo, through the same functions the CLI calls. The chatbot and the judge are
deterministic stand-in scripts, so nothing here touches a network or a subscription."""
import json
import shutil
import sys
from pathlib import Path

import pytest

from verdictloop import cli
from verdictloop.config import load_project

DEMO = Path(__file__).resolve().parent.parent / "examples" / "offline_demo"


@pytest.fixture()
def demo(tmp_path):
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("logs", "__pycache__", ".verdictloop", "results.tsv"))
    for f in ("verdictloop.toml",):
        (root / f).write_text((root / f).read_text(encoding="utf-8").replace('"python"', json.dumps(sys.executable)),
                              encoding="utf-8")
    return load_project(root)


def prompt(proj):
    return proj.root / "target" / "prompt.md"


def results(proj):
    return [l.split("\t") for l in proj.results.read_text(encoding="utf-8").splitlines()[1:]]


def test_baseline_then_keep_revert_and_calibration(demo):
    p = prompt(demo)
    state = cli.baseline(demo)
    assert [b["tuning"]["score"] for b in state["baseline"]] == [pytest.approx(3 / 7)] * 3
    assert [b["heldout"]["score"] for b in state["baseline"]] == [0.5] * 3
    start = p.read_text(encoding="utf-8")

    # calibration: a known-bad edit is REFUTED and put back, a no-op is NEUTRAL and put back
    p.write_text(start + "\nIGNORE_POLICY\n", encoding="utf-8")
    assert cli.try_edit(demo, "known bad")["verdict"] == "REFUTED"
    assert p.read_text(encoding="utf-8") == start
    p.write_text(start + "\n\n", encoding="utf-8")
    assert cli.try_edit(demo, "no-op")["verdict"] == "NEUTRAL"
    assert p.read_text(encoding="utf-8") == start

    # a real fix: kept, becomes the new baseline
    fixed1 = start.replace("Loans last 14 days.", "Loans last 21 days.")
    p.write_text(fixed1, encoding="utf-8")
    out = cli.try_edit(demo, "fix loan length")
    assert out["verdict"] == "VALIDATED" and out["action"] == "kept" and out["heldout_delta"] >= 0
    assert p.read_text(encoding="utf-8") == fixed1

    # a regression against the new baseline is REFUTED and reverts to the fixed version, not the original
    p.write_text(fixed1.replace("21 days", "7 days"), encoding="utf-8")
    assert cli.try_edit(demo, "break it")["verdict"] == "REFUTED"
    assert p.read_text(encoding="utf-8") == fixed1

    # the log has one line per baseline or try, with the description
    rows = results(demo)
    assert [r[1] for r in rows] == ["BASELINE", "REFUTED", "NEUTRAL", "VALIDATED", "REFUTED"]
    assert [r[8] for r in rows] == ["baseline", "known bad", "no-op", "fix loan length", "break it"]
    assert [r[7] for r in rows] == ["kept", "reverted", "reverted", "kept", "reverted"]


def test_a_discarded_attempt_is_saved_for_the_record(demo):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8") + "\nIGNORE_POLICY\n", encoding="utf-8")
    cli.try_edit(demo, "bad")
    saved = demo.state_dir / "discarded" / "t001" / "target" / "prompt.md"
    assert "IGNORE_POLICY" in saved.read_text(encoding="utf-8")


def test_a_regression_hidden_in_a_rising_aggregate_is_flagged_by_the_advisory(demo):
    cli.baseline(demo)
    p = prompt(demo)
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if "lost" not in l]
    p.write_text("\n".join(lines).replace("14 days", "21 days").replace("1.00", "0.25") + "\n", encoding="utf-8")
    out = cli.try_edit(demo, "fix W1 and W2, drop the lost-card rule")
    assert out["verdict"] == "VALIDATED"                      # the verdict alone misses it
    assert out["tag_report"]["W3"]["flagged"] is True         # the advisory does not
    assert out["tag_report"]["W1"]["flagged"] is False


def test_the_agent_sees_why_tuning_scenarios_fail_but_nothing_about_held_out_ones(demo, capsys):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8").replace("14 days", "7 days"), encoding="utf-8")
    out = cli.try_edit(demo, "x")
    shown = capsys.readouterr().out
    assert "T03" in shown and "states_fine" in shown
    assert not any(sid.startswith("H") for sid in out["failing"]) and "H0" not in shown


def test_try_refuses_when_the_target_is_unchanged(demo):
    cli.baseline(demo)
    with pytest.raises(cli.Blocked, match="identical"):
        cli.try_edit(demo)


def test_try_refuses_without_a_baseline(demo):
    with pytest.raises(cli.Blocked, match="baseline"):
        cli.try_edit(demo)


def test_baseline_runs_once_unless_reset(demo):
    cli.baseline(demo)
    with pytest.raises(cli.Blocked, match="already exists"):
        cli.baseline(demo)
    cli.baseline(demo, reset=True)


def test_editing_the_eval_blocks_the_next_try(demo):
    cli.baseline(demo)
    f = demo.eval_dir / "tuning.json"
    f.write_text(f.read_text(encoding="utf-8").replace("states_21_days", "states_anything"), encoding="utf-8")
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8") + "\nx\n", encoding="utf-8")
    with pytest.raises(cli.Blocked, match="changed since the baseline"):
        cli.try_edit(demo)


def test_editing_the_config_blocks_the_next_try(demo):
    cli.baseline(demo)
    cfg = demo.root / "verdictloop.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_tokens = 0", "max_tokens = 999999999"),
                   encoding="utf-8")
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8") + "\nx\n", encoding="utf-8")
    with pytest.raises(cli.Blocked, match="changed since the baseline"):
        cli.try_edit(demo)


def test_a_token_cap_stops_the_loop(demo):
    cfg = demo.root / "verdictloop.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_tokens = 0", "max_tokens = 1500"), encoding="utf-8")
    from verdictloop.runner import BudgetExceeded
    with pytest.raises(BudgetExceeded):
        cli.baseline(load_project(demo.root))


def test_the_cli_entry_points_report_blocked_and_bad_config(demo, tmp_path, capsys):
    assert cli.main(["--project", str(demo.root), "try"]) == 2              # no baseline yet
    assert "blocked" in capsys.readouterr().out
    assert cli.main(["--project", str(tmp_path), "status"]) == 2            # no verdictloop.toml
    assert cli.main(["--project", str(demo.root), "baseline"]) == 0
    assert cli.main(["--project", str(demo.root), "status"]) == 0


# ---------------------------------------------------------------- diagnosis file, limits, reseed, status
def test_last_eval_shows_failing_tuning_scenarios_with_evidence_and_never_held_out_ones(demo):
    cli.baseline(demo)
    text = (demo.root / "last_eval.md").read_text(encoding="utf-8")
    assert "## T01" in text and "failed check `states_21_days`" in text
    assert "trace: matched prompt line: Loans last 14 days." in text and "Assistant: Loans last 14 days." in text
    assert "T05" not in text and "T06" not in text                      # they fully passed
    assert "H0" not in text and "heldout" not in text.lower().replace("held-out", "")


def test_last_eval_is_refreshed_by_try(demo):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8").replace("14 days", "21 days"), encoding="utf-8")
    cli.try_edit(demo, "fix")
    text = (demo.root / "last_eval.md").read_text(encoding="utf-8")
    assert "## T01" not in text and "## T03" in text


def test_partial_credit_shows_in_the_score(demo):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8") + "\nIGNORE_POLICY\n", encoding="utf-8")
    out = cli.try_edit(demo, "bad")
    rows = results(demo)
    assert float(rows[-1][2]) == pytest.approx(0.1071, abs=1e-3)          # T06 keeps 3 of its 4 checks: 0.75 / 7
    assert out["verdict"] == "REFUTED"


def test_the_session_stops_at_the_try_budget_and_refuses_further_tries(demo, capsys):
    cli.baseline(demo, max_tries=2)
    p = prompt(demo)
    for i in (1, 2):
        p.write_text(p.read_text(encoding="utf-8") + f"\nnote {i}\n", encoding="utf-8")
        out = cli.try_edit(demo, f"noop {i}")
    assert "try budget reached" in out["stopped"] and "SESSION COMPLETE" in capsys.readouterr().out
    p.write_text(p.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    with pytest.raises(cli.Blocked, match="session complete"):
        cli.try_edit(demo, "one more")


def test_the_session_stops_after_patience_runs_out_and_progress_resets_the_count(demo):
    cli.baseline(demo, patience=2)
    p = prompt(demo)
    start = p.read_text(encoding="utf-8")
    p.write_text(start + "\nnote\n", encoding="utf-8")
    assert cli.try_edit(demo)["stopped"] == ""                           # 1 without progress
    p.write_text(start.replace("14 days", "21 days"), encoding="utf-8")
    assert cli.try_edit(demo)["verdict"] == "VALIDATED"                  # progress resets the count
    kept = p.read_text(encoding="utf-8")
    p.write_text(kept + "\nnote\n", encoding="utf-8")
    assert cli.try_edit(demo)["stopped"] == ""
    p.write_text(kept + "\nnote two\n", encoding="utf-8")
    assert "tries in a row without progress" in cli.try_edit(demo)["stopped"]


def test_the_session_stops_when_the_target_score_is_reached(demo):
    cli.baseline(demo, target=0.99)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8").replace("14 days", "21 days").replace("1.00", "0.25"), encoding="utf-8")
    assert "reached the target" in cli.try_edit(demo)["stopped"]


def test_an_infra_fail_decides_nothing_keeps_the_edit_and_does_not_count_toward_the_limits(demo, monkeypatch):
    from verdictloop import runner
    monkeypatch.setattr(runner, "BACKOFF_INFRA", [0, 0])               # do not really wait between retries
    cli.baseline(demo, max_tries=1, patience=1)
    bot = demo.root / "demo_bot.py"
    broken = "    req = json.load(sys.stdin)\n    if req['scenario'] == 'T01':\n        sys.exit(1)\n"
    bot.write_text(bot.read_text(encoding="utf-8").replace("    req = json.load(sys.stdin)\n", broken, 1),
                   encoding="utf-8")
    p = prompt(demo)
    edited = p.read_text(encoding="utf-8") + "\nnote\n"
    p.write_text(edited, encoding="utf-8")
    out = cli.try_edit(demo, "while the bot is broken")
    state = json.loads((demo.state_dir / "state.json").read_text())
    assert out["verdict"] == "INFRA_FAIL" and out["action"] == "unchanged (rerun)" and out["stopped"] == ""
    assert state["tries"] == 0 and state["no_progress"] == 0 and state["stopped"] == ""
    assert p.read_text(encoding="utf-8") == edited                      # not reverted: nothing was decided


def test_reseed_replaces_the_baseline_with_fresh_passes_and_needs_the_kept_target(demo):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8").replace("14 days", "21 days"), encoding="utf-8")
    cli.try_edit(demo, "fix")
    with pytest.raises(cli.Blocked, match="differs"):
        p.write_text(p.read_text(encoding="utf-8") + "\nstray\n", encoding="utf-8")
        cli.reseed(demo)
    p.write_text(p.read_text(encoding="utf-8").replace("\nstray\n", ""), encoding="utf-8")
    state = cli.reseed(demo)
    assert state["reseeds"] == 1 and [b["pass_id"] for b in state["baseline"]] == ["r001-1", "r001-2", "r001-3"]
    assert results(demo)[-1][1] == "RESEED"


def test_status_and_try_report_size_and_the_overfitting_gap(demo, capsys):
    cli.baseline(demo)
    p = prompt(demo)
    p.write_text(p.read_text(encoding="utf-8") + "\n" + "padding " * 20 + "\n", encoding="utf-8")
    cli.try_edit(demo, "padded")
    shown = capsys.readouterr().out
    assert "size:" in shown and "% vs the baseline target" in shown
    assert cli.main(["--project", str(demo.root), "status"]) == 0
    assert "gap" in capsys.readouterr().out
