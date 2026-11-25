"""Stand-in judge model for the offline demo: always says PASS. A real project points [judge] command at a
real model (see examples/claude_cli.py).

stdin:  {"model", "system", "user"}
stdout: {"text": str, "tokens": int}
"""
import json
import sys

req = json.load(sys.stdin)
text = json.dumps({"verdict": "PASS", "reason": "stand-in judge always passes"})
json.dump({"text": text, "tokens": (len(req["system"]) + len(req["user"]) + len(text)) // 4}, sys.stdout)
