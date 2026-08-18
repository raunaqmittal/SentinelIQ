"""CrewAI orchestration: deterministic supervisor routes to one specialist,
then the Red-Team verifies, then `engine.py` synthesises without an LLM.

Routing is plain Python (Context.md 13: "deterministic routing and explicit
state"). Agents never talk to each other freely and there are no autonomous
loops — the supervisor decides who runs, in what order, once.

CrewAI is the orchestration framework per ADR-002. LangGraph is deliberately
not used in SentinelIQ.
"""

import logging
import re
from dataclasses import dataclass

from crewai import LLM, Agent, Crew, Process, Task

from sentineliq.components.agents import compliance, financial, red_team, security
from sentineliq.components.agents.tools import build_evidence_block, retrieve_evidence
from sentineliq.pipeline import engine

logger = logging.getLogger(__name__)

#: Clause types each specialist owns. Anything unmapped goes to compliance,
#: which covers general contractual obligations.
ROUTING = {
    financial: {
        "Cap On Liability", "Uncapped Liability", "Liquidated Damages",
        "Minimum Commitment", "Revenue/Profit Sharing", "Price Restrictions",
        "Volume Restriction", "Most Favored Nation", "Insurance",
    },
    security: {
        "Ip Ownership Assignment", "Joint Ip Ownership", "License Grant",
        "Non-Transferable License", "Irrevocable Or Perpetual License",
        "Affiliate License-Licensee", "Affiliate License-Licensor",
        "Unlimited/All-You-Can-Eat-License",
    },
}
DEFAULT_SPECIALIST = compliance

#: Investigation questions are tagged with a risk category, not a CUAD clause
#: type, so they need their own map. `contract` goes to the Compliance Analyst
#: on purpose — FR-011 already lists contractual obligations among its checks
#: and the documentation defines no Contract Agent.
CATEGORY_ROUTING = {
    "compliance": compliance,
    "contract": compliance,
    "financial": financial,
    "security": security,
}

#: The verify task must end with these lines so Python, not the LLM, turns the
#: judgement into a number and into the escalation flag.
SEVERITY_PATTERN = re.compile(r"SEVERITY:\s*(LOW|MEDIUM|HIGH)", re.IGNORECASE)
CONTRADICTION_PATTERN = re.compile(r"CONTRADICTION:\s*(YES|NO)", re.IGNORECASE)


@dataclass
class RunContext:
    """Everything loaded once and shared across questions."""

    config: object
    embedder: object
    faiss_index: object
    bm25_index: object
    cross_encoder: object
    chunks: list
    llm: LLM


def route(clause_type: str):
    """Pick the one specialist that owns this clause type."""
    for specialist, owned in ROUTING.items():
        if clause_type in owned:
            return specialist
    return DEFAULT_SPECIALIST


def route_category(category: str):
    """Pick the specialist that owns an investigation question's category."""
    if category not in CATEGORY_ROUTING:
        raise ValueError(f"unknown question category: {category!r}")
    return CATEGORY_ROUTING[category]


def parse_severity(text: str) -> str | None:
    """Read the SEVERITY label out of an agent's answer.

    Returns None when the agent did not give one — an abstention has no
    severity, and a missing label must not be guessed at.
    """
    match = SEVERITY_PATTERN.search(text)
    return match.group(1).upper() if match else None


def parse_contradiction(text: str) -> bool:
    """True when the Red-Team reported a contradiction in the evidence.

    The agent only says YES or NO; what that does to the recommendation is
    decided by `engine.score_investigation` in plain Python (ADR-021). Anything
    missing or unreadable counts as no contradiction, so the flag can only be
    raised by an explicit report.
    """
    match = CONTRADICTION_PATTERN.search(text)
    return bool(match) and match.group(1).upper() == "YES"


def build_llm(model: str, api_key: str, temperature: float, base_url: str) -> LLM:
    """CrewAI's LLM handle for a Groq-hosted model.

    Uses CrewAI's **native openai provider** against Groq's OpenAI-compatible
    endpoint rather than the `groq/` LiteLLM route: the LiteLLM path forwards
    CrewAI's internal `cache_breakpoint` message field, which Groq rejects with
    a 400.
    """
    return LLM(
        model=f"openai/{model}",
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
    )


def _agent(module, llm: LLM) -> Agent:
    return Agent(
        role=module.ROLE,
        goal=module.GOAL,
        backstory=(
            f"A due-diligence specialist focused on {module.FOCUS}. You answer "
            "only from the evidence supplied and you cite the id of every chunk "
            "you rely on, in square brackets."
        ),
        llm=llm,
        allow_delegation=False,  # no agent-to-agent chatter (Context.md 13)
        verbose=False,
    )


#: CrewAI's usage counter accumulates across the process rather than resetting
#: per Crew, so raw readings are running totals. Track the last value and report
#: the delta. Handles either behaviour: a counter that reset reads lower than
#: the last total, in which case it is already the per-run figure.
_last_usage = {"prompt": 0, "completion": 0}


