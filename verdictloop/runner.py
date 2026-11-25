"""Runs the eval: drives the target through each scenario, applies rule checks, asks the judge where a
scenario needs one, and logs every run as JSONL.

Both the target and the judge are plain commands (one JSON object in on stdin, one out on stdout):

  target   in:  {"conversation_id", "scenario", "turn", "context", "messages": [{"role","text"}...]}   (so far)
           out: {"reply": str, "events": [str], "metrics": {name: number}, "trace": any, "tokens": int}
                (everything but "reply" is optional)
  judge    in:  {"model", "system", "user"}
           out: {"text": str, "tokens": int (optional)}

A command that exits non-zero, prints something unparseable or returns an empty reply is INFRA_FAIL.
{"rate_limited": true} (or limit wording on stderr) is RATE_LIMITED: wait, retry, and past the ceiling stop
with RunPaused. Rerunning resumes, because finished scenarios are already in the log.
"""
import hashlib
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import checks

RATE_RE = re.compile(
    r"(usage|rate|5-hour|weekly|session)\s+limit|hit your limit|limit reached|too many requests|\b429\b|quota", re.I)
BACKOFF_RATE = [60, 120, 240, 480]     # seconds; then RunPaused
BACKOFF_INFRA = [5, 20]
JUDGE_HEADER = "FACTS:\n{facts}\n\nCRITERION:\n{criterion}\n\nTRANSCRIPT:\n{transcript}"
_lock = threading.Lock()


class RunPaused(RuntimeError):
    """Rate limit persisted past the wait ceiling. Rerun the same command to resume."""


class BudgetExceeded(RuntimeError):
    pass


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def run_command(cmd, payload: dict, cwd, timeout):
    """One call. Returns (status, detail, data)."""
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        return "INFRA_FAIL", "timeout", {}
    try:
        data = json.loads(proc.stdout)
    except Exception:
        data = None
    if data is None:
        if RATE_RE.search(proc.stderr or ""):
            return "RATE_LIMITED", "stderr matched limit pattern", {}
        return "INFRA_FAIL", f"unparseable output (rc={proc.returncode}) {(proc.stderr or '')[:120]}", {}
    if data.get("rate_limited"):
        return "RATE_LIMITED", str(data.get("detail", ""))[:160], {}
    if proc.returncode != 0:
        return "INFRA_FAIL", f"rc={proc.returncode}", {}
    return "OK", "", data


class Ctx:
    """Everything a pass needs. `call(kind, payload)` is injectable so tests run with no commands."""

    def __init__(self, project, scenarios, patterns, surface_sha, call=None, sleep=time.sleep):
        self.project, self.scenarios, self.patterns, self.surface_sha = project, scenarios, patterns, surface_sha
        self.sleep = sleep
        self._call = call
        self.judge_prompt = ""
        jf = project.eval_dir / "judge.md"
        if jf.exists():
            self.judge_prompt = jf.read_text(encoding="utf-8")
        ff = project.eval_dir / "facts.md"
        self.facts = ff.read_text(encoding="utf-8") if ff.exists() else ""
        self.tokens = sum(r.get("tokens", 0) for r in load_runs(project))

    def call(self, kind, payload):
        """kind is 'target' or 'judge'. Returns the parsed reply dict; raises RunPaused/BudgetExceeded."""
        cmd = self.project.target_command if kind == "target" else self.project.judge_command
        rate_tries = infra_tries = 0
        while True:
            if self.project.max_tokens and self.tokens >= self.project.max_tokens:
                raise BudgetExceeded(f"token cap {self.project.max_tokens} reached ({self.tokens} used)")
            if self._call:
                status, detail, data = self._call(kind, payload)
            else:
                status, detail, data = run_command(cmd, payload, self.project.root, self.project.timeout)
            if status == "OK":
                with _lock:
                    self.tokens += int(data.get("tokens", 0) or 0)
                return {"status": "OK", "data": data, "tokens": int(data.get("tokens", 0) or 0)}
            if status == "RATE_LIMITED":
                if rate_tries >= len(BACKOFF_RATE):
                    raise RunPaused(f"rate limit persisted after {rate_tries} waits: {detail}")
                self.sleep(BACKOFF_RATE[rate_tries])
                rate_tries += 1
                continue
            if infra_tries >= len(BACKOFF_INFRA):
                return {"status": "INFRA_FAIL", "detail": detail, "tokens": 0}
            self.sleep(BACKOFF_INFRA[infra_tries])
            infra_tries += 1


def parse_judge(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    v = str(obj.get("verdict", "")).strip().upper()
    return {"verdict": v, "reason": str(obj.get("reason", ""))[:300]} if v in ("PASS", "FAIL") else None


def transcript_text(messages) -> str:
    return "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}" for m in messages)


