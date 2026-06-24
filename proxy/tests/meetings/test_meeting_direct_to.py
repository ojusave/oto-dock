"""direct_to `agents` argument parsing — `meeting_orchestrator._parse_directed_agents`.

The orchestrator must tolerate every shape models emit for `direct_to(agents=…)`:
a real list, a JSON-encoded list, a bare agent name, a comma list, or junk.
A malformed value used to raise out of the turn's event loop into the
catch-all, marking the agent failed ("Agent disconnected from meeting") and —
when the moderator was the victim — stranding the whole meeting. The
meetings-mcp server has always auto-parsed these shapes; the orchestrator's
copy of the logic must match it.
"""

from services.meetings.meeting_orchestrator import _parse_directed_agents as parse


def test_list_passthrough():
    assert parse(["home-assistant", "test"]) == ["home-assistant", "test"]


def test_list_strips_whitespace_and_drops_empties():
    assert parse([" home-assistant ", ""]) == ["home-assistant"]


def test_json_encoded_list():
    assert parse('["home-assistant", "test"]') == ["home-assistant", "test"]


def test_bare_agent_name_string():
    # The live-bug shape: json.loads("home-assistant") raised
    # "Expecting value: line 1 column 1 (char 0)" and killed the turn.
    assert parse("home-assistant") == ["home-assistant"]


def test_comma_separated_names():
    assert parse("home-assistant, test") == ["home-assistant", "test"]


def test_json_encoded_scalar_string():
    assert parse('"home-assistant"') == ["home-assistant"]


def test_empty_inputs_return_none():
    assert parse("") is None
    assert parse("   ") is None
    assert parse([]) is None
    assert parse(None) is None


def test_non_string_non_list_returns_none():
    assert parse(123) is None
    assert parse({"agents": ["x"]}) is None


def test_never_raises_on_junk():
    for junk in ("{not json", "[unterminated", "null", "{}", "[]", ",,,"):
        parse(junk)  # must not raise; routing junk is filtered downstream
