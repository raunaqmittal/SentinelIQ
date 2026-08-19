"""SentinelIQ dashboard (Streamlit).

Talks to the FastAPI backend over HTTP only — it never imports the pipeline, so
the UI can run in its own environment without the ML stack.

Run with:
    streamlit run frontend/app.py
"""

# 1. Standard library imports
import os
import time

# 2. Third-party imports
import requests
import streamlit as st

# 3. Constants
API_URL = os.environ.get("SENTINELIQ_API_URL", "http://127.0.0.1:8000")
# Every call is short now that /run returns straight away (FR-022).
TIMEOUT = 60
# Q&A blocks until the answer is returned. On the first call the retrieval
# models (embedder + cross-encoder) cold-start from disk which can take
# 60-120s. Subsequent calls reuse the cached models and finish in seconds.
QA_TIMEOUT = 180
POLL_SECONDS = 3

RISK_COLOURS = {
    "low": "green",
    "medium": "orange",
    "high": "red",
    "critical": "red",
}


def headers() -> dict:
    """This showcase frontend is always anonymous — no token to send.

    The API's JWT/RBAC implementation is untouched; this frontend just never
    calls /api/auth/login. It relies on the API running with
    SENTINELIQ_DEMO_MODE=true, which serves an anonymous request as a fixed
    demo principal (see app.get_principal).
    """
    return {}


def call(method: str, path: str, timeout: int = TIMEOUT, **kwargs):
    """Call the API, or return None after showing the user what went wrong.

    Every caller gets one of two things: a successful response, or None with a
    message already on screen. That keeps the page functions free of error
    handling and means an unreachable API never shows a raw traceback.
    """
    try:
        response = requests.request(
            method, f"{API_URL}{path}", headers=headers(), timeout=timeout, **kwargs
        )
    except requests.Timeout:
        st.error("The API took too long to respond. It may still be working.")
        return None
    except requests.RequestException:
        st.error(f"Cannot reach the API at {API_URL}. Is it running?")
        return None

    # This frontend never sends a token, so a 401 means the API is not
    # running in demo mode (see main()'s check before any page renders).
    if response.status_code == 401:
        st.error("The API rejected this as unauthenticated. Is demo mode on?")
        return None
    if response.status_code == 403:
        st.error("You do not have permission to do that.")
        return None
    return response


def api_get(path: str):
    """GET from the API."""
    return call("GET", path)


def api_post(path: str, **kwargs):
    """POST to the API."""
    return call("POST", path, **kwargs)


def api_health() -> dict | None:
    """Ask the API whether it is up and whether demo mode is on."""
    try:
        response = requests.get(f"{API_URL}/health", timeout=10)
    except requests.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


def failed(response, message: str) -> bool:
    """True when the call did not succeed; shows `message` if so."""
    if response is None:
        return True
    if response.status_code >= 400:
        st.error(f"{message} ({response.status_code})")
        return True
    return False


def show_verdict(report: dict) -> None:
    """The headline: recommendation, score and whether a human must look."""
    left, middle, right = st.columns(3)
    left.metric("Recommendation", report["recommendation"])
    colour = RISK_COLOURS.get(report["risk_level"], "gray")
    middle.metric("Overall risk", f"{report['overall_score']} ({report['risk_level']})")
    right.metric("Human review", "REQUIRED" if report["escalate"] else "not required")
    st.markdown(f":{colour}[Risk level: **{report['risk_level']}**]")

    if report.get("contradiction_questions"):
        found = ", ".join(report["contradiction_questions"])
        st.error(f"Contradiction detected in {found} — human review required.")
    if report.get("injection_flagged"):
        st.warning("An injection attempt was reported in the evidence.")


def show_category_scores(report: dict) -> None:
    """Per-category risk, with evidence quality shown as quality not risk."""
    st.subheader("Category scores")
    scores = report["category_scores"]
    rows = []
    for category, score in scores.items():
        note = ""
        if category == "evidence_quality":
            note = f"risk; evidence quality {report.get('evidence_quality')}"
        count = report.get("findings_per_category", {}).get(category)
        if count is not None:
            note = note or f"{count} findings"
        rows.append({"category": category, "risk score": score, "note": note})
    st.table(rows)


