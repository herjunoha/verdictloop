# verdictloop

An improvement loop for an AI agent or chatbot. A coding agent edits the target, a fixed eval scores it, and the edit is kept only if it is really better. Built on the [autoresearch](https://github.com/karpathy/autoresearch) idea (an eval, one thing to edit, an instruction file, keep or revert), with one addition: a verdict that says whether a change is better, indistinguishable from luck, or worse, because a chatbot's score is noisy.

```
project/
  program.md          instructions for the improving agent        (you steer by editing this)
  target/             what gets improved: prompt, config, tool descriptions, code
  eval/               fixed: tuning.json  heldout.json  patterns.json  judge.md  facts.md
  verdictloop.toml    how to call your agent (and your judge)
  results.tsv         one line per try
  last_eval.md        written each run: the tuning scenarios that failed, with transcripts and traces
```

## The loop

The improving agent (Claude Code, say, reading `program.md`) repeats:

1. read `results.tsv` and `last_eval.md`, diagnose one failure from its transcript or trace
2. make one edit to `target/`
3. `verdictloop try -m "cause -> edit"`
4. read the verdict

```
---
verdict:   VALIDATED  (better beyond the band, no held-out regression)
tuning:    delta 0.2857  band 0.1429
heldout:   delta 0.2500  band 0.2500
tokens:    3321
size:      281 bytes (+9% vs the baseline target)
action:    kept
failing tuning scenarios (passes failed, of 3); details in last_eval.md:
  T03  3/3  states_fine
```

`VALIDATED` keeps the edit and it becomes the new baseline. `NEUTRAL` and `REFUTED` put the target back (the discarded version is saved in `.verdictloop/discarded/`). `INFRA_FAIL` means the run itself broke: nothing is decided and the edit stays, run `try` again.

Other commands: `verdictloop baseline`, `verdictloop reseed`, `verdictloop status`.

## What the verdict adds

- **Noise band.** `baseline` runs the eval three times on the unchanged target. A change smaller than `max(2 x sd, one scenario)` is not evidence.
- **Held-out set.** `eval/heldout.json` is never shown to the agent. Better on tuning but worse on held-out is REFUTED as overfitting. `status` shows the tuning versus held-out gap.
- **Broken runs are not zeros.** They are excluded and rerun.
- **Fixed eval.** `eval/` and `verdictloop.toml` are fingerprinted at baseline. If either changes, `try` refuses to run.
- **Honest bar.** A kept edit was picked because it scored well, partly by luck. `reseed` re-measures the kept target and makes that the baseline.
- **Advisory.** If a tagged group of scenarios got worse while the score rose, `try` says so. It never changes the verdict.
- **Optional stop rules**, set when you run `baseline`: `--max-tries N`, `--target SCORE`, `--patience N` (tries in a row without progress). When one is hit, `try` prints `SESSION COMPLETE` and refuses further tries.

## Run the demo (no model, no key)

```
pip install -e ".[dev]"
python -m pytest -q

cp -r examples/offline_demo my_demo && cd my_demo     # edit "python" in verdictloop.toml if it is not on your PATH
python -m verdictloop baseline
echo IGNORE_POLICY >> target/prompt.md
python -m verdictloop try -m "known bad edit"         # REFUTED, put back
# fix the prompt yourself (Loans last 21 days) and try again: VALIDATED
```

The demo's chatbot is a deterministic stand-in script, so its numbers show the mechanics, not a result.

## Use it on your own agent or chatbot

**1. The target command.** `[target] command` is an executable that answers one turn: JSON in on stdin, JSON out on stdout.

```
in:   {"conversation_id", "scenario", "turn", "context": {...},
       "messages": [{"role": "user" | "assistant", "text": "..."}]}            the conversation so far
out:  {"reply": "...",                                    required
       "events":  ["tool:lookup", "handover"],            optional: what the agent did besides talk
       "metrics": {"confidence": 0.82},                   optional: numbers the agent reports
       "trace":   "intent=refund; kb hits: doc 3, doc 9", optional: why it did it, shown in last_eval.md
       "tokens":  123}                                    optional: for the token cap
```

A stateless wrapper works because `messages` is the whole conversation; a stateful agent can key its session on `conversation_id`. If the agent lives elsewhere (a deployed service, a framework), the command is a few lines that call it. If edits must be pushed somewhere before they take effect, set `[target] setup`: it runs before every pass. Point it at a development copy, never production. `examples/claude_chatbot.py` is a complete command for a prompt-only chatbot on `claude -p`.

**2. The editable files.** `[target] files` lists the files or folders the agent may edit.

**3. The eval.** `eval/tuning.json` (what the agent optimizes) and `eval/heldout.json` (what it never sees), about two thirds and one third, around 24 in all. A scenario:

```json
{"id": "T07", "tag": "W4", "context": {"member": "gold"},
 "opening": "Our reading week starts Monday. Reply INFO for details.",
 "turns": ["INFO", "What do I owe on card 1234?"],
 "checks": [{"type": "present", "pattern": "reading week", "turn": 0},
            {"type": "event", "event": "tool:fines", "turn": 1},
            {"type": "metric", "name": "confidence", "min": 0.6},
            {"type": "max_seconds", "seconds": 10}],
 "judge": {"criterion": "The reply stays friendly and invents no policy."}}
```

- `opening` is something the agent says first (a broadcast, a greeting); it is shown to the agent but never scored.
- `present` / `absent` take a `pattern` (regex) or a `set` from `patterns.json` (a set may list `negation_terms` so "I can't guarantee" does not count as a promise). `event` / `no_event` look at what the agent reported. `metric` bounds a reported number. `max_seconds` bounds the time the engine measured.
- `turn` limits a check to one reply (0 is the first). Without it a check looks at every reply.
- **Scoring.** A scenario scores the share of its checks that passed (the judge counts as one), and a set scores the mean. Use a rule wherever a rule works and `judge` only where it cannot. Protect facts that must not change (prices, policy) with `absent` checks, so breaking them costs score.
- `tag` names a planted weakness a scenario probes; scenarios the starting target already passes make good over-correction guards.

**4. The judge (only if you used one).** `[judge] command` takes `{"model","system","user"}` and returns `{"text","tokens"}`. `eval/judge.md` is its system prompt, `eval/facts.md` the ground truth only it sees. Use a different model from the one behind your agent. `examples/claude_cli.py` is that command for `claude -p`.

**5. Plant a few known weaknesses** in the starting target and tag the scenarios that probe them, so you know what the loop should find.

**6. Edit `program.md`, then run `verdictloop baseline`.** Read the noise band and the warnings: no room to improve means the eval is too easy, no room to fall means a bad edit cannot be shown worse. Calibrate once: a deliberately bad edit should be REFUTED, a blank-line edit NEUTRAL.

**7. Hand the agent `program.md` and let it run.** Everything it did is in `results.tsv`. A fresh agent session can pick up at any time, because all state is in files.

## Config (`verdictloop.toml`)

```toml
[target]
files = ["target/"]                  # required
command = ["python", "my_agent.py"]  # required
setup = []                           # optional, runs before every pass

[judge]                              # optional
command = ["python", "my_judge.py"]
model = "..."

[run]
repeats = 3        # passes per baseline and per try, at least 3
workers = 2        # scenarios in parallel
max_tokens = 0     # 0 = no cap
timeout = 150      # seconds per command call
```

## Limits

- The fixed-eval check is a fingerprint, not a lock. It stops the agent from silently changing the eval, and `program.md` forbids it, but nothing stops it from editing the engine. Run the engine installed, outside the agent's reach, if that matters. Commit the eval and config before the first baseline; the commit timestamp is the pre-registration.
- The held-out set is hidden by convention. `try` and `last_eval.md` never show held-out details, but the agent can read `eval/heldout.json` and `logs/runs.jsonl` if it ignores `program.md`.
- Three passes is the minimum for a noise band, not a recommendation. A wide band means more repeats or more scenarios.
- Claims are only "in this setup": one target, one run configuration, small numbers.
- Rate limits: a command that prints `{"rate_limited": true}` (or limit wording on stderr) makes the engine wait 1, 2, 4, 8 minutes, then stop; rerunning resumes. Detection for `claude -p` rests on assumed signals and has not met a real limit, and the `claude -p` scripts have not been run live from this project.
- If your model call cannot set temperature, the judge is pinned only by model id and prompt hash.

## Layout

`verdictloop/` is the engine: `policy.py` (the verdict), `checks.py`, `runner.py` (commands, retry, passes), `cli.py` (baseline, try, reseed, status), `config.py`. `examples/` holds the offline demo and the two `claude -p` scripts. License: Apache-2.0.
