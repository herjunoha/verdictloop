"""verdictloop baseline | try -m "what changed" | reseed | status

The agent edits the target, runs `try`, reads one verdict. VALIDATED keeps the edit and it becomes the new
baseline. Anything else puts the target back.
"""
import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

from . import policy
from .config import ConfigError, load_patterns, load_project, load_scenarios
from .runner import BudgetExceeded, Ctx, RunPaused, load_runs, pass_summary, run_pass

TSV_HEADER = "try\tverdict\ttuning\theldout\tdelta\tband\ttokens\taction\tdescription\n"


class Blocked(Exception):
    """An expected refusal: the message tells the agent what to do."""


# ---------------------------------------------------------------- fingerprints and snapshots
def _file_digest(items) -> str:
    h = hashlib.sha256()
    for rel, p in items:
        h.update(rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _files_under(base: Path, root: Path):
    paths = [base] if base.is_file() else sorted(p for p in base.rglob("*")
                                                   if p.is_file() and "__pycache__" not in p.parts)
    return [(p.relative_to(root).as_posix(), p) for p in paths]


def _target_items(proj):
    return [it for f in proj.target_files for it in _files_under(f, proj.root)]


def target_sha(proj) -> str:
    return _file_digest(_target_items(proj))


def target_size(proj) -> int:
    return sum(p.stat().st_size for _, p in _target_items(proj))


def eval_fingerprint(proj) -> str:
    """Hash of everything that judges the target: eval/ plus the config. Fixed at baseline."""
    return _file_digest(_files_under(proj.eval_dir, proj.root) + [("verdictloop.toml", proj.root / "verdictloop.toml")])


def snapshot(proj, dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    for f in proj.target_files:
        rel = f.relative_to(proj.root)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        if f.is_dir():
            shutil.copytree(f, dest / rel, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(f, dest / rel)


def restore(proj, src: Path):
    for f in proj.target_files:
        rel = f.relative_to(proj.root)
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
            shutil.copytree(src / rel, f)
        else:
            shutil.copy2(src / rel, f)


# ---------------------------------------------------------------- state and logs
def _state(proj) -> dict:
    f = proj.state_dir / "state.json"
    if not f.exists():
        raise Blocked("no baseline yet: run `verdictloop baseline` first.")
    return json.loads(f.read_text(encoding="utf-8"))


def _save(proj, state):
    proj.state_dir.mkdir(parents=True, exist_ok=True)
    (proj.state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _log(proj, row):
    new = not proj.results.exists()
    with open(proj.results, "a", encoding="utf-8") as f:
        if new:
            f.write(TSV_HEADER)
        f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in row) + "\n")


def _fmt(x):
    return "n/a" if x is None else f"{x:.4f}"


def _mean(passes, key):
    scores = policy.valid_scores([p[key] for p in passes])
    return statistics.mean(scores) if scores else None


def _context(proj):
    return load_scenarios(proj), load_patterns(proj)


def _check_eval_unchanged(proj, state):
    if eval_fingerprint(proj) != state["fingerprint"]:
        raise Blocked("eval/ or verdictloop.toml changed since the baseline. Restore them, or `baseline --reset` "
                      "to accept the change and start the history over.")


# ---------------------------------------------------------------- running the eval
def _setup(proj):
    if proj.setup_command:
        r = subprocess.run(proj.setup_command, cwd=str(proj.root), capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise Blocked(f"[target] setup failed (rc={r.returncode}): {(r.stderr or r.stdout)[:300]}")


def _write_diagnosis(proj, final, scenarios, pass_id, sha):
    """eval/-independent file the improving agent may read: the tuning scenarios that did not fully pass in the
    last pass, with their transcripts, events and traces. Held-out scenarios never appear."""
    lines = [f"# Last eval: tuning scenarios that did not fully pass\n",
             f"pass {pass_id}, target version {sha[:8]}. Held-out scenarios are never shown.\n"]
    shown = 0
    for sid, r in sorted(final.items()):
        if r["set"] != "tuning" or r["status"] != "OK" or r["passed"]:
            continue
        shown += 1
        bad = [c for c in r["rules"] if not c["passed"]]
        lines.append(f"\n## {sid}  score {r['score']:.2f}" + (f"  tag {r['tag']}" if r.get("tag") else ""))
        for c in bad:
            lines.append(f"- failed check `{c['id']}` ({c['type']}): {c['detail']}")
        if r["judge"] and r["judge"]["verdict"] == "FAIL":
            lines.append(f"- judge said FAIL: {r['judge']['reason']}")
        lines.append("")
        for m in r["messages"]:
            who = "User" if m["role"] == "user" else "Assistant"
            extra = ""
            if m.get("events"):
                extra += f"  [events: {', '.join(m['events'])}]"
            if m.get("metrics"):
                extra += f"  [metrics: {json.dumps(m['metrics'])}]"
            lines.append(f"{who}: {m['text']}{extra}")
            if m.get("trace"):
                trace = m["trace"] if isinstance(m["trace"], str) else json.dumps(m["trace"])
                lines.append(f"  trace: {trace[:1500]}")
    if not shown:
        lines.append("\nEvery tuning scenario passed in the last pass.")
    (proj.state_dir).mkdir(parents=True, exist_ok=True)
    (proj.root / "last_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _evaluate(proj, scenarios, patterns, prefix, ctx):
    passes, final = [], {}
    for i in range(1, proj.repeats + 1):
        _setup(proj)
        final = run_pass(f"{prefix}-{i}", ctx)
        s = pass_summary(final, scenarios)
        if s["infra_share"] > policy.MAX_INFRA_FRACTION:
            raise Blocked(f"{s['infra_share']:.0%} of scenario runs failed on infrastructure (over "
                          f"{policy.MAX_INFRA_FRACTION:.0%}); fix the target or judge command and rerun.")
        passes.append({"pass_id": f"{prefix}-{i}", **s})
        print(f"{prefix}-{i}  tuning {_fmt(s['tuning']['score'])}  heldout {_fmt(s['heldout']['score'])}", flush=True)
    _write_diagnosis(proj, final, scenarios, passes[-1]["pass_id"], ctx.surface_sha)
    return passes


# ---------------------------------------------------------------- commands
def baseline(proj, reset=False, max_tries=0, target=0.0, patience=0):
    if (proj.state_dir / "state.json").exists():
        if not reset:
            raise Blocked("a baseline already exists. `baseline --reset` starts over (history in results.tsv stays).")
        shutil.rmtree(proj.state_dir)
    scenarios, patterns = _context(proj)
    ctx = Ctx(proj, scenarios, patterns, target_sha(proj))
    start = ctx.tokens
    passes = _evaluate(proj, scenarios, patterns, "base", ctx)
    if any(p["tuning"]["status"] != "OK" or p["heldout"]["status"] != "OK" for p in passes):
        raise Blocked("baseline incomplete (some scenarios could not run); rerun `verdictloop baseline`.")
    state = {"fingerprint": eval_fingerprint(proj), "kept_sha": target_sha(proj), "baseline": passes, "tries": 0,
             "no_progress": 0, "reseeds": 0, "size0": target_size(proj), "stopped": "",
             "limits": {"max_tries": max_tries, "target": target, "patience": patience}}
    _save(proj, state)
    snapshot(proj, proj.state_dir / "kept")
    n_t = sum(1 for s in scenarios if s["set"] == "tuning")
    tun = [p["tuning"]["score"] for p in passes]
    band, mean, held = policy.noise_band(tun, n_t), statistics.mean(tun), _mean(passes, "heldout")
    _log(proj, [0, "BASELINE", _fmt(mean), _fmt(held), "", _fmt(band), ctx.tokens - start, "kept", "baseline"])
    print("---")
    print(f"tuning:    {_fmt(mean)}  band {_fmt(band)}  (room to improve {1 - mean:.4f}, room to fall {mean:.4f})")
    print(f"heldout:   {_fmt(held)}")
    print(f"tokens:    {ctx.tokens - start}")
    print("diagnosis: last_eval.md (the tuning scenarios that did not fully pass)")
    if 1 - mean <= band:
        print("warning:   no room to improve beyond the noise band; add harder scenarios.")
    if mean <= band:
        print("warning:   no room to fall beyond the noise band; a bad edit cannot be shown to be worse.")
    return state


def _stop_reason(state, mean_tuning):
    lim = state["limits"]
    if lim["max_tries"] and state["tries"] >= lim["max_tries"]:
        return f"try budget reached ({state['tries']}/{lim['max_tries']})"
    if lim["target"] and mean_tuning >= lim["target"]:
        return f"tuning score {mean_tuning:.4f} reached the target {lim['target']}"
    if lim["patience"] and state["no_progress"] >= lim["patience"]:
        return f"{state['no_progress']} tries in a row without progress (patience {lim['patience']})"
    return ""


def try_edit(proj, message=""):
    state = _state(proj)
    _check_eval_unchanged(proj, state)
    if state.get("stopped"):
        raise Blocked(f"session complete: {state['stopped']}. Report your results; `baseline --reset` starts a new session.")
    sha = target_sha(proj)
    if sha == state["kept_sha"]:
        raise Blocked("the target is identical to the kept version; edit it first.")
    scenarios, patterns = _context(proj)
    n = state["tries"] + 1
    ctx = Ctx(proj, scenarios, patterns, sha)
    start = ctx.tokens
    size = target_size(proj)
    cand = _evaluate(proj, scenarios, patterns, f"t{n:03d}", ctx)
    n_t = sum(1 for s in scenarios if s["set"] == "tuning")
    base = state["baseline"]
    v = policy.decide([b["tuning"] for b in base], [c["tuning"] for c in cand],
                      [b["heldout"] for b in base], [c["heldout"] for c in cand],
                      n_tuning=n_t, n_heldout=len(scenarios) - n_t)
    tokens = ctx.tokens - start
    if v.label == "VALIDATED":
        action = "kept"
        state.update(kept_sha=sha, baseline=cand, tries=n, no_progress=0)
        snapshot(proj, proj.state_dir / "kept")
    elif v.label == "INFRA_FAIL":
        action = "unchanged (rerun)"
    else:
        action = "reverted"
        snapshot(proj, proj.state_dir / "discarded" / f"t{n:03d}")
        restore(proj, proj.state_dir / "kept")
        state.update(tries=n, no_progress=state["no_progress"] + 1)
    state["stopped"] = "" if v.label == "INFRA_FAIL" else _stop_reason(state, _mean(state["baseline"], "tuning"))
    _save(proj, state)
    _log(proj, [n, v.label, _fmt(_mean(cand, "tuning")), _fmt(_mean(cand, "heldout")), _fmt(v.delta), _fmt(v.band),
                tokens, action, message])
    flags = policy.tag_drops([b["outcomes"] for b in base], [c["outcomes"] for c in cand],
                             {s["id"]: s.get("tag") for s in scenarios})
    print("---")
    print(f"verdict:   {v.label}  ({v.reason})")
    print(f"tuning:    delta {_fmt(v.delta)}  band {_fmt(v.band)}")
    print(f"heldout:   delta {_fmt(v.heldout_delta)}  band {_fmt(v.heldout_band)}")
    print(f"tokens:    {tokens}")
    print(f"size:      {size} bytes ({(size / state['size0'] - 1) * 100:+.0f}% vs the baseline target)")
    print(f"action:    {action}")
    failing = {}
    for c in cand:
        for sid, bad in c["failures"].items():
            failing.setdefault(sid, [0, bad])[0] += 1
    if failing:
        print(f"failing tuning scenarios (passes failed, of {len(cand)}); details in last_eval.md:")
        for sid, (k, bad) in sorted(failing.items()):
            print(f"  {sid}  {k}/{len(cand)}  {', '.join(bad)}")
    dropped = [t for t, r in flags.items() if r["flagged"]]
    if dropped:
        print("advisory:  these tags got worse even if the score rose: " + ", ".join(dropped))
    if state["stopped"]:
        print(f"SESSION COMPLETE: {state['stopped']}")
    return {"verdict": v.label, "action": action, "n": n, "delta": v.delta, "band": v.band,
            "heldout_delta": v.heldout_delta, "tag_report": flags, "failing": failing, "stopped": state["stopped"]}


def reseed(proj):
    """Re-measure the kept target and make that the baseline. A kept edit was chosen because it scored well,
    partly by luck; fresh passes give the next try an honest bar."""
    state = _state(proj)
    _check_eval_unchanged(proj, state)
    if target_sha(proj) != state["kept_sha"]:
        raise Blocked("the target differs from the kept version; run `try` or restore it first.")
    scenarios, patterns = _context(proj)
    ctx = Ctx(proj, scenarios, patterns, state["kept_sha"])
    start = ctx.tokens
    n = state["reseeds"] + 1
    fresh = _evaluate(proj, scenarios, patterns, f"r{n:03d}", ctx)
    old, new = _mean(state["baseline"], "tuning"), _mean(fresh, "tuning")
    state.update(baseline=fresh, reseeds=n)
    _save(proj, state)
    _log(proj, ["-", "RESEED", _fmt(new), _fmt(_mean(fresh, "heldout")), _fmt(new - old), "", ctx.tokens - start,
                "baseline replaced", f"re-measured the kept target (was {old:.4f})"])
    print("---")
    print(f"reseed:    tuning baseline {_fmt(old)} -> {_fmt(new)}")
    return state


def status(proj):
    state = _state(proj)
    t, h = _mean(state["baseline"], "tuning"), _mean(state["baseline"], "heldout")
    tokens = sum(r.get("tokens", 0) for r in load_runs(proj))
    print(f"tries: {state['tries']}  no progress in a row: {state['no_progress']}  reseeds: {state['reseeds']}")
    print(f"kept target: tuning {_fmt(t)}  heldout {_fmt(h)}  gap {_fmt(t - h)} (a large gap means overfitting)")
    print(f"tokens used: {tokens}   size: {target_size(proj)} bytes ({(target_size(proj) / state['size0'] - 1) * 100:+.0f}%)")
    if state.get("stopped"):
        print(f"SESSION COMPLETE: {state['stopped']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verdictloop", description="An improvement loop for AI agents and chatbots.")
    ap.add_argument("--project", default=".", help="folder holding verdictloop.toml (default: here)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline", help="run the eval on the unchanged target")
    b.add_argument("--reset", action="store_true", help="discard the current baseline and start over")
    b.add_argument("--max-tries", type=int, default=0, help="stop the session after this many tries (0 = no limit)")
    b.add_argument("--target", type=float, default=0.0, help="stop once the tuning score reaches this (0 = off)")
    b.add_argument("--patience", type=int, default=0, help="stop after this many tries in a row without progress (0 = off)")
    t = sub.add_parser("try", help="run the eval on the edited target and decide: keep or revert")
    t.add_argument("-m", "--message", default="", help="what you changed and why, logged in results.tsv")
    sub.add_parser("reseed", help="re-measure the kept target and make that the baseline")
    sub.add_parser("status", help="tries so far, the kept target's scores and the overfitting gap")
    a = ap.parse_args(argv)
    try:
        proj = load_project(a.project)
        if a.cmd == "baseline":
            baseline(proj, a.reset, a.max_tries, a.target, a.patience)
        elif a.cmd == "try":
            try_edit(proj, a.message)
        elif a.cmd == "reseed":
            reseed(proj)
        else:
            status(proj)
    except (ConfigError, Blocked) as e:
        print(f"blocked: {e}")
        return 2
    except RunPaused as e:
        print(f"paused: {e}. Rerun the same command to resume.")
        return 3
    except BudgetExceeded as e:
        print(f"stopped: {e}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
