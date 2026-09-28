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
