"""Deterministic stand-in for a deployed chatbot, used only by this offline demo.

Reads target/prompt.md and "behaves" according to it, so editing the prompt changes the scores the way
editing a real prompt would, with no noise. It reports a tool call or a handover as an event, which is
how a real agent would expose them. A real project points [target] command at its own agent instead.

stdin:  {"conversation_id", "scenario", "turn", "context", "messages": [{"role", "text"}, ...]}
stdout: {"reply": str, "events": [str], "metrics": {name: number}, "trace": str, "tokens": int}
        (only "reply" is required)
"""
import json
import sys
from pathlib import Path


def prompt_line(prompt: str, prefix: str):
    for line in prompt.splitlines():
        if line.strip().lower().startswith(prefix.lower()):
            return line.strip()
    return None


def answer(prompt: str, question: str):
    """Returns (reply, events, trace)."""
    q = question.lower()
    if "IGNORE_POLICY" in prompt:
        return "Anything you like!", [], "rule: none, the prompt says to ignore policy"
    if "owe" in q or "balance" in q:
        if "use the fines tool" in prompt.lower():
            return "You owe 0.75.", ["tool:fines"], "intent: balance -> called fines tool"
        return "I cannot check balances.", [], "intent: balance -> no tool described in the prompt"
    if "lost" in q:
        if "hand over lost cards" in prompt.lower():
            return "I am passing you to a person at the front desk.", ["handover"], "intent: lost card -> handover rule"
        return "Please keep your card safe.", [], "intent: lost card -> no handover rule in the prompt"
    if "late" in q or "fee" in q or "fine" in q:
        line = prompt_line(prompt, "Fine:")
        return line or "I do not know.", [], f"matched prompt line: {line}"
    if "book" in q or "loan" in q or "borrow" in q or "week" in q:
        line = prompt_line(prompt, "Loans last")
        return line or "I do not know.", [], f"matched prompt line: {line}"
    return "We are open 09:00 to 17:00.", [], "default answer: opening hours"


def main():
    req = json.load(sys.stdin)
    prompt = (Path(__file__).parent / "target" / "prompt.md").read_text(encoding="utf-8")
    users = [m["text"] for m in req["messages"] if m["role"] == "user"]
    reply, events, trace = answer(prompt, users[-1])
    json.dump({"reply": reply, "events": events, "metrics": {"confidence": 0.9}, "trace": trace,
               "tokens": (len(prompt) + len(users[-1]) + len(reply)) // 4}, sys.stdout)


if __name__ == "__main__":
    main()