def show_why(report: dict) -> None:
    """The ordered, cited reasons behind the recommendation (FR-016)."""
    st.subheader("Why this recommendation")
    reasons = report.get("why") or []
    if not reasons:
        st.info("No reasons recorded — nothing was found to explain.")
        return
    for position, reason in enumerate(reasons, start=1):
        label = reason["label"]
        headline = (
            f"{position}. {label} · {reason['category']} · {reason['severity']}"
            + (" · CONTRADICTION" if reason["contradiction"] else "")
        )
        if label == "CRITICAL":
            st.error(headline)
        else:
            st.warning(headline)
        st.write(reason["reason"])
        for evidence in reason["evidence"]:
            st.caption(f"Evidence → {cite(evidence)}")


def cite(evidence: dict) -> str:
    """One evidence item as a reader sees it."""
    if evidence.get("page_start") is None:
        return evidence["document_name"]
    if evidence.get("page_end") and evidence["page_end"] != evidence["page_start"]:
        return (
            f"{evidence['document_name']}, "
            f"pp.{evidence['page_start']}-{evidence['page_end']}"
        )
    return f"{evidence['document_name']}, p.{evidence['page_start']}"


def show_findings(report: dict) -> None:
    """Findings grouped by category, including the ones that found nothing."""
    st.subheader("Findings by category")
    findings = report["findings"]
    for category in ("compliance", "security", "financial", "contract"):
        in_category = [f for f in findings if f["category"] == category]
        if not in_category:
            continue
        st.markdown(f"**{category.upper()}**")
        for finding in in_category:
            severity = finding["severity"] or "NO ANSWER"
            title = f"[{finding['question_id']}] {severity}"
            if finding["contradiction"]:
                title += " · CONTRADICTION"
            with st.expander(title):
                st.write(f"**Q:** {finding['question']}")
                st.write(f"**A:** {finding['answer']}")
                for evidence in finding["evidence"]:
                    st.caption(f"Evidence → {cite(evidence)}  `{evidence['chunk_id']}`")

    unanswered = [f for f in findings if f["severity"] is None]
    if unanswered:
        st.subheader("Questions with no usable evidence")
        for finding in unanswered:
            st.write(f"- [{finding['question_id']}] {finding['question']}")


def evidence_explorer(investigation_id: str) -> None:
    """Every citation the investigation used, in one list."""
    st.subheader("Evidence explorer")
    response = api_get(f"/api/investigations/{investigation_id}/evidence")
    if failed(response, "Could not load the evidence."):
        return
    items = response.json()
    if not items:
        st.info("No evidence stored for this investigation.")
        return
    st.table(
        [
            {
                "document": item["document_name"],
                "page": item["page_start"] or "-",
                "chunk_id": item["chunk_id"],
            }
            for item in items
        ]
    )


def show_alerts(investigations: list[dict]) -> None:
    """Alerts: the investigations a human must look at, and the failed runs.

    "Human review required" is the escalation flag the decision engine sets
    (FR-019), which the list endpoint already returns. Contradiction and
    injection detail lives on the report page — it is not in this response, so
    it is not claimed here.
    """
    escalated = [r for r in investigations if r["escalate"]]
    broken = [r for r in investigations if r["status"] == "failed"]
    if not escalated and not broken:
        return

    st.subheader("Alerts")
    for row in escalated:
        st.warning(
            f"**{row['vendor_name']}** needs human review — "
            f"{row['risk_level'] or 'unscored'} risk, "
            f"{row['recommendation'] or 'no recommendation'} "
            f"({row['investigation_id'][:8]})"
        )
    for row in broken:
        st.error(
            f"**{row['vendor_name']}** — the run failed ({row['investigation_id'][:8]})"
        )


def page_dashboard() -> None:
    """List every investigation for this tenant."""
    st.header("Investigations")
    response = api_get("/api/investigations")
    if failed(response, "Could not load investigations."):
        return
    investigations = response.json()
    if not investigations:
        st.info("No investigations yet. Start one from the sidebar.")
        return

    show_alerts(investigations)
    st.subheader("All investigations")
    st.table(
        [
            {
                # "subject", not "vendor": this column holds a vendor name for a
                # curated run and a filename for an uploaded document.
                "subject": row["vendor_name"],
                "status": row["status"],
                "risk": row["risk_level"] or "-",
                "recommendation": row["recommendation"] or "-",
                "human review": "YES" if row["escalate"] else "-",
                "id": row["investigation_id"],
            }
            for row in investigations
        ]
    )


