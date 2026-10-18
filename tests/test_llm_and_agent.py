"""Tests for the LLM layer and the agent loop. Zero tokens, zero network.

    python tests/test_llm_and_agent.py      # needs FalkorDB on localhost:6379

Two halves:

* **Adapter translation** is tested against a stub client that returns a
  canned response object. This exercises every line of the Claude adapter
  except the HTTP call itself -- message conversion, tool schemas, stop-reason
  mapping, refusal handling -- with no API key.
* **The agent loop** is tested with FakeAdapter against a real FalkorDB graph,
  so tool dispatch and the memory writes are real; only the model is scripted.

What is NOT covered here: an actual request to Anthropic. That needs
credentials and costs money, and nothing below proves the wire format is
accepted by the live API.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from falkordb import FalkorDB  # noqa: E402

from agents import PLANNER_TOOLS, REVIEWER_TOOLS, Agent, Toolbox  # noqa: E402
from llm import FakeAdapter, calls, calls_many, get_adapter, says  # noqa: E402
from llm.base import LLMError  # noqa: E402
from llm.claude import (  # noqa: E402
    ClaudeAdapter,
    from_api_response,
    to_api_messages,
    to_api_tools,
)
from llm.types import (  # noqa: E402
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from memory import MemoryStore  # noqa: E402

TENANT = "test"
PASSED, FAILED = [], []


def test(fn):
    name = fn.__name__.replace("test_", "").replace("_", " ")
    try:
        fn()
    except AssertionError as e:
        FAILED.append((name, str(e) or "assertion failed"))
        print(f"[FAIL] {name}\n       {e}")
    except Exception as e:
        FAILED.append((name, f"{type(e).__name__}: {e}"))
        print(f"[ERROR] {name}\n       {type(e).__name__}: {e}")
    else:
        PASSED.append(name)
        print(f"[PASS] {name}")
    return fn


# ------------------------------------------------------- stubs for the adapter
class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class StubClient:
    """Stands in for anthropic.Anthropic. Records the request, returns a canned reply."""

    def __init__(self, response):
        self._response = response
        self.last_request = None

    @property
    def beta(self):
        return self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.last_request = kwargs
        return self._response


SPEC = ToolSpec(
    name="recall_related",
    description="look something up",
    parameters={"type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False},
)


# --------------------------------------------------------- adapter translation
@test
def test_tool_schema_is_strict_mode_safe():
    [out] = to_api_tools([SPEC])
    assert out["name"] == "recall_related"
    assert out["strict"] is True, out
    assert out["input_schema"]["additionalProperties"] is False, out
    assert out["input_schema"]["required"] == ["name"], out


@test
def test_tool_schema_fills_in_missing_strict_fields():
    loose = ToolSpec(name="t", description="d",
                     parameters={"properties": {"a": {"type": "string"}}})
    [out] = to_api_tools([loose])
    schema = out["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["a"], schema


@test
def test_user_text_message_converts():
    assert to_api_messages([Message(role="user", text="hi")]) == [
        {"role": "user", "content": "hi"}
    ]


@test
def test_assistant_tool_call_converts_to_tool_use_block():
    msg = Message(role="assistant", text="looking",
                  tool_calls=(ToolCall(id="c1", name="recall_related",
                                       arguments={"name": "Billing v2"}),))
    [out] = to_api_messages([msg])
    assert out["role"] == "assistant"
    kinds = [b["type"] for b in out["content"]]
    assert kinds == ["text", "tool_use"], out
    assert out["content"][1] == {"type": "tool_use", "id": "c1",
                                 "name": "recall_related",
                                 "input": {"name": "Billing v2"}}


@test
def test_assistant_turn_replays_provider_state_verbatim():
    # Reasoning blocks are bound to the conversation; rebuilding the turn from
    # text alone would drop them. provider_state must win.
    original = [{"type": "thinking", "thinking": ""},
                {"type": "text", "text": "hello"}]
    msg = Message(role="assistant", text="hello", provider_state=original)
    [out] = to_api_messages([msg])
    assert out["content"] is original, "provider_state must be replayed unchanged"


@test
def test_all_tool_results_go_back_in_one_user_message():
    # Splitting them trains the model out of parallel tool calls.
    msg = Message(role="user", tool_results=(
        ToolResult(call_id="a", content="1"),
        ToolResult(call_id="b", content="2"),
    ))
    out = to_api_messages([msg])
    assert len(out) == 1, f"expected one message, got {len(out)}"
    assert [b["tool_use_id"] for b in out[0]["content"]] == ["a", "b"]


@test
def test_is_error_flag_only_present_when_set():
    ok, bad = to_api_messages([
        Message(role="user", tool_results=(ToolResult(call_id="a", content="fine"),)),
        Message(role="user", tool_results=(
            ToolResult(call_id="b", content="boom", is_error=True),)),
    ])
    assert "is_error" not in ok["content"][0], ok
    assert bad["content"][0]["is_error"] is True, bad


@test
def test_response_parsing_skips_reasoning_blocks():
    resp = Resp([
        Block(type="thinking", thinking=""),          # empty by default
        Block(type="text", text="Here is the answer"),
        Block(type="tool_use", id="c9", name="explain_path", input={"a": 1}),
    ], stop_reason="tool_use")
    out = from_api_response(resp)
    assert out.text == "Here is the answer", out.text
    assert out.stop_reason == STOP_TOOL_USE
    assert out.tool_calls[0].name == "explain_path"
    assert out.tool_calls[0].arguments == {"a": 1}


@test
def test_unknown_stop_reason_passes_through_unchanged():
    # Must never be silently mapped onto end_turn.
    out = from_api_response(Resp([Block(type="text", text="x")],
                                 stop_reason="pause_turn"))
    assert out.stop_reason == "pause_turn", out.stop_reason


@test
def test_refusal_raises_instead_of_looking_like_an_empty_answer():
    client = StubClient(Resp([], stop_reason="refusal",
                             stop_details={"category": "cyber"}))
    adapter = ClaudeAdapter(client=client, model="claude-sonnet-5-5")
    try:
        adapter.chat([Message(role="user", text="hi")])
    except LLMError as e:
        assert "declined" in str(e), str(e)
        return
    raise AssertionError("a refusal must raise, not return empty text")


@test
def test_truncated_reply_raises():
    client = StubClient(Resp([Block(type="text", text="half an ans")],
                             stop_reason="max_tokens"))
    try:
        ClaudeAdapter(client=client).chat([Message(role="user", text="hi")])
    except LLMError as e:
        assert "truncated" in str(e), str(e)
        return
    raise AssertionError("a truncated reply must raise")


@test
def test_request_omits_thinking_and_forced_tool_choice():
    # Both would 400 on the Sonnet 5.5 / Opus 5.5 family.
    client = StubClient(Resp([Block(type="text", text="ok")]))
    ClaudeAdapter(client=client).chat([Message(role="user", text="hi")],
                                      tools=[SPEC], system="be brief")
    req = client.last_request
    assert "thinking" not in req, "thinking must be omitted, not disabled"
    assert "tool_choice" not in req, "forced tool_choice 400s on these models"
    assert req["system"] == "be brief"
    assert req["model"] == "claude-sonnet-5-5"


@test
def test_refusal_fallback_is_on_by_default_and_can_be_turned_off():
    on = StubClient(Resp([Block(type="text", text="ok")]))
    ClaudeAdapter(client=on).chat([Message(role="user", text="hi")])
    assert on.last_request["fallbacks"] == "default", on.last_request
    assert "server-side-fallback-2026-07-01" in on.last_request["betas"]

    off = StubClient(Resp([Block(type="text", text="ok")]))
    ClaudeAdapter(client=off, refusal_fallback=False).chat(
        [Message(role="user", text="hi")])
    assert "fallbacks" not in off.last_request


# ------------------------------------------------------------------- factory
@test
def test_factory_selects_provider_and_rejects_unknown():
    assert isinstance(get_adapter("fake", script=[says("hi")]), FakeAdapter)
    try:
        get_adapter("gpt-9000")
    except ValueError as e:
        assert "unknown LLM provider" in str(e)
        return
    raise AssertionError("expected ValueError for an unknown provider")


@test
def test_fake_adapter_raises_when_the_loop_overruns_the_script():
    fake = FakeAdapter([says("one")])
    fake.chat([Message(role="user", text="a")])
    try:
        fake.chat([Message(role="user", text="b")])
    except AssertionError as e:
        assert "exhausted" in str(e)
        return
    raise AssertionError("an exhausted script must fail loudly")


# -------------------------------------------------------------- the agent loop
def fresh_store():
    world = json.loads((ROOT / "data" / "world_acme.json").read_text(encoding="utf-8"))
    world["graph"] = f"tenant_{TENANT}"
    import importlib.util
    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    seed.seed_tenant(FalkorDB(host="localhost", port=6379), world,
                     ROOT / "data" / "world_acme.json")
    return MemoryStore(TENANT)


print("seeding fixture graph tenant_test ...")
STORE = fresh_store()
SESSION = STORE.start_session(user="demo-user", session_id="sess-agent-1")
print()


def planner(script, **kw):
    box = Toolbox(STORE, "Planner", SESSION, allowed=PLANNER_TOOLS)
    return Agent("Planner", box, FakeAdapter(script), prompt="test prompt", **kw)


def reviewer(script, **kw):
    box = Toolbox(STORE, "Reviewer", SESSION, allowed=REVIEWER_TOOLS)
    return Agent("Reviewer", box, FakeAdapter(script), prompt="test prompt", **kw)


@test
def test_both_sides_of_the_exchange_are_recorded():
    """A conversation has to be readable back out of the graph.

    Recording only the user's half would make a transcript impossible to
    reconstruct, which is what the UI relies on instead of browser state.
    """
    before = len(STORE.recall_recent(limit=200))
    agent = planner([says("Nothing to do.")])
    turn = agent.run("just saying hello")
    assert turn.text == "Nothing to do."
    assert turn.steps == []

    # Both halves are written, but only the person's half is *recalled*: an
    # agent's own prose is output, not evidence, and feeding it back let a
    # retired fact be repeated out of an old reply. The transcript below is
    # what proves both were stored.
    after = STORE.recall_recent(limit=200)
    assert len(after) == before + 1, "the person's message belongs in recall"
    assert after[0]["summary"] == "just saying hello"
    assert not [a for a in after if a["summary"] == "Nothing to do."], \
        "the agent is recalling its own reply"

    # And the transcript reads back in conversational order, with speakers.
    said = STORE.session_transcript(SESSION)
    assert [s["speaker"] for s in said[-2:]] == ["user", "Planner"], said[-2:]
    assert [s["text"] for s in said[-2:]] == ["just saying hello", "Nothing to do."]


@test
def test_tool_call_is_dispatched_and_its_result_fed_back():
    agent = planner([
        calls("current_decisions", {"project": "Spring Launch"}),
        says("The launch is set for April 1."),
    ])
    turn = agent.run("when does the launch ship?")
    assert turn.tools_used == ["current_decisions"], turn.tools_used
    assert "Ship Spring Launch" in turn.steps[0].result, turn.steps[0].result
    # The result must actually reach the model on the next call.
    assert any("Ship Spring Launch" in r for r in agent.llm.tool_results_seen())


@test
def test_parallel_tool_calls_return_in_a_single_user_message():
    agent = reviewer([
        calls_many(("current_decisions", {"project": "Spring Launch"}),
                   ("depends_on_chain", {"project": "Billing v2"})),
        says("Blocked."),
    ])
    turn = agent.run("can we ship billing?")
    assert len(turn.steps) == 2, turn.tools_used
    final_messages = agent.llm.exchanges[-1].messages
    result_msgs = [m for m in final_messages if m.tool_results]
    assert len(result_msgs) == 1, "results must be batched into one message"
    assert len(result_msgs[0].tool_results) == 2


@test
def test_tool_failure_comes_back_as_an_error_result_not_an_exception():
    agent = reviewer([
        calls("explain_path", {"from_label": "Person", "from_name": "Ghost",
                               "to_label": "Project", "to_name": "Nowhere"}),
        says("I could not connect those."),
    ])
    turn = agent.run("how are they linked?")
    assert len(turn.steps) == 1
    # Unknown entities yield no path -> null, not a crash.
    assert turn.steps[0].result == "null", turn.steps[0].result


@test
def test_bad_arguments_produce_an_error_result():
    agent = reviewer([
        calls("explain_path", {"from_label": "Person"}),   # missing args
        says("Sorry."),
    ])
    turn = agent.run("explain")
    assert turn.steps[0].is_error, turn.steps[0]
    assert "bad arguments" in turn.steps[0].result, turn.steps[0].result


@test
def test_tool_outside_the_role_is_refused():
    # record_decision is a Planner tool; the Reviewer must not reach it.
    agent = reviewer([
        calls("record_decision", {"title": "x", "reason": "y"}),
        says("Not mine to make."),
    ])
    turn = agent.run("decide something")
    assert turn.steps[0].is_error, turn.steps[0]
    assert "no tool named" in turn.steps[0].result, turn.steps[0].result
    assert "record_decision" not in Toolbox(
        STORE, "Reviewer", SESSION, allowed=REVIEWER_TOOLS).names


@test
def test_tool_call_budget_stops_the_loop_and_still_answers():
    agent = planner(
        [calls("recall_recent", {"limit": 1})] * 3 + [says("Enough.")],
        max_tool_calls=2,
    )
    turn = agent.run("keep digging")
    assert turn.stopped_early, "should have hit the cap"
    assert len(turn.steps) == 2, turn.tools_used
    assert turn.text == "Enough."


@test
def test_every_tool_call_gets_a_result_even_when_budget_is_gone():
    # A tool_use block without a matching tool_result makes the next request
    # invalid, so the refusal must still be returned per call.
    agent = planner(
        [calls_many(("recall_recent", {}), ("recall_recent", {})),
         calls_many(("recall_recent", {}), ("recall_recent", {})),
         says("Done.")],
        max_tool_calls=2,
    )
    turn = agent.run("dig")
    last_results = [m for m in agent.llm.exchanges[-1].messages if m.tool_results][-1]
    assert len(last_results.tool_results) == 2, last_results
    assert all(r.is_error for r in last_results.tool_results)
    assert turn.stopped_early


@test
def test_agent_and_session_are_bound_not_model_supplied():
    # The model never chooses who it acts as or where it writes: the schemas
    # do not expose `agent` or `session_id`, so provenance is a property of
    # the wiring rather than of the model's good behaviour.
    #
    # `read_conversation` is the one tool that names a session, and only to
    # read one. It cannot write, and the store it reads through is bound to
    # a single tenant's graph, so the most it can reach is another
    # conversation in the same workspace -- which is the whole point of it.
    reads_a_named_conversation = {"read_conversation"}
    box = Toolbox(STORE, "Planner", SESSION, allowed=PLANNER_TOOLS)
    for spec in box.specs():
        if spec.name in reads_a_named_conversation:
            continue
        props = set(spec.parameters["properties"])
        assert "agent" not in props and "session_id" not in props, spec.name

    agent = planner([
        calls("record_decision", {"title": "Freeze the API",
                                  "reason": "too many breaking changes",
                                  "affects": ["Mobile App"]}),
        says("Recorded."),
    ])
    agent.run("freeze the API")
    live = STORE.current_decisions(project="Mobile App")
    assert any(d["title"] == "Freeze the API" for d in live), live
    # The event carries the bound agent, not anything the model chose.
    recent = STORE.recall_recent(limit=5)
    assert any(e["agent"] == "Planner" and "Freeze the API" in e["summary"]
               for e in recent), recent


@test
def test_system_prompt_carries_runtime_facts():
    agent = planner([says("ok")])
    agent.run("hi")
    system = agent.llm.exchanges[-1].system
    assert "'Planner'" in system
    assert TENANT in system
    assert "test prompt" in system


@test
def test_provider_state_is_carried_on_assistant_turns():
    # The loop must hand the provider's original turn back untouched, so an
    # adapter can replay reasoning blocks byte-identically.
    first = LLMResponse(
        text="",
        tool_calls=(ToolCall(id="c1", name="recall_recent", arguments={"limit": 1}),),
        stop_reason=STOP_TOOL_USE,
        raw=["ORIGINAL-BLOCKS-1"],
    )
    final = LLMResponse(text="done", stop_reason=STOP_END_TURN,
                        raw=["ORIGINAL-BLOCKS-2"])
    agent = planner([first, final])
    turn = agent.run("what happened")
    # Only the turns this run produced. `turn.replayed` counts the leading
    # messages that are this conversation read back from the graph; those
    # are prior turns and carry no provider_state by construction, because
    # the provider only needs reasoning blocks preserved within the turn
    # that produced them.
    own = turn.messages[turn.replayed:]
    carried = [m.provider_state for m in own if m.role == "assistant"]
    assert carried == [["ORIGINAL-BLOCKS-1"], ["ORIGINAL-BLOCKS-2"]], carried
    assert all(m.provider_state is None
               for m in turn.messages[:turn.replayed]), "replayed history"



@test
def test_end_to_end_supersede_through_the_loop_changes_the_graph():
    old = [d for d in STORE.current_decisions(project="Spring Launch")
           if d["title"].startswith("Ship Spring Launch")]
    assert old, "fixture should start with the April decision live"

    agent = planner([
        calls("current_decisions", {"project": "Spring Launch"}),
        calls("record_decision", {
            "title": "Delay Spring Launch to May 15",
            "reason": "Ledger Migration is blocked",
            "affects": ["Spring Launch"],
            "supersedes": old[0]["id"],
        }),
        says("Recorded the delay and retired the April date."),
    ])
    turn = agent.run("we need to delay the launch to May 15")
    assert turn.tools_used == ["current_decisions", "record_decision"]

    live = {d["title"] for d in STORE.current_decisions(project="Spring Launch")}
    assert "Delay Spring Launch to May 15" in live, live
    assert "Ship Spring Launch on April 1, 2027" not in live, \
        f"the old decision should have been retired: {live}"


# ------------------------------------------------------------------- report
if __name__ == "__main__":
    try:
        FalkorDB(host="localhost", port=6379).select_graph(f"tenant_{TENANT}").delete()
    except Exception:
        pass
    print("\n" + "=" * 62)
    print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} tests passed")
    for name, why in FAILED:
        print(f"  FAILED: {name} -- {why}")
    sys.exit(1 if FAILED else 0)
