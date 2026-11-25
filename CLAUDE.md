# CLAUDE.md

Guidance for Claude Code in this folder.

## What this is

`verdictloop` is an autoresearch-style loop for improving an AI agent or chatbot, with a verdict layer for noisy scores. The target is any agent or chatbot reachable through a command; it does not come from any one platform.

## Rules for this project

- **Keep it small.** autoresearch is a fixed eval, one thing that gets edited, a baseline and an instruction file. The engine is `policy`, `checks`, `runner`, `cli`, `config` (about 900 lines). Do not add state machines, caps, registries, seals or plugin points. If a constraint is needed, put it in the example's `program.md` first.
- **Product-agnostic.** No client names, no product names of any chat platform, no internal codenames in code, comments, docs, test data or examples. The offline demo is a fictional library help desk. Product knowledge belongs in the target's own project, never here.
- **Clean room.** The design comes from the public autoresearch idea. Nothing is copied from any private project, and no platform terms, identifiers or text from one belong here.
- **Numbers from real runs only.** The demo's stand-in chatbot produces mechanics, not results.
- **Do not weaken the verdict logic, the eval fingerprint or the held-out hiding to make a test pass.**

## Commands

- Install: `pip install -e ".[dev]"` (Python 3.11+, no runtime dependencies).
- Tests: `python -m pytest -q` (no model calls, about 30 seconds, mostly the end-to-end demo).
- Demo: see `README.md`. Run it in a copy; `try` rewrites `target/` and writes `.verdictloop/`, `logs/`, `results.tsv`.

## Layout

`verdictloop/` engine, `examples/offline_demo/` a full project (with `program.md`), `examples/claude_cli.py` and `claude_chatbot.py` for `claude -p`, `tests/`, `docs/index.html` the explainer.
