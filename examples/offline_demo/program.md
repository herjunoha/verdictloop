# verdictloop program

You are improving an AI chatbot (or agent) by trial and error. Make one change at a time, measure it, keep it only if it is really better. A deliberate, boring loop beats a clever rewrite.

## Setup (once)

1. Read `README.md`, everything under `target/` (the only things you may edit) and `eval/tuning.json` (the scenarios you optimize for).
2. Do NOT read `eval/heldout.json` or `logs/`. Held-out scenarios exist to catch overfitting. If you design for them they stop meaning anything. `last_eval.md` is safe: it only ever shows tuning scenarios.
3. If the target is a live agent that `setup` pushes to, confirm which instance you are tuning. It must be a development copy, never production. If it is not clear, stop and ask.
4. Run `verdictloop baseline`. Note the noise band: a change smaller than the band is indistinguishable from luck.
5. `results.tsv` is the log and your memory. Leave it untracked.

## What you can and cannot do

- You CAN edit anything under `target/`: prompts, config, tool descriptions, code.
- You CANNOT edit `eval/` or `verdictloop.toml`. They define the score. If they change, `try` refuses to run. If a scenario feels unfair, improve the target, do not relax the test.
- Do not delete a rule a human wrote unless your evidence shows it causes the failure. Say so in the `-m` message.
- Do not add dependencies the target does not already have.

## The loop

LOOP until `try` prints `SESSION COMPLETE` or you are told to stop:

1. **Read.** `results.tsv` (what was tried, what worked) and `last_eval.md` (the tuning scenarios that did not fully pass: transcript, failed checks, events and traces).
2. **Diagnose before editing.** Pick a failing scenario and name the cause from its transcript or trace, not from a hunch. Typical causes: a wrong fact, a rule that exists but was ignored, context lost between turns, the wrong tool or no tool, answering when it should hand over (or the reverse), wrong tone or language, two rules that contradict each other, a slow step. If you cannot explain the failure from the evidence, read it again. Do not edit yet.
3. **One edit**, aimed at that cause, worded so it helps any similar conversation and not only this scenario. One idea per try; unrelated cleanups are their own tries.
4. **Try.** `verdictloop try -m "cause -> edit"`, for example `-m "T03 ignores the fine rule because it is buried at the end -> moved it to the top"`.
5. **Read the verdict.**
   - `VALIDATED`: better beyond the noise band and not worse on held-out. Kept, and it is the new baseline.
   - `NEUTRAL`: inside the band. Reverted. Not evidence either way.
   - `REFUTED`: worse, or better on tuning but worse on held-out, which means overfitting. Reverted.
   - `INFRA_FAIL`: the run itself broke (model, network, your command). Nothing was decided. Fix the cause if it is yours, then run `try` again.
6. If there is an `advisory` line, a group of scenarios got worse although the score rose. Treat the kept edit as suspect and repair that group next.
7. Repeat. Do not stop to ask whether to continue.

## Staying out of ruts

- Read `results.tsv` first every time. Do not repeat an idea that already failed, and do not make the same kind of edit a fifth time in a row.
- After three tries in a row that were not VALIDATED, make one deliberately different try: another kind of edit or another part of the target.
- Watch `size:`. If the target has grown by about a third, spend a try deleting or merging text; keeping the score with less text is a win.
- After every few kept edits run `verdictloop reseed`. A kept edit was picked because it scored well, partly by luck, and the bar it left behind may be too high.
- A good run looks like this: a few VALIDATED, more NEUTRAL, some REFUTED. REFUTED is information, not failure.

## Ways this goes wrong

- **Chasing noise.** Calling a change a win because the score moved less than the band. Do not second-guess the verdict.
- **Reward hacking.** Wanting to loosen a check, or to edit `eval/`. That is the failure mode, stop.
- **Cargo culting.** Applying generic prompt advice without evidence from this target's failures.
- **Diff bloat and prompt creep.** Big edits, and a target that only ever gets longer.
- **Tunnel vision.** Rewording the same sentence repeatedly while the real cause is elsewhere.

## When you stop

Run `verdictloop status` and report: the kept target (`.verdictloop/kept/`), tuning versus held-out score (a large gap means overfitting), the three tries that helped most (from `results.tsv`), and the failures that remain.

## Calibration (optional, once, before trusting the loop)

- Make a deliberately bad edit. `try` should say `REFUTED` and put it back.
- Add trailing blank lines to a file. `try` should say `NEUTRAL`.

If either is wrong, the eval is too noisy or too easy for this target. Say so instead of continuing.
