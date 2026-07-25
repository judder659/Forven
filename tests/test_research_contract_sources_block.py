from forven.research_contract import get_research_sources_block, default_research_settings


def test_defaults_contain_every_gated_source():
    block = default_research_settings()["research_sources"]
    assert set(block.keys()) == {"reddit", "blog", "podcast", "github", "forum"}
    # Enabled by default — matches youtube, which has no per-source gate.
    # Operators can still disable individual sources via the Research Settings UI.
    assert all(block[k]["enabled"] is True for k in block)


def test_get_research_sources_block_returns_dict_with_defaults():
    block = get_research_sources_block(raw_settings={})
    assert "reddit" in block and block["reddit"]["subs"]  # default subs list present
    assert "blog" in block and block["blog"]["feeds"]
    assert "github" in block and block["github"]["orgs"]
    assert "forum" in block and block["forum"]["sites"]


def test_user_override_merges_atop_defaults():
    override = {"research_settings": {"research_sources": {"reddit": {"enabled": True}}}}
    block = get_research_sources_block(raw_settings=override)
    # enabled flipped, but defaults for other fields preserved via deep merge
    assert block["reddit"]["enabled"] is True
    assert "algotrading" in block["reddit"]["subs"]  # default preserved