def run_scenario(sc, ctx: Ctx, pass_id: str) -> dict:
    t0 = time.time()
    messages, tokens, status, detail = [], 0, "OK", ""
    if sc.get("opening"):                                  # the agent speaks first (a broadcast, a greeting)
        messages.append({"role": "assistant", "text": sc["opening"], "scripted": True})
    for ti, turn in enumerate(sc["turns"]):
        messages.append({"role": "user", "text": turn})
        t_call = time.time()
        res = ctx.call("target", {"conversation_id": f"{pass_id}-{sc['id']}", "scenario": sc["id"], "turn": ti,
                                  "context": sc.get("context", {}),
                                  "messages": [{"role": m["role"], "text": m["text"]} for m in messages]})
        tokens += res["tokens"]
        data = res.get("data", {})
        reply = str(data.get("reply", ""))
        if res["status"] != "OK" or not reply.strip():
            status, detail = "INFRA_FAIL", res.get("detail") or "empty reply"
            break
        trace = data.get("trace")
        messages.append({"role": "assistant", "text": reply.strip(), "events": [str(e) for e in data.get("events", [])],
                         "metrics": data.get("metrics", {}), "ms": int((time.time() - t_call) * 1000),
                         "trace": trace[:4000] if isinstance(trace, str) else trace})
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pass_id": pass_id, "scenario": sc["id"], "set": sc["set"],
           "tag": sc.get("tag"), "surface_sha": ctx.surface_sha, "status": status, "detail": detail,
           "messages": messages, "rules": [], "judge": None, "passed": None, "score": None}
    if status == "OK":
        rec["rules"] = checks.run_checks(sc.get("checks", []), messages, ctx.patterns)
        results = [r["passed"] for r in rec["rules"]]
        if sc.get("judge"):
            user = JUDGE_HEADER.format(facts=ctx.facts.strip() or "(none)", criterion=sc["judge"]["criterion"],
                                       transcript=transcript_text(messages))
            parsed = None
            for _ in (1, 2):
                res = ctx.call("judge", {"model": ctx.project.judge_model, "system": ctx.judge_prompt, "user": user})
                tokens += res["tokens"]
                if res["status"] == "OK":
                    parsed = parse_judge(res["data"].get("text", ""))
                    if parsed:
                        break
            if parsed is None:
                rec["status"], rec["detail"] = "INFRA_FAIL", "judge failed or unparseable twice"
            else:
                rec["judge"] = {**parsed, "judge_prompt_sha": sha256_text(ctx.judge_prompt)}
                results.append(parsed["verdict"] == "PASS")
        if rec["status"] == "OK":
            rec["score"] = sum(results) / len(results)     # partial credit: the share of this scenario's checks that passed
            rec["passed"] = all(results)
    rec["tokens"], rec["wall_ms"] = tokens, int((time.time() - t0) * 1000)
    with _lock:
        Path(ctx.project.runs_log).parent.mkdir(parents=True, exist_ok=True)
        with open(ctx.project.runs_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


def load_runs(project):
    f = Path(project.runs_log)
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []


def run_pass(pass_id: str, ctx: Ctx) -> dict:
    """One pass over every scenario. Resumes: scenarios already OK for this pass_id and this exact target
    version are skipped. INFRA_FAIL scenarios get one more sweep. Returns {scenario_id: record}."""
    final = {r["scenario"]: r for r in load_runs(ctx.project)
             if r["pass_id"] == pass_id and r["status"] == "OK" and r["surface_sha"] == ctx.surface_sha}
    todo = [s for s in ctx.scenarios if s["id"] not in final]
    for _ in (1, 2):
        if not todo:
            break
        failed = []
        with ThreadPoolExecutor(max_workers=ctx.project.workers) as ex:
            futs = [(s, ex.submit(run_scenario, s, ctx, pass_id)) for s in todo]
            fatal = None
            for s, f in futs:
                try:
                    r = f.result()
                except (RunPaused, BudgetExceeded) as e:
                    fatal = fatal or e
                    continue
                final[s["id"]] = r
                if r["status"] != "OK":
                    failed.append(s)
            if fatal:
                raise fatal
        todo = failed
    return final


def pass_summary(final: dict, scenarios) -> dict:
    """Mean scenario score per set (a scenario scores the share of its checks that passed), plus which scenarios
    fully passed and why the tuning ones did not. A set with any non-OK scenario is INFRA_FAIL, never scored."""
    out = {"outcomes": {}, "failures": {}}
    for name in ("tuning", "heldout"):
        recs = [final.get(s["id"]) for s in scenarios if s["set"] == name]
        if any(r is None or r["status"] != "OK" for r in recs):
            out[name] = {"status": "INFRA_FAIL", "score": None}
            continue
        out[name] = {"status": "OK", "score": sum(r["score"] for r in recs) / len(recs)}
    for sid, r in final.items():
        if r["status"] == "OK":
            out["outcomes"][sid] = bool(r["passed"])
            if not r["passed"] and r["set"] == "tuning":      # the tuner sees why tuning scenarios failed, never held-out ones
                bad = [c["id"] for c in r["rules"] if not c["passed"]]
                if r["judge"] and r["judge"]["verdict"] == "FAIL":
                    bad.append("judge: " + r["judge"]["reason"])
                out["failures"][sid] = bad
    out["infra_share"] = sum(1 for r in final.values() if r["status"] != "OK") / max(1, len(final))
    return out
