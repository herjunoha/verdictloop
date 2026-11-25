"""A prompt-only chatbot as the target: target/prompt.md is its system prompt, `claude -p` is its model.

    [target]
    files = ["target/"]
    command = ["python", "<path to>/claude_chatbot.py", "<model id>"]

Reads {"messages": [{"role","text"}...]} and writes {"reply","tokens"}. For a chatbot or agent that is not a
bare prompt (a deployed service, a framework, tools), write your own command with the same two JSON shapes:
call your agent with the conversation, and return its reply plus any "events" you want the eval to check
(a tool call, a handover). That command is the only thing that has to know how your agent works.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_cli import ask, serve  # noqa: E402

CLOSING = "Write the assistant's next message only."


def handle(req):
    model = sys.argv[1]
    system = (Path.cwd() / "target" / "prompt.md").read_text(encoding="utf-8")
    lines = ["Conversation so far:"] + [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}"
                                        for m in req["messages"]] + ["", CLOSING]
    res = ask(model, system, "\n".join(lines))
    return {"reply": res["text"], "tokens": res["tokens"]}


if __name__ == "__main__":
    serve(handle)
