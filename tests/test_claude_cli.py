"""Classification inside examples/claude_cli.py against fixtures. No claude process is run."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "claude_cli", Path(__file__).resolve().parent.parent / "examples" / "claude_cli.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

MODEL = "model-x"


def ok_json(**over):
    d = {"is_error": False, "terminal_reason": "completed", "result": "hello",
         "modelUsage": {MODEL: {}}, "usage": {"input_tokens": 1, "output_tokens": 2}}
    d.update(over)
    return json.dumps(d)


def test_ok():
    assert cli.classify(0, ok_json(), "", MODEL)[0] == "OK"


def test_model_mismatch_is_infra_fail():
    st, detail, _ = cli.classify(0, ok_json(modelUsage={"other": {}}), "", MODEL)
    assert st == "INFRA_FAIL" and "mismatch" in detail


def test_429_and_limit_wording_are_rate_limited():
    out = ok_json(is_error=True, terminal_reason="api_error", api_error_status=429, result="Too many requests")
    assert cli.classify(1, out, "", MODEL)[0] == "RATE_LIMITED"
    out = ok_json(is_error=True, terminal_reason="api_error", result="You've hit your usage limit")
    assert cli.classify(1, out, "", MODEL)[0] == "RATE_LIMITED"


def test_other_errors_and_empty_results_are_infra_fail():
    out = ok_json(is_error=True, terminal_reason="api_error", api_error_status=500, result="boom")
    assert cli.classify(1, out, "", MODEL)[0] == "INFRA_FAIL"
    assert cli.classify(0, ok_json(result="  "), "", MODEL)[0] == "INFRA_FAIL"


def test_unparseable_output_depends_on_limit_wording():
    assert cli.classify(1, "not json", "weekly limit reached", MODEL)[0] == "RATE_LIMITED"
    assert cli.classify(1, "not json", "segfault", MODEL)[0] == "INFRA_FAIL"