def page_investigation() -> None:
    """Pick an investigation and show its full report."""
    st.header("Investigation report")
    response = api_get("/api/investigations")
    if failed(response, "Could not load investigations."):
        return

    complete = [r for r in response.json() if r["status"] == "complete"]
    if not complete:
        st.info("No completed investigations yet.")
        return

    labels = {
        f"{r['vendor_name']} — {r['investigation_id'][:8]}": r["investigation_id"]
        for r in complete
    }
    chosen = st.selectbox("Investigation", list(labels))
    investigation_id = labels[chosen]

    report_response = api_get(f"/api/investigations/{investigation_id}/report")
    if failed(report_response, "Could not load the report."):
        return

    report = report_response.json()
    st.caption(
        f"Investigation {report.get('investigation_id')} · "
        f"report v{report.get('report_version')} · {report.get('generated_at')}"
    )
    show_verdict(report)
    show_category_scores(report)
    show_why(report)
    show_findings(report)
    evidence_explorer(investigation_id)


def poll_until_finished(investigation_id: str, timeout: int = 1800) -> str | None:
    """Poll `/status` until the run leaves `running`. Returns the final status.

    `/run` is asynchronous (FR-022), so the UI watches the investigation rather
    than holding a request open for the length of the run.
    """
    deadline = time.monotonic() + timeout
    placeholder = st.empty()
    while time.monotonic() < deadline:
        response = api_get(f"/api/investigations/{investigation_id}/status")
        if failed(response, "Could not read the run status."):
            return None
        state = response.json()
        if state["status"] not in ("pending", "running"):
            placeholder.empty()
            return state["status"]
        placeholder.caption(f"Status: {state['status']}…")
        time.sleep(POLL_SECONDS)
    placeholder.empty()
    st.warning(
        "Still running after "
        f"{timeout // 60} minutes. It has not been cancelled — reopen this page "
        "or check the dashboard for the result."
    )
    return None


def page_new_investigation() -> None:
    """Create and run an investigation."""
    st.header("Start an investigation")
    vendor = st.text_input("Vendor name", value="Meridian CloudWorks")
    st.caption(
        "Running an investigation calls the LLM for every question and can "
        "take several minutes. The run happens on the server; this page polls "
        "for its status."
    )
    if not st.button("Create and run"):
        return

    created = api_post("/api/investigations", json={"vendor_name": vendor})
    if failed(created, "Could not create the investigation."):
        return
    investigation_id = created.json()["investigation_id"]
    st.success(f"Created {investigation_id}")

    run = api_post(f"/api/investigations/{investigation_id}/run")
    if failed(run, "Could not start the run."):
        return

    with st.spinner("Running — retrieval, agents, scoring…"):
        final = poll_until_finished(investigation_id)

    if final == "complete":
        st.success("Complete. Open the Investigation report page.")
    elif final == "failed":
        status_response = api_get(f"/api/investigations/{investigation_id}/status")
        detail = status_response.json().get("error") if status_response else None
        st.error(f"The run failed. {detail or 'Check the API logs.'}")


# ------------------------------------------------- uploaded documents


PIPELINE = ["DOCUMENT", "RETRIEVAL", "AGENTS", "RED TEAM", "RISK ENGINE", "REPORT"]

DOC_TYPE_LABELS = {
    "contract": "Contract / Legal",
    "financial": "Financial",
    "security": "Security / Compliance",
}

SUGGESTED_QUESTIONS = [
    "What are the termination clauses?",
    "Does this contract allow unilateral termination?",
    "What security or confidentiality obligations does it impose?",
    "Are there any limitations of liability?",
    "What are the most important risks in this document?",
]

CAVEAT = (
    "This score comes from a single uploaded document answered with a generic "
    "question set. The risk weights were tuned on multi-document vendor "
    "dossiers, so read it as a document risk indication, not a validated "
    "vendor risk score."
)


def show_pipeline() -> None:
    """The architecture, so a reader can see what the run actually does."""
    st.markdown("  ->  ".join(f"`{stage}`" for stage in PIPELINE))


def show_type_checklist(available: set) -> None:
    """Which of the three document types are present (spec: "Documents analyzed")."""
    for key, label in DOC_TYPE_LABELS.items():
        mark = ":green[Y]" if key in available else ":red[N]"
        st.write(f"{mark} {label}")


