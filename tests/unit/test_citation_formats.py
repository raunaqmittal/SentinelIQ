"""Citations must survive the way a model actually writes them (Stage 13).

Two production defects found by the Stage 12/13 analysis:
  1. `cited_ids` matched only ASCII brackets, so a full-width citation was read
     as no citation. 6 of 35 Stage 9 answers cited only that way.
  2. `synthesise` compared ids by exact string, so a correct citation written
     with a non-breaking hyphen was dropped from the report.

No LLM is called here.
"""

from sentineliq.pipeline import engine, investigation

NBHYPHEN = "‑"
NNBSP = " "
OPEN, CLOSE = "【", "】"

CHUNK = "SomeContract_8-K_EX-10.1_0004"
OTHER = "OtherContract_0001"


class FakeChunk:
    def __init__(self, chunk_id):
        self.chunk_id = chunk_id
        self.document_name = "SomeContract.pdf"
        self.page_start = 3
        self.page_end = 4


# --- extraction ---------------------------------------------------------


def test_an_ascii_citation_is_extracted():
    assert engine.cited_ids(f"as stated [{CHUNK}] in clause 4") == [CHUNK]


def test_a_full_width_citation_is_extracted():
    assert engine.cited_ids(f"as stated {OPEN}{CHUNK}{CLOSE} in clause 4") == [CHUNK]


def test_both_bracket_styles_in_one_answer():
    assert engine.cited_ids(f"[{CHUNK}] and {OPEN}{OTHER}{CLOSE}") == [CHUNK, OTHER]


def test_a_repeated_citation_appears_once():
    assert engine.cited_ids(f"[{CHUNK}] again [{CHUNK}]") == [CHUNK]


def test_prose_without_brackets_cites_nothing():
    assert engine.cited_ids("The agreement lasts twelve months.") == []


# --- matching in synthesise ---------------------------------------------


def test_an_ascii_citation_survives_synthesis():
    result = engine.synthesise("draft", f"verified [{CHUNK}]", [CHUNK])

    assert result["citations"] == [CHUNK]
    assert result["dropped_citations"] == []


def test_a_full_width_citation_survives_synthesis():
    result = engine.synthesise("draft", f"verified {OPEN}{CHUNK}{CLOSE}", [CHUNK])

    assert result["citations"] == [CHUNK]


def test_a_non_breaking_hyphen_does_not_drop_a_valid_citation():
    written = CHUNK.replace("-", NBHYPHEN)
    result = engine.synthesise("draft", f"verified [{written}]", [CHUNK])

    assert result["citations"] == [CHUNK], "kept, and in the supplied spelling"
    assert result["dropped_citations"] == []


def test_a_narrow_no_break_space_does_not_drop_a_valid_citation():
    spaced = "HEMISPHERX - Sales, Marketing and Supply Agreement_0013"
    written = spaced.replace(" ", NNBSP)
    result = engine.synthesise("draft", f"verified [{written}]", [spaced])

    assert result["citations"] == [spaced]


def test_an_invented_citation_is_still_dropped():
    result = engine.synthesise("draft", "verified [Invented_9999]", [CHUNK])

    assert result["citations"] == []
    assert result["dropped_citations"] == ["Invented_9999"]


def test_a_citation_to_another_tenants_chunk_is_still_dropped():
    """The isolation property this filter exists for must not weaken."""
    result = engine.synthesise("draft", f"verified [{OTHER}]", [CHUNK])

    assert result["citations"] == []
    assert OTHER in result["dropped_citations"]


def test_two_spellings_of_one_id_yield_one_citation():
    written = CHUNK.replace("-", NBHYPHEN)
    answer = f"[{CHUNK}] and {OPEN}{written}{CLOSE}"
    result = engine.synthesise("draft", answer, [CHUNK])

    assert result["citations"] == [CHUNK]


# --- the citation reaches the report ------------------------------------


def test_a_recovered_citation_resolves_to_real_evidence_in_the_report():
    """`evidence_detail` looks a chunk up by exact key, so synthesis must
    return the supplied spelling — otherwise a recovered citation crashes
    the report."""
    by_id = {CHUNK: FakeChunk(CHUNK)}
    written = CHUNK.replace("-", NBHYPHEN)
    result = engine.synthesise("draft", f"verified {OPEN}{written}{CLOSE}", [CHUNK])

    detail = investigation.evidence_detail(by_id, result["citations"][0])

    assert detail["chunk_id"] == CHUNK
    assert detail["document_name"] == "SomeContract.pdf"
    assert detail["page_start"] == 3


def test_an_injection_marker_still_surfaces_with_a_full_width_citation():
    """The security property must not regress with the new pattern."""
    answer = f"INJECTION ATTEMPT DETECTED in {OPEN}{CHUNK}{CLOSE}"
    result = engine.synthesise("draft", answer, [CHUNK])

    assert result["injection_flagged"] is True
    assert result["citations"] == [CHUNK]
