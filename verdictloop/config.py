"""Project layout and config. Convention over configuration:

  verdictloop.toml      the only config file
  program.md            instructions for the agent that improves the target (you steer it by editing this)
  target/               what gets improved (the files named in [target] files)
  eval/                 fixed: tuning.json, heldout.json, patterns.json, judge.md, facts.md
  results.tsv           one line per baseline or try
  .verdictloop/         state, the kept copy of the target, discarded attempts
  logs/runs.jsonl       every scenario run with its transcript
"""
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import List

CONFIG_NAME = "verdictloop.toml"


class ConfigError(ValueError):
    pass


@dataclass
class Project:
    root: Path
    target_files: List[Path]
    target_command: List[str]
    setup_command: List[str]
    judge_command: List[str]
    judge_model: str
    repeats: int
    workers: int
    max_tokens: int
    timeout: int

    @property
    def eval_dir(self) -> Path:
        return self.root / "eval"

    @property
    def state_dir(self) -> Path:
        return self.root / ".verdictloop"

    @property
    def runs_log(self) -> Path:
        return self.root / "logs" / "runs.jsonl"

    @property
    def results(self) -> Path:
        return self.root / "results.tsv"


def load_project(path) -> Project:
    p = Path(path)
    cfg_path = p / CONFIG_NAME if p.is_dir() else p
    if not cfg_path.exists():
        raise ConfigError(f"no {CONFIG_NAME} in {p}")
    root = cfg_path.parent.resolve()
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    t, j, r = raw.get("target", {}), raw.get("judge", {}), raw.get("run", {})
    if not t.get("command"):
        raise ConfigError("[target] needs command: the executable that answers one turn")
    if not t.get("files"):
        raise ConfigError("[target] needs files: the files or folders the agent may edit")
    files = [root / f for f in t["files"]]
    for f in files:
        if not f.exists():
            raise ConfigError(f"target file not found: {f}")
    proj = Project(root=root, target_files=files, target_command=list(t["command"]),
                   setup_command=list(t.get("setup", [])), judge_command=list(j.get("command", [])),
                   judge_model=str(j.get("model", "judge")), repeats=int(r.get("repeats", 3)),
                   workers=int(r.get("workers", 2)), max_tokens=int(r.get("max_tokens", 0)),
                   timeout=int(r.get("timeout", 150)))
    if proj.repeats < 3:
        raise ConfigError("[run] repeats must be at least 3 (the noise band needs three baseline passes)")
    if proj.workers < 1:
        raise ConfigError("[run] workers must be at least 1")
    return proj


def load_scenarios(proj: Project) -> List[dict]:
    """eval/tuning.json and eval/heldout.json, each a list of scenarios. Returns them with "set" filled in."""
    out, seen, needs_judge = [], set(), False
    for name in ("tuning", "heldout"):
        f = proj.eval_dir / f"{name}.json"
        if not f.exists():
            raise ConfigError(f"missing {f}")
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data["scenarios"] if isinstance(data, dict) else data
        if not items:
            raise ConfigError(f"{f.name} has no scenarios")
        for s in items:
            sid = s.get("id")
            if not sid or sid in seen:
                raise ConfigError(f"scenario id missing or duplicated: {sid!r}")
            seen.add(sid)
            if not s.get("turns"):
                raise ConfigError(f"{sid}: needs at least one turn")
            if not s.get("checks") and not s.get("judge"):
                raise ConfigError(f"{sid}: needs at least one check or a judge")
            needs_judge = needs_judge or bool(s.get("judge"))
            out.append({**s, "set": name})
    if needs_judge and (not proj.judge_command or not (proj.eval_dir / "judge.md").exists()):
        raise ConfigError("a scenario uses a judge: set [judge] command and write eval/judge.md")
    return out


def load_patterns(proj: Project) -> dict:
    f = proj.eval_dir / "patterns.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