def show_document_report(investigation_id: str) -> None:
    """The full report of a vendor-group investigation."""
    response = api_get(f"/api/investigations/{investigation_id}/report")
    if failed(response, "Could not load the report."):
        return
    report = response.json()
    st.divider()
    st.subheader("Investigation report")
    st.caption(
        f"Investigation {report.get('investigation_id')} - "
        f"report v{report.get('report_version')} - {report.get('generated_at')}"
    )
    show_verdict(report)
    st.subheader("Documents analyzed")
    show_type_checklist(
        {
            t
            for t, present in (report.get("documents_analyzed") or {}).items()
            if present
        }
    )
    if report.get("coverage_caveat"):
        st.warning(report["coverage_caveat"])
    st.info(report.get("generalisation_caveat") or CAVEAT)
    show_category_scores(report)
    show_why(report)
    show_findings(report)
    evidence_explorer(investigation_id)


def show_citation(item: dict, expanded: bool = False) -> None:
    """One piece of evidence, with the text behind it."""
    with st.expander(f"{cite(item)}  -  {item['chunk_id']}", expanded=expanded):
        st.write(item["text"])


def ask_documents(endpoint: str, scope_caption: str) -> None:
    """The question box, answer, citations and retrieved evidence for one Q&A endpoint.

    Shared by the preloaded-demo Q&A and the new-investigation Q&A — same
    grounded retrieval -> citation flow either way, just a different
    document-set endpoint.
    """
    st.caption(f"Answering from {scope_caption} only.")
    st.caption("  -  ".join(SUGGESTED_QUESTIONS[:3]))

    question = st.text_input("Your question", key=f"question_{endpoint}")
    if not st.button("Ask", key=f"ask_{endpoint}") or not question.strip():
        return

    with st.spinner("Retrieving evidence and answering..."):
        response = call("POST", endpoint, timeout=QA_TIMEOUT, json={"question": question})
    if failed(response, "Could not answer the question."):
        return

    result = response.json()
    if result["abstained"]:
        st.warning(result["answer"])
    else:
        st.success(result["answer"])

    if result["citations"]:
        st.subheader("Citations")
        for item in result["citations"]:
            show_citation(item, expanded=True)
    elif not result["abstained"]:
        st.caption("No citations survived validation.")

    if result["retrieved"]:
        st.subheader("Evidence retrieved")
        st.caption(
            "Every chunk the model was allowed to read. An answer can only "
            "cite from this list."
        )
        for item in result["retrieved"]:
            show_citation(item)


def page_home() -> None:
    """Landing page: the two-path demo structure the whole frontend is built around."""
    st.header("SentinelIQ")
    st.caption("AI-Powered Vendor Risk Investigation")
    st.write(
        "SentinelIQ analyzes a company's legal/contractual, financial and "
        "security/compliance documentation, retrieves relevant evidence "
        "across the available document set, uses specialist risk agents plus "
        "a Red-Team agent to investigate risks and contradictions, applies "
        "deterministic risk scoring, and produces an evidence-grounded risk "
        "report."
    )

    left, right = st.columns(2)
    with left:
        st.subheader("View Existing Demo Results")
        st.caption(
            "A real, precomputed investigation (Meridian CloudWorks) — "
            "instant, no waiting."
        )
        if st.button("View Existing Demo Results", type="primary"):
            st.session_state["page"] = "Existing demo"
            st.rerun()
    with right:
        st.subheader("Start New Investigation")
        st.caption(
            "Upload your own Contract, Financial and/or Security documents. "
            "The real pipeline runs live and can take several minutes."
        )
        if st.button("Start New Investigation"):
            st.session_state["page"] = "New investigation"
            st.rerun()


def page_existing_demo() -> None:
    """Precomputed Demo Investigation — Meridian CloudWorks, instant."""
    st.header("Existing Demo Investigation")
    response = api_get("/api/demo/meridian")
    if failed(response, "Could not load the preloaded demo."):
        return
    report = response.json()
    if report.get("demo_note"):
        st.info(report["demo_note"])
    show_verdict(report)
    show_category_scores(report)
    show_why(report)
    show_findings(report)

    st.divider()
    st.subheader("Ask SentinelIQ about these documents")
    ask_documents(
        "/api/demo/meridian/questions", "the preloaded Meridian CloudWorks documents"
    )