def _usage_delta(crew) -> dict:
    """Tokens used by this crew alone, not the process total."""
    usage = getattr(crew, "usage_metrics", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0}
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    delta_in = prompt - _last_usage["prompt"] if prompt >= _last_usage["prompt"] else prompt
    delta_out = (
        completion - _last_usage["completion"]
        if completion >= _last_usage["completion"]
        else completion
    )
    _last_usage["prompt"], _last_usage["completion"] = prompt, completion
    return {"input_tokens": delta_in, "output_tokens": delta_out}


def investigate(context: RunContext, question: str, clause_type: str) -> dict:
    """Answer one question: retrieve, specialist, red-team, deterministic merge."""
    chunks = retrieve_evidence(context, question)
    evidence = build_evidence_block(chunks)
    supplied = [chunk.chunk_id for chunk in chunks]

    specialist = route(clause_type)
    draft_task = Task(
        description=(
            f"{evidence}\n\nQuestion: {question}\n\n"
            "Answer in two or three sentences using only the evidence. Cite the "
            "id of every chunk you use, like [some_id_0007]. If the evidence "
            "does not answer the question, reply exactly: NOT FOUND IN EVIDENCE"
        ),
        expected_output="A short grounded answer with citations, or NOT FOUND IN EVIDENCE.",
        agent=_agent(specialist, context.llm),
    )
    verify_task = Task(
        # The evidence is repeated here on purpose. `context=[draft_task]`
        # passes only the draft *output*, so without this the Red-Team cannot
        # see the source text — it could neither check claims against it nor
        # spot injected instructions (NFR-003c). Costs tokens; kept anyway.
        description=(
            f"{evidence}\n\nQuestion: {question}\n\n"
            "Check the draft answer against the evidence. Remove unsupported "
            "claims, drop citations that do not support their claim, and keep "
            "only what the evidence proves. Report the corrected answer in the "
            "same format. If nothing in the evidence answers the question, "
            "reply exactly: NOT FOUND IN EVIDENCE"
        ),
        expected_output="The corrected, verified answer with citations.",
        agent=_agent(red_team, context.llm),
        context=[draft_task],
    )

    crew = Crew(
        agents=[draft_task.agent, verify_task.agent],
        tasks=[draft_task, verify_task],
        process=Process.sequential,
        verbose=False,
    )
    crew.kickoff()

    result = engine.synthesise(
        str(draft_task.output), str(verify_task.output), supplied
    )
    result.update(
        supplied=supplied,
        specialist=specialist.ROLE,
        draft=str(draft_task.output),
        verified=str(verify_task.output),
        **_usage_delta(crew),
    )
    return result


def investigate_finding(context: RunContext, question: str, category: str) -> dict:
    """Answer one investigation question and rate how risky the answer is.

    Same two-agent shape as `investigate`, with two differences: the specialist
    is chosen by risk category rather than CUAD clause type, and the Red-Team
    also labels the finding LOW/MEDIUM/HIGH. It is a separate function rather
    than a flag on `investigate` because that one is frozen for the Stage 8/9
    comparison and must keep producing byte-identical prompts.
    """
    chunks = retrieve_evidence(context, question)
    evidence = build_evidence_block(chunks)
    supplied = [chunk.chunk_id for chunk in chunks]

    specialist = route_category(category)
    draft_task = Task(
        description=(
            f"{evidence}\n\nQuestion: {question}\n\n"
            "Answer in two or three sentences using only the evidence. Cite the "
            "id of every chunk you use, like [some_id_0007]. If the evidence "
            "does not answer the question, reply exactly: NOT FOUND IN EVIDENCE"
        ),
        expected_output="A short grounded answer with citations.",
        agent=_agent(specialist, context.llm),
    )
    verify_task = Task(
        description=(
            f"{evidence}\n\nQuestion: {question}\n\n"
            "Check the draft answer against the evidence. Remove unsupported "
            "claims and drop citations that do not support their claim. Then "
            "add two final lines, exactly like:\n"
            "SEVERITY: MEDIUM\nCONTRADICTION: NO\n"
            "Severity is LOW, MEDIUM or HIGH — how much risk this finding "
            "shows for the vendor. Contradiction is YES only when the evidence "
            "contains statements that contradict each other or contradict the "
            "vendor's own claims. If nothing in the evidence answers the "
            "question, reply exactly NOT FOUND IN EVIDENCE and give neither line."
        ),
        expected_output=(
            "The verified answer with citations, then a SEVERITY and a "
            "CONTRADICTION line."
        ),
        agent=_agent(red_team, context.llm),
        context=[draft_task],
    )

    crew = Crew(
        agents=[draft_task.agent, verify_task.agent],
        tasks=[draft_task, verify_task],
        process=Process.sequential,
        verbose=False,
    )
    crew.kickoff()

    verified = str(verify_task.output)
    result = engine.synthesise(str(draft_task.output), verified, supplied)
    # Those two lines are machine input, not part of the answer a reader sees.
    answer = SEVERITY_PATTERN.sub("", result["answer"])
    result["answer"] = CONTRADICTION_PATTERN.sub("", answer).strip()
    result.update(
        supplied=supplied,
        category=category,
        specialist=specialist.ROLE,
        severity=parse_severity(verified),
        contradiction=parse_contradiction(verified),
        **_usage_delta(crew),
    )
    return result
