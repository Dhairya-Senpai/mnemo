"""The agent loop: ask the model, run the tools it asks for, repeat.

The loop is ours rather than the SDK's tool runner, on purpose. The runner
would drive this cycle for us, but the cycle would then live inside a vendor
helper and swapping providers would mean rewriting the agent. Here the loop
talks only to `LLMAdapter`, so a different provider is a config change.

Every turn is also recorded in the graph. An agent that thinks and acts
without leaving an episodic trace would break the central claim of the
project, so `Agent.run` writes the user's message as an Event before calling
the model, and the tools write their own events as they go.

Reads are recorded too. Each one becomes a Lookup node, and the answer the
agent finally gives points at the lookups behind it, so the reasoning can be
read back out of the graph later -- by a human, or by the agent itself.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMAdapter
from llm.types import STOP_TOOL_USE, Message, ToolCall, ToolResult

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Token discipline (PROJECT_CONTEXT.md section 9): cap tool calls per question.
# The cap is a backstop against a loop that never converges, not a target.
DEFAULT_MAX_TOOL_CALLS = 6

# How many times one tool may be called in a single turn. The repeat cache
# below catches identical calls; this catches the other shape of the same
# failure, where a model walks a tool across varying arguments and never
# stops. Asked to resume an earlier conversation, one read the conversation
# correctly and then called what_is_on on eight different dates, spending the
# whole budget after it already had its answer.
MAX_CALLS_PER_TOOL = 4


def load_prompt(name: str) -> str:
    """Prompts live in plain text files, with no provider-specific syntax."""
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"no prompt file at {path}")
    return path.read_text(encoding="utf-8").strip()


@dataclass
class Step:
    """One tool call and its result, kept for the UI's memory-changes panel."""
    name: str
    arguments: dict
    result: str
    is_error: bool


@dataclass
class Turn:
    """What one `Agent.run` produced."""
    text: str
    steps: list[Step] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    stopped_early: bool = False       # hit the tool-call cap
    lookups: list[str] = field(default_factory=list)   # Lookup ids behind it
    curation: object = None           # what the curator wrote, or None
    # How many leading messages are this conversation replayed from the
    # graph rather than produced by this turn. Replayed assistant turns
    # carry no provider_state by construction -- they are prior turns, and
    # the provider only requires reasoning blocks to survive within the
    # turn that produced them.
    replayed: int = 0

    @property
    def tools_used(self) -> list[str]:
        return [s.name for s in self.steps]