def page_new_investigation_upload() -> None:
    """Upload Contract / Financial / Security documents for one company."""
    st.header("Start a new investigation")
    st.caption(
        "Recommended: upload all 3 document types for the most complete "
        "investigation. SentinelIQ can also run with one or two available "
        "document types; the resulting assessment will be based only on the "
        "evidence provided."
    )

    vendor_name = st.text_input(
        "Company / entity name", value=st.session_state.get("new_vendor_name", "")
    )
    st.session_state["new_vendor_name"] = vendor_name

    columns = st.columns(3)
    for column, (doc_type, label) in zip(columns, DOC_TYPE_LABELS.items(), strict=True):
        with column:
            st.markdown(f"**{label}**")
            uploaded = st.file_uploader(
                "PDF, DOCX or TXT",
                type=["pdf", "docx", "txt"],
                key=f"upload_{doc_type}",
            )
            if uploaded is not None and st.button(
                f"Add {label}", key=f"add_{doc_type}"
            ):
                if not vendor_name.strip():
                    st.error("Enter a company / entity name first.")
                else:
                    with st.spinner(f"Indexing {label}..."):
                        response = call(
                            "POST",
                            "/api/documents",
                            timeout=600,
                            data={
                                "vendor_name": vendor_name,
                                "document_type": doc_type,
                            },
                            files={"upload": (uploaded.name, uploaded.getvalue())},
                        )
                    if not failed(response, f"Could not upload the {label} document."):
                        st.success(
                            f"{label} added ({response.json()['chunk_count']} chunks)"
                        )

    if not vendor_name.strip():
        return
    group = api_get(f"/api/vendor-groups/{vendor_name}")
    if group is None or group.status_code != 200:
        st.info("Upload at least one document above to continue.")
        return

    body = group.json()
    st.divider()
    st.subheader("Documents ready")
    show_type_checklist(set(body["available_types"]))
    chunks = sum(d["chunk_count"] for d in body["documents"])
    st.caption(f"{len(body['documents'])} document(s), {chunks} chunks combined")

    st.divider()
    st.subheader("Run the investigation")
    show_pipeline()
    st.caption(
        "Retrieval is hybrid dense + BM25 fused with RRF and reranked by a "
        "cross-encoder. A specialist agent per document type answers each "
        "question, a Red-Team agent checks it against the evidence, and the "
        "risk score is plain Python - no LLM decides a number."
    )

    if st.button("Run investigation", type="primary"):
        started = api_post(f"/api/vendor-groups/{vendor_name}/investigate")
        if failed(started, "Could not start the investigation."):
            return
        investigation_id = started.json()["investigation_id"]
        st.session_state["new_investigation_id"] = investigation_id
        st.session_state["qa_vendor_name"] = vendor_name
        with st.spinner(
            "Indexing documents... running specialist agents... Red-Team... "
            "risk engine..."
        ):
            final = poll_until_finished(investigation_id)
        if final == "failed":
            state = api_get(f"/api/investigations/{investigation_id}/status")
            detail = state.json().get("error") if state else None
            st.error(f"The run failed. {detail or 'Check the API logs.'}")
            return

    investigation_id = st.session_state.get("new_investigation_id")
    if investigation_id:
        show_document_report(investigation_id)


def page_ask_new() -> None:
    """Ask questions about the documents uploaded in "Start new investigation"."""
    st.header("Ask SentinelIQ about these documents")
    vendor_name = st.session_state.get("qa_vendor_name")
    if not vendor_name:
        st.info("Upload documents and run a new investigation first.")
        return
    ask_documents(
        f"/api/vendor-groups/{vendor_name}/questions",
        f"the documents uploaded for **{vendor_name}**",
    )


METRIC_LABELS = {
    "citation_validity": ("Citation validity", "answerable"),
    "citation_accuracy": ("Citation accuracy", "answered"),
    "numeric_grounding": ("Numeric grounding (hallucination check)", "answered"),
    "retrieval_hit_rate": ("Retrieval hit rate", "answerable"),
    "abstention_rate_controls": ("Abstention on unanswerable controls", "controls"),
    "false_abstention_rate": ("False abstention", "answerable"),
    "abstention_accuracy_overall": ("Abstention accuracy", "total"),
}


def as_percent(value) -> str:
    """A metric as a reader sees it, or a dash when it was never measured."""
    return "—" if value is None else f"{value * 100:.1f}%"


def show_reliability_metrics(data: dict) -> None:
    """Each metric beside the group it was measured over, computed vs recorded.

    The two columns must not be collapsed: `computed` is recalculated from the
    stored per-question records, `recorded` is the audited figure quoted in the
    documentation. Showing both means a change in the aggregation is visible
    rather than silently rewriting history.
    """
    computed = data["computed"]
    recorded = data.get("recorded") or {}
    groups = computed.get("groups", {})

    rows = []
    for key, (label, group) in METRIC_LABELS.items():
        rows.append(
            {
                "metric": label,
                "computed": as_percent(computed.get(key)),
                "recorded": as_percent(recorded.get(key)),
                "measured over": f"{groups.get(group, '?')} {group}",
            }
        )
    st.table(rows)
    st.caption(
        "Each metric uses its own denominator — pooling them into one would "
        "misreport all of them."
    )


