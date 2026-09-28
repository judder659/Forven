"""AI drafts are shaped to what the visual builder shows (one level of groups)."""
from forven.strategies.builtin.rule_engine import validate_rule_spec
from forven.strategies.nl_spec_gen import _normalize_spec


def c(left, op=">", right=0):
    return {"left": left, "op": op, "right": right}


def test_a_bare_list_side_becomes_a_group_with_the_sides_usual_logic():
    spec = _normalize_spec({"entry_long": [c("close")], "exit_long": [c("open"), c("high")]})
    assert spec["entry_long"] == {"logic": "and", "conditions": [c("close")]}
    assert spec["exit_long"] == {"logic": "or", "conditions": [c("open"), c("high")]}
    assert spec["entry_short"] is None and spec["exit_short"] is None


def test_nesting_that_keeps_its_meaning_flat_is_lifted_into_the_parent():
    side = {"logic": "and", "conditions": [
        c("close"),
        {"logic": "and", "conditions": [c("open"), {"logic": "or", "conditions": [c("low")]}]},
        {"logic": "or", "conditions": []},
    ]}
    assert _normalize_spec({"entry_long": side})["entry_long"] == {
        "logic": "and", "conditions": [c("close"), c("open"), c("low")],
    }


def test_mixed_logic_keeps_one_group_level():
    side = {"logic": "and", "conditions": [c("close"), {"logic": "or", "conditions": [
        c("open"), {"logic": "or", "conditions": [c("high"), c("low")]},
    ]}]}
    spec = _normalize_spec({"entry_long": side})
    assert spec["entry_long"] == {"logic": "and", "conditions": [
        c("close"), {"logic": "or", "conditions": [c("open"), c("high"), c("low")]},
    ]}
    assert validate_rule_spec(spec) == []


def _ready(monkeypatch, reply):
    import json as _json

    from forven import ai

    calls = []
    monkeypatch.setattr("forven.strategies.idea_readiness.check_idea_readiness",
                        lambda *a, **k: {"can_generate": True, "issues": [], "warnings": ["checked"]})
    monkeypatch.setattr(ai, "resolve_available_provider", lambda: "stub")
    monkeypatch.setattr(ai, "_provider_has_credentials", lambda provider: True)

    async def call_ai(provider, *, prompt, system, max_tokens, temperature):
        calls.append(prompt)
        return _json.dumps(reply)

    monkeypatch.setattr(ai, "call_ai", call_ai)
    return calls


CURRENT = {"indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}], "params": {"lo": 30},
           "entry_long": {"logic": "and", "conditions": [c("rsi", "<", {"param": "lo"})]},
           "exit_long": None, "entry_short": None, "exit_short": None}


def test_edit_sends_the_current_spec_and_returns_the_whole_updated_one(monkeypatch):
    import asyncio

    from forven.strategies.nl_spec_gen import nl_edit_rule_spec

    updated = {**CURRENT, "indicators": CURRENT["indicators"] + [{"id": "ema200", "kind": "ema", "params": {"length": 200}}],
               "entry_long": {"logic": "and", "conditions": [c("rsi", "<", {"param": "lo"}), c("close", ">", "ema200")]}}
    calls = _ready(monkeypatch, updated)
    result = asyncio.run(nl_edit_rule_spec(instruction="add a 200 EMA trend filter", spec=CURRENT, symbol="BTC/USDT"))
    assert result["valid"] and result["spec"]["entry_long"]["conditions"][1] == c("close", ">", "ema200")
    assert '"lo": 30' in calls[0] and "add a 200 EMA trend filter" in calls[0]


def test_edit_needs_an_instruction_and_a_spec(monkeypatch):
    import asyncio

    from forven.strategies.nl_spec_gen import nl_edit_rule_spec

    calls = _ready(monkeypatch, CURRENT)
    assert "Describe the change" in asyncio.run(nl_edit_rule_spec(instruction=" ", spec=CURRENT))["errors"][0]
    assert "no strategy" in asyncio.run(nl_edit_rule_spec(instruction="tighten", spec=None))["errors"][0]
    assert calls == []