class Agent:
    """One named agent with a role prompt, an adapter, and a toolbox."""

    def __init__(self, name, toolbox, llm: LLMAdapter, prompt=None,
                 prompt_name=None, max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
                 standing_rules=False, topic_index=False, curator=None,
                 roster=False):
        if prompt is None and prompt_name is None:
            raise ValueError("pass prompt or prompt_name")
        self.name = name
        self.toolbox = toolbox
        self.llm = llm
        self.prompt = prompt if prompt is not None else load_prompt(prompt_name)
        self.max_tool_calls = max_tool_calls
        # Off by default, and deliberately. An agent that is handed its rules
        # has not recalled them, and the Reviewer exists to show that an
        # agent can work from the graph alone -- find_prompt_leaks checks
        # exactly that, and would count an injected rule as having been told.
        # The Planner talks to a person in free-form chat, where a small model
        # will not think to look; the Reviewer gets one fixed trigger and must
        # read everything, including its rules.
        self.standing_rules = standing_rules
        # The index of past subjects, same reasoning as above: a model cannot
        # ask for a conversation whose existence it does not know about.
        self.topic_index = topic_index
        # Who else exists. Same reasoning a third time: an agent cannot hand
        # work to a colleague it has never heard of.
        self.roster = roster
        # An adapter for the curation pass, or None to leave the graph
        # unchanged by this agent's turns. The Planner curates; the Reviewer
        # does not, because its job is to act on what is there rather than to
        # learn from a person.
        self.curator = curator

    def system_prompt(self) -> str:
        """Role prompt plus the facts only the runtime knows."""
        store = self.toolbox.store
        user = store.session_user(self.toolbox.session_id)
        # Naming the person matters more than it looks: without a referent for
        # "I" the agent invents a subject when recording a personal fact, and
        # has been observed filing the user's own appointment under its own
        # name.
        about_user = (
            f"You are talking to {user!r}. When they say \"I\", \"me\" or "
            f"\"my\", that means {user!r} - use that name as the subject when "
            f"you record something about them.\n" if user else "")
        return (
            f"{self.prompt}\n\n"
            f"You are the agent named {self.name!r}. "
            f"You are working in session {self.toolbox.session_id!r} "
            f"for tenant {store.tenant!r}.\n"
            f"{about_user}"
            f"Today's date is {store.now()[:10]}.\n"
            f"{self._standing_rules()}"
            f"{self._known_facts()}"
            f"{self._roster()}"
            f"{self._topic_index()}"
        )

    # How many facts about the person to carry. Small on purpose: this is
    # "what you already know about them", not their file.
    MAX_KNOWN_FACTS = 12

    def _known_facts(self) -> str:
        """What is currently true about the person, read from the graph.

        The third fix of this shape, and the reason is the same each time.
        Asked "how many shoes do i own?" in a new chat, a live agent called
        **no tools at all** and answered "I don't have any record of your
        shoe collection" -- while `you HAS 3 pairs of Nike shoes` sat in
        the graph, written correctly by the curator two chats earlier.

        It did not look. Prompting it to look is what failed three times
        with standing rules (docs/TESTING.md 3b), and the fix that held
        there was to stop depending on the model electing to search. Facts
        about the person are few, always relevant, and cheap to carry, so
        they are carried.

        This does not replace the recall tools. Anything about a project, a
        date or another person still has to be looked up; this is only the
        handful of things true of whoever is talking.
        """
        if not self.standing_rules:
            return ""
        store = self.toolbox.store
        try:
            user = store.session_user(self.toolbox.session_id)
            known = store.recall_entity(user) if user else None
        except Exception:
            return ""                      # never break a turn over this
        if not known:
            return ""
        lines = [f"- {user} {f['predicate'].lower().replace('_', ' ')} "
                 f"{f['name']}" + (f" (on {f['when']})" if f.get("when") else "")
                 for f in known.get("facts_out", [])][:self.MAX_KNOWN_FACTS]
        if not lines:
            return ""
        return (
            f"\nWhat you already know about {user!r}, from the graph. These "
            f"are current;\nanswer from them rather than saying you have no "
            f"record. Do not recite them\nunasked -- raise one only when it "
            f"bears on what was said:\n" + "\n".join(lines) + "\n")

    def _roster(self) -> str:
        """Who else can be handed work, read from the graph.

        Routing used to be a constant: the Planner knew the word "Reviewer"
        because its prompt said so, and a new agent meant editing a prompt
        and shipping code. Reading the roster instead means an `Agent` node
        with a role and a capability is routed to without anything being
        changed -- the list the model sees is the list that exists.
        """
        if not self.roster:
            return ""
        try:
            agents = self.toolbox.store.agent_roster(exclude=(self.name,))
        except Exception:
            return ""                      # never break a turn over this
        if not agents:
            return ""
        lines = "\n".join(
            f"- {a['agent']} ({', '.join(a['handles'])}): {a['role']}"
            for a in agents)
        return (
            "\nOther agents you can hand work to with create_task. Pass "
            "`needs`\nwith the capability in brackets, and attach what the "
            "work concerns:\n" + lines + "\n")

    # The index, not the content. Putting past turns themselves in here would
    # re-open the bug in docs/TESTING.md section 3 attempt 3: an agent reading
    # back its own prose and repeating a fact retired minutes earlier, because
    # a sentence has no valid_to. A one-line index carries no assertions, and
    # the model fetches the real conversation with read_topic when it wants it.
    #
    # Without this the model cannot ask at all. "Let's pick up the lift logic"
    # only works if something already told it a lift conversation exists --
    # recent_conversations lists sessions by their *opening line*, which for a
    # long design discussion is the least representative sentence in it.
    MAX_TOPIC_INDEX = 8

    def _topic_index(self) -> str:
        """What this workspace has talked about, newest first."""
        if not self.topic_index:
            return ""
        try:
            topics = self.toolbox.store.topic_index(limit=self.MAX_TOPIC_INDEX)
        except Exception:
            return ""                      # never break a turn over this
        if not topics:
            return ""
        lines = "\n".join(
            f"- {t['topic']} ({t['turns']} turn(s), last {t['last'][:10]})"
            for t in topics)
        # Worded as reference, not as an invitation. The first version said
        # "To continue one, call read_topic with the name exactly as written
        # here", and a model asked for Java Fibonacci read the unrelated lift
        # conversation and appended where it had left off on it. The index
        # has to say the subjects exist without suggesting they be opened.
        # The wording matters and has been wrong in both directions. It
        # first read "To continue one, call read_topic", and a model asked
        # for Java Fibonacci opened an unrelated lift conversation and
        # reported where it had left off. Corrected to "do not read any of
        # these unless the person asks to return to one", which produced
        # the opposite failure: asked "how many shoes do i own?" with a
        # `nike shoes` subject sitting in this list, it read nothing and
        # said it had no record.
        #
        # The distinction is not whether they asked to *resume*, it is
        # whether the question is *about* one of these.
        return (
            "\nSubjects this person has worked on before:\n" + lines +
            "\nIf what they just said is about one of these, read it with "
            "read_topic, using\nthe name exactly as written. If it is not, "
            "leave them alone -- do not open one\nand do not mention where "
            "you left off on something they did not ask about.\n")

    # A rule the person taught once has to apply every time, including on the
    # turns where the model sees no reason to consult its memory. Leaving that
    # to a tool call does not work: asked to write hello world, a model that
    # had been taught "prefer Java" called no tools at all and answered in
    # Python, because it did not think the question needed remembering.
    #
    # So the rules are read from the graph on every turn and put in front of
    # it. This is still graph-backed memory -- the prompt is rebuilt from a
    # FalkorDB query each time, and retiring a rule removes it from the next
    # turn -- it just does not depend on the model electing to look.
    MAX_STANDING_RULES = 10

    def _standing_rules(self) -> str:
        """Live procedural memory, rendered into the prompt."""
        if not self.standing_rules:
            return ""
        try:
            rules = self.toolbox.store.get_applicable_procedures()
        except Exception:
            return ""                      # never break a turn over this
        if not rules:
            return ""
        lines = "\n".join(
            f"- {r['name']}: {r['action']}"
            + (f" (applies when: {r['trigger']})" if r.get("trigger") else "")
            for r in rules[:self.MAX_STANDING_RULES])
        return (
            "\nStanding rules this person has taught you. They apply whether "
            "or not\nanything in the message mentions them, and they are not "
            "to be restated\nback at the person as news:\n" + lines + "\n")

    # How much of the current conversation to replay. Bounded by characters
    # rather than turns because a turn here can be four thousand characters
    # of Java: `failed_curations` recorded a real
    # "reply hit the context limit (num_ctx=16384)" on exactly that.
    # 5000 was too tight to be useful: one Java answer was 6,918 characters
    # on its own, so a single code exchange did not fit and the history came
    # back empty. The real ceiling is the model's window -- ornith:9b runs
    # num_ctx=16384, roughly 65k characters, against which the system
    # prompt, the tool schemas and the reply also have to fit.
    HISTORY_CHARS = 12000
    HISTORY_TURNS = 20

    def _history(self) -> list[Message]:
        """Replay this conversation, read back out of the graph.

        Until now the loop began every turn with `[Message(user_message)]`
        and nothing else, so the model had never seen what was said earlier
        in the same chat. The UI shows a continuous thread; the model saw
        each message alone. Asked "can you add my uid to this code?" two
        messages after writing three thousand characters of Java, it
        answered "I don't see any code attached to your message" -- and it
        was telling the truth.

        The history comes from `session_transcript`, which is a Cypher
        query, not from anything held in this process. That is the same
        argument the UI already makes for rebuilding its transcript on every
        render: kill the process, reload the page, and the conversation is
        identical because it was never anywhere but the graph. An agent that
        kept its own history in memory would be the one place in this system
        where recall did not go through FalkorDB.

        Oldest messages are dropped first, and the result always starts on a
        user turn -- an assistant message with no question before it reads
        as the model talking to itself.
        """
        try:
            rows = self.toolbox.store.session_transcript(
                self.toolbox.session_id, limit=self.HISTORY_TURNS)
        except Exception:
            return []                       # never break a turn over this
        if not rows:
            return []

        # Group into exchanges -- a user message and everything said in
        # reply -- and keep whole exchanges only. Trimming by individual
        # message does not work: the first version kept the newest message,
        # which for a code answer was a single 6,918-character assistant
        # turn, then dropped it again because a history may not begin on an
        # assistant message. Two correct guards, and between them they
        # produced an empty history in precisely the case that matters --
        # "can you add my uid to this code?" right after the code.
        exchanges, current = [], []
        for row in rows:
            if row["speaker"] == "user" and current:
                exchanges.append(current)
                current = []
            current.append(row)
        if current:
            exchanges.append(current)
        # Anything before the first user message is a reply with no question
        # in front of it; a provider will reject a history that starts that
        # way, and it reads as the model talking to itself.
        while exchanges and exchanges[0][0]["speaker"] != "user":
            exchanges.pop(0)

        kept, total = [], 0
        for group in reversed(exchanges):   # newest first, drop the oldest
            size = sum(len(r["text"] or "") for r in group)
            # The most recent exchange goes in whatever it costs. It is what
            # "this code" and "that one" refer to, and dropping it to stay
            # under budget defeats the point of having a history at all.
            if kept and total + size > self.HISTORY_CHARS:
                break
            total += size
            kept.append(group)
        kept.reverse()

        return [
            Message(role="user" if r["speaker"] == "user" else "assistant",
                    text=r["text"])
            for group in kept for r in group
        ]

    def _remember(self, speaker, text):
        return self.toolbox.store.record_event(
            self.toolbox.session_id, self.name, "message", text, speaker=speaker)

    def _curate(self, user_text, reply_text, event_ids):
        """Second pass: the only thing in the system that writes facts.

        Runs after the answer is already in the graph, so a failure here
        costs edges and never the reply. Returns None when this agent has no
        curator, which is how the Reviewer stays a pure reader.
        """
        if not (self.curator and reply_text and any(event_ids)):
            return None
        from agents.curate import curate_turn          # local: optional path
        return curate_turn(
            self.toolbox.store, self.curator, self.toolbox.session_id,
            self.name, user_text, reply_text, event_ids)

    def _close(self, text, steps, messages, stopped_early, record,
               user_event=None, user_text="", replayed=0) -> Turn:
        """Write the answer, then point it at the reads that produced it.

        The READ edges are what make "why did you say that" a traversal
        instead of a screenshot: they live in the graph, so the trace
        outlives this process, this page and this session.

        With `record=False` there is no answer node to attach them to, so
        the lookups stay in the graph unlinked, findable by their `session`
        property. That is the honest outcome -- the reads did happen.
        """
        lookups = list(self.toolbox.lookups)
        curation = None
        if record and text:
            answer_id = self._remember(self.name, text)
            if lookups:
                self.toolbox.store.link_reasoning(answer_id, lookups)
            # Both events get the turn's topics and mentions. The question
            # and the answer are about the same thing, and linking only one
            # would make half the conversation unreachable from its subject.
            curation = self._curate(user_text, text, [user_event, answer_id])
            if curation is not None:
                # Record whether it worked, on the answer itself. A curator
                # that fails quietly is worse than no curator: the person is
                # told "noted" and nothing is kept, with nothing anywhere to
                # say so.
                try:
                    self.toolbox.store.record_curation(
                        answer_id, not curation.error,
                        curation.error or ", ".join(
                            curation.topics + curation.facts + curation.rules))
                except Exception:
                    pass          # never let bookkeeping break a turn
        return Turn(text=text, steps=steps, messages=messages,
                    stopped_early=stopped_early, lookups=lookups,
                    curation=curation, replayed=replayed)

    def run(self, user_message: str, record=True, speaker="user") -> Turn:
        """Answer one message, running tools until the model stops asking.

        Both sides of the exchange are written to the graph, tagged with who
        said them. That is what lets a conversation be read back out of
        memory later -- by this agent in a future session, by the other
        agent, or by a UI rebuilding a transcript.

        `record=False` skips the episodic write, for read-only questions
        where an event would be noise.
        """
        # Replay this conversation BEFORE recording the new message, so the
        # current turn is not in it twice.
        history = self._history()
        user_event = self._remember(speaker, user_message) if record else None

        # One turn, one trace. The toolbox fills this as the model reads.
        self.toolbox.lookups.clear()
        messages: list[Message] = history + [
            Message(role="user", text=user_message)]
        specs = self.toolbox.specs()
        steps: list[Step] = []
        stopped_early = False
        # Smaller models re-ask for things they already have. Caching the
        # answer and saying so is cheaper than re-querying, and the nudge
        # pushes the model to answer instead of circling.
        seen: dict[tuple, str] = {}
        per_tool: dict[str, int] = {}

        while True:
            response = self.llm.chat(messages, tools=specs,
                                     system=self.system_prompt())

            if not response.wants_tools:
                messages.append(Message(role="assistant", text=response.text,
                                        provider_state=response.raw))
                return self._close(response.text, steps, messages,
                                   stopped_early, record,
                                   user_event, user_message, len(history))

            # Carry provider_state so the assistant turn replays byte-identically.
            messages.append(Message(
                role="assistant",
                text=response.text,
                tool_calls=response.tool_calls,
                provider_state=response.raw,
            ))

            remaining = self.max_tool_calls - len(steps)
            if remaining <= 0:
                # Refuse the calls rather than dropping them: every tool_use
                # block must get a tool_result, or the next request is invalid.
                results = [
                    ToolResult(call_id=c.id, is_error=True,
                               content="Error: tool-call budget exhausted for "
                                       "this question. Answer with what you have.")
                    for c in response.tool_calls
                ]
                stopped_early = True
            else:
                results = []
                for call in response.tool_calls:
                    key = (call.name, json.dumps(call.arguments, sort_keys=True,
                                                 default=str))
                    per_tool[call.name] = per_tool.get(call.name, 0) + 1
                    if per_tool[call.name] > MAX_CALLS_PER_TOOL:
                        result = ToolResult(
                            call_id=call.id, is_error=True,
                            content=f"You have called {call.name} "
                                    f"{MAX_CALLS_PER_TOOL} times this turn. "
                                    f"It has told you what it can. Answer "
                                    f"with what you already have.")
                    elif key in seen:
                        result = ToolResult(
                            call_id=call.id,
                            content=f"You already called {call.name} with these "
                                    f"arguments. The answer has not changed:\n"
                                    f"{seen[key]}\n"
                                    f"Use it and give your answer now.")
                    else:
                        result = self.toolbox.dispatch(call)
                        if not result.is_error:
                            seen[key] = result.content
                    results.append(result)
                    steps.append(Step(name=call.name, arguments=call.arguments,
                                      result=result.content,
                                      is_error=result.is_error))

            # All results for one assistant turn go back in ONE user message.
            messages.append(Message(role="user", tool_results=tuple(results)))

            if stopped_early:
                # Let the model produce a final answer from what it has, then
                # stop regardless of what it asks for next.
                final = self._final_answer(messages)
                messages.append(Message(role="assistant", text=final,
                                        provider_state=None))
                return self._close(final, steps, messages, True, record,
                                   user_event, user_message, len(history))

    def _final_answer(self, messages) -> str:
        """Get prose out of the model once the tool budget is spent.

        Asking again *with* tools often just produces another tool call and
        no text, which surfaces as a silent empty reply. Dropping the tools
        leaves the model nothing to do but answer.
        """
        reply = self.llm.chat(messages, tools=self.toolbox.specs(),
                              system=self.system_prompt())
        if reply.text:
            return reply.text
        closing = list(messages) + [Message(
            role="user",
            text="Do not call any more tools. Answer now, in plain prose, "
                 "using only what the tools above returned.")]
        last = self.llm.chat(closing, tools=(), system=self.system_prompt()).text
        # A model that spends its whole budget searching and then returns
        # nothing leaves an empty bubble on screen, which reads as a crash.
        # Saying so is worse than an answer and far better than silence.
        return last or (
            "I ran out of tool calls before I got to an answer. Ask me again, "
            "more specifically, and I will go straight at it.")