#: The judge scored only the answers the model actually produced. Spelling the
#: split out matters: 2 of the 19 are controls the model should have declined,
#: so this group is NOT the `answered` group in the deterministic table above.
JUDGE_SAMPLE = "n = 19 non-abstained answers (17 answerable + 2 controls)"


def show_judge_metrics(judge: dict) -> None:
    """Faithfulness and Answer Relevance, or why they are not shown (FR-021)."""
    st.subheader("Faithfulness and Answer Relevance")
    if judge.get("available"):
        st.table(
            [
                {"metric": name, "score": as_percent(value)}
                for name, value in judge["scores"].items()
            ]
        )
        st.caption(f"Judged by {judge.get('model')} — {JUDGE_SAMPLE}.")
        st.info(
            "**Stage 8 single-agent baseline only — not a multi-agent "
            "comparison.** The Stage 9 multi-agent answers carry no judge "
            "scores, so these numbers say nothing about which path is better.\n\n"
            "These scores describe answer quality **when the model chose to "
            "answer**, not end-to-end performance: 19 of the 35 questions are "
            "scored here, the other 16 are abstentions with nothing to score, "
            "and 13 of those were answerable. Answer "
            "Relevance of 100% is not evidence of perfect quality — this judge "
            "is generous to genuine attempts even though it rejects bad ones. "
            "Read these beside the deterministic metrics above, which win "
            "where the two disagree (ADR-019)."
        )
        show_rejected_judge(judge.get("rejected_history"))
        return
    st.warning(
        "Not available. These need an LLM judge, and the judge run stored in "
        "the results file is marked invalid, so its scores must not be quoted "
        "as model performance."
    )
    if judge.get("model"):
        st.caption(f"Recorded judge: {judge['model']} — {judge.get('reason', '')}")


def show_rejected_judge(history: dict | None) -> None:
    """Keep the rejected judge run visible next to the valid one (ADR-020)."""
    if not history:
        return
    with st.expander("Earlier judge run, rejected"):
        st.caption(f"{history.get('model')} — {history.get('reason', '')}")


def page_reliability() -> None:
    """The measured quality of the system itself."""
    st.header("AI reliability")
    response = api_get("/api/evaluations")
    if response is None:
        return
    if response.status_code == 404:
        st.info("No evaluation results have been recorded yet.")
        return
    if failed(response, "Could not load the reliability metrics."):
        return
    data = response.json()

    st.subheader("Generation quality")
    show_reliability_metrics(data)
    show_judge_metrics(data.get("judge") or {})

    with st.expander("Raw figures"):
        st.json(data)

    st.caption(
        "These describe the system's own measured quality on the CUAD DEV "
        "benchmark, not any tenant's data."
    )


def main() -> None:
    """Render the app.

    This is an anonymous showcase: no sign-in screen, no token handling. The
    API's own JWT/RBAC is untouched underneath — the frontend just never calls
    it, and relies on SENTINELIQ_DEMO_MODE=true on the API instead (see
    `headers()`).
    """
    st.set_page_config(page_title="SentinelIQ", page_icon="🛡️", layout="wide")
    st.title("SentinelIQ")
    st.caption("AI-powered vendor risk investigation")

    health = api_health()
    if health is None:
        st.error(f"Cannot reach the API at {API_URL}. Is it running?")
        return
    if not health.get("demo_mode"):
        st.error(
            "This showcase frontend needs the API running with "
            "SENTINELIQ_DEMO_MODE=true. Set it and restart the API."
        )
        return

    pages = {
        "Home": page_home,
        "Existing demo": page_existing_demo,
        "New investigation": page_new_investigation_upload,
        "Ask about new investigation": page_ask_new,
    }
    advanced = {
        "Dashboard": page_dashboard,
        "Investigate a curated vendor": page_new_investigation,
        "Investigation report": page_investigation,
        "AI reliability": page_reliability,
    }

    current = st.session_state.get("page", "Home")
    choice = st.sidebar.radio(
        "Page", list(pages), index=list(pages).index(current) if current in pages else 0
    )
    st.session_state["page"] = choice

    with st.sidebar.expander("Advanced"):
        advanced_choice = st.radio("Advanced pages", ["(none)"] + list(advanced))

    if advanced_choice != "(none)":
        advanced[advanced_choice]()
    else:
        pages[choice]()


main()
