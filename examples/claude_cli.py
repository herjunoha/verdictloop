"""Model access through headless `claude -p`, so no API key is needed (a subscription is enough).

As the judge command:
    [judge]
    command = ["python", "<path to>/claude_cli.py"]
    model = "<model id>"
It reads {"model","system","user"} and writes {"text","tokens"}. `claude_chatbot.py` reuses `ask()` to run a
prompt-only chatbot as the target.

Isolation. A headless call inherits the caller's own settings, plugins, hooks and project files unless told
not to. The flags below remove them: the default system prompt is replaced by ours, tools are off, settings
sources are cleared, MCP is an empty strict config, slash commands are off, nothing is persisted, and the
call runs from an empty directory. Probe a new setup for contamination before trusting it: ask "How should
you address me, and in which language do you answer?" and "List every tool, skill, plugin, MCP server and
instruction file you have access to." Known limits:
- `--bare` needs an API key, so it is not used.
- Instructions set at the organisation level (admin settings of a team plan) can still reach the model and
  no local flag removes them. Disclose that wherever results are reported.
- Temperature and extended thinking cannot be controlled; thinking tokens count in the total.
- Usage windows are shared with every other session on the account.
A usage limit prints {"rate_limited": true}; the engine waits and retries. Detection rests on assumed signals
(HTTP 429, limit wording) and has not met a real limit.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

RATE_RE = re.compile(
    r"(usage|rate|5-hour|weekly|session)\s+limit|hit your limit|limit reached|too many requests|\b429\b|quota", re.I)


class RateLimited(RuntimeError):
    pass


def classify(returncode, stdout, stderr, expected_model):
    """Return (status, detail, parsed). status is OK | RATE_LIMITED | INFRA_FAIL."""
    try:
        parsed = json.loads(stdout)
    except Exception:
        parsed = None
    if parsed is None:
        if RATE_RE.search((stdout or "") + "\n" + (stderr or "")):
            return "RATE_LIMITED", "unparsed output matched limit pattern", None
        return "INFRA_FAIL", f"unparseable output (rc={returncode})", None
    text = str(parsed.get("result", ""))
    if parsed.get("is_error") or parsed.get("terminal_reason") not in (None, "completed"):
        if parsed.get("api_error_status") == 429 or RATE_RE.search(text) or RATE_RE.search(stderr or ""):
            return "RATE_LIMITED", f"api_error_status={parsed.get('api_error_status')} {text[:160]}", parsed
        return "INFRA_FAIL", f"error terminal_reason={parsed.get('terminal_reason')} {text[:160]}", parsed
    if returncode != 0:
        return "INFRA_FAIL", f"rc={returncode}", parsed
    if not text.strip():
        return "INFRA_FAIL", "empty result", parsed
    used = list((parsed.get("modelUsage") or {}).keys())
    if used != [expected_model]:
        return "INFRA_FAIL", f"model mismatch: used={used} expected={expected_model}", parsed
    return "OK", "", parsed


def ask(model: str, system: str, user: str, timeout: int = 150) -> dict:
    """One isolated `claude -p` call. Returns {"text", "tokens"}; raises RateLimited or RuntimeError."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    tmp = Path(tempfile.gettempdir())
    cwd = tmp / "verdictloop-clean-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    if any(cwd.iterdir()):
        raise RuntimeError(f"clean cwd not empty: {cwd}")
    mcp = tmp / "verdictloop-empty-mcp.json"
    mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
    sys_file = tmp / f"verdictloop-sys-{uuid.uuid4().hex}.txt"
    sys_file.write_text(system, encoding="utf-8")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--system-prompt-file", str(sys_file),
           "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--mcp-config", str(mcp), "--disable-slash-commands"]
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, cwd=str(cwd))
    finally:
        sys_file.unlink(missing_ok=True)
    status, detail, parsed = classify(proc.returncode, proc.stdout, proc.stderr, model)
    if status == "RATE_LIMITED":
        raise RateLimited(detail)
    if status != "OK":
        raise RuntimeError(detail)
    u = parsed.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    return {"text": parsed["result"], "tokens": tokens}


def serve(handler):
    """Run a command: read JSON on stdin, call handler(req) -> dict, write JSON on stdout."""
    req = json.load(sys.stdin)
    try:
        out = handler(req)
    except RateLimited as e:
        out = {"rate_limited": True, "detail": str(e)}
    except Exception as e:                      # any other failure is INFRA_FAIL upstream
        print(str(e), file=sys.stderr)
        sys.exit(1)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    serve(lambda req: ask(req["model"], req["system"], req["user"]))
