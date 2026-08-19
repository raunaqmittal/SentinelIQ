"""Document Q&A: grounding, citation validation and abstention.

No LLM and no models are involved — the provider returns canned text and
retrieval is replaced by a fixed chunk list, so what is tested here is the part
that decides whether an answer may be shown at all.
"""

# 2. Third-party imports
import pytest

# 3. Internal imports
from sentineliq.components.llm.provider import LLMResponse
from sentineliq.components.models.schemas import Chunk
from sentineliq.pipeline import qa

CHUNKS = [
    Chunk(
        chunk_id="doc-abc_0000",
        document_id="doc-abc",
        document_name="contract.pdf",
        text="Either party may terminate for convenience on ninety days notice.",
        char_start=0,
        char_end=64,
        page_start=1,
        page_end=1,
    ),
    Chunk(
        chunk_id="doc-abc_0001",
        document_id="doc-abc",
        document_name="contract.pdf",
        text="Liability is capped at the fees paid in the prior twelve months.",
        char_start=64,
        char_end=128,
        page_start=2,
        page_end=2,
    ),
]


class FakeProvider:
    """Returns one fixed completion, so the test controls what the model said."""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def complete(self, system, user, *, temperature, max_tokens):
        self.calls += 1
        self.last_user_prompt = user
        return LLMResponse(
            text=self.text, input_tokens=1, output_tokens=1, model="fake"
        )


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch):
    """Retrieval returns the fixed chunks, standing in for the frozen pipeline."""
    monkeypatch.setattr(qa, "retrieve_evidence", lambda context, question: list(CHUNKS))


def ask(text, question="What are the termination terms?"):
    provider = FakeProvider(text)
    result = qa.ask(None, provider, question, temperature=0.0, max_tokens=512)
    return result, provider


def test_a_grounded_answer_keeps_its_citation():
    result, _ = ask("Either party may terminate on ninety days notice [doc-abc_0000].")

    assert result["abstained"] is False
    assert [item["chunk_id"] for item in result["citations"]] == ["doc-abc_0000"]
    assert result["citations"][0]["document_name"] == "contract.pdf"
    assert result["citations"][0]["page_start"] == 1


def test_a_fabricated_citation_is_dropped_but_a_real_one_survives():
    result, _ = ask("Notice is ninety days [doc-abc_0000] and [made_up_9999].")

    assert [item["chunk_id"] for item in result["citations"]] == ["doc-abc_0000"]


def test_an_answer_whose_every_citation_was_invented_is_not_shown():
    """An answer standing on nothing is insufficient evidence, not an answer."""
    result, _ = ask("The contract says whatever I like [not_a_real_chunk].")

    assert result["abstained"] is True
    assert result["answer"] == qa.INSUFFICIENT
    assert result["citations"] == []


def test_an_answer_with_no_citations_at_all_is_not_shown():
    result, _ = ask("The contract allows termination at any time.")

    assert result["abstained"] is True
    assert result["answer"] == qa.INSUFFICIENT


def test_an_explicit_abstention_is_reported_as_insufficient_evidence():
    result, _ = ask("NOT FOUND IN EVIDENCE")

    assert result["abstained"] is True
    assert result["answer"] == qa.INSUFFICIENT
    assert result["citations"] == []


def test_the_retrieved_evidence_is_always_returned():
    """A reader must be able to check the answer against what was retrieved."""
    result, _ = ask("NOT FOUND IN EVIDENCE")

    assert [item["chunk_id"] for item in result["retrieved"]] == [
        "doc-abc_0000",
        "doc-abc_0001",
    ]
    assert all(item["text"] for item in result["retrieved"])


def test_only_the_retrieved_chunks_reach_the_model():
    _, provider = ask("Capped at twelve months of fees [doc-abc_0001].")

    assert provider.calls == 1
    assert "doc-abc_0000" in provider.last_user_prompt
    assert "doc-abc_0001" in provider.last_user_prompt


def test_an_empty_question_is_refused():
    with pytest.raises(ValueError):
        qa.ask(None, FakeProvider("x"), "   ", temperature=0.0, max_tokens=512)


def test_no_evidence_retrieved_means_insufficient_evidence(monkeypatch):
    monkeypatch.setattr(qa, "retrieve_evidence", lambda context, question: [])
    provider = FakeProvider("should never be called")

    result = qa.ask(None, provider, "anything?", temperature=0.0, max_tokens=512)

    assert result["abstained"] is True
    assert provider.calls == 0, "the model was called with no evidence"


def test_a_citation_written_with_an_unusual_hyphen_still_counts():
    """Models reproduce ids with lookalike characters; that is still a citation."""
    kept = qa.valid_citations("see [doc‑abc_0000]", ["doc-abc_0000"])

    assert kept == ["doc-abc_0000"]


def test_duplicate_citations_are_listed_once():
    kept = qa.valid_citations(
        "[doc-abc_0000] and again [doc-abc_0000]", ["doc-abc_0000"]
    )

    assert kept == ["doc-abc_0000"]
