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
POLL_SECONDS = 3

RISK_COLOURS = {
    "low": "green",
    "medium": "orange",
    "high": "red",
    "critical": "red",
}


def headers() -> dict:
    """Authorization header, empty when signed out."""
    token = st.session_state.get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def call(method: str, path: str, **kwargs):
    """Call the API, or return None after showing the user what went wrong.

    Every caller gets one of two things: a successful response, or None with a
    message already on screen. That keeps the page functions free of error
    handling and means an unreachable API never shows a raw traceback.
    """
    try:
        response = requests.request(
            method, f"{API_URL}{path}", headers=headers(), timeout=TIMEOUT, **kwargs
        )
    except requests.Timeout:
        st.error("The API took too long to respond. It may still be working.")
        return None
    except requests.RequestException:
        st.error(f"Cannot reach the API at {API_URL}. Is it running?")
        return None

    # A 401 means the 30-minute token has expired; send the user back to login.
    if response.status_code == 401:
        st.session_state.pop("token", None)
        st.warning("Your session has expired. Please sign in again.")
        st.rerun()
    if response.status_code == 403:
        st.error("You do not have permission to do that.")
        return None
    return response


def api_get(path: str):
    """GET from the API with the signed-in user's token."""
    return call("GET", path)


def api_post(path: str, **kwargs):
    """POST to the API with the signed-in user's token."""
    return call("POST", path, **kwargs)


def failed(response, message: str) -> bool:
    """True when the call did not succeed; shows `message` if so."""
    if response is None:
        return True
    if response.status_code >= 400:
        st.error(f"{message} ({response.status_code})")
        return True
    return False


def sign_in_form() -> None:
    """Sidebar sign-in."""
    st.sidebar.subheader("Sign in")
    username = st.sidebar.text_input("Username")
    password = st.sidebar.text_input("Password", type="password")
    if st.sidebar.button("Sign in"):
        try:
            response = requests.post(
                f"{API_URL}/api/auth/login",
                json={"username": username, "password": password},
                timeout=30,
            )
        except requests.RequestException:
            st.sidebar.error(f"Cannot reach the API at {API_URL}")
            return
        if response.status_code == 200:
            st.session_state["token"] = response.json()["access_token"]
            st.session_state["username"] = username
            st.rerun()
        else:
            st.sidebar.error("Incorrect username or password")


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
                "vendor": row["vendor_name"],
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
        st.caption(f"Judged by {judge.get('model')}.")
        return
    st.warning(
        "Not available. These need an LLM judge, and the judge run stored in "
        "the results file is marked invalid, so its scores must not be quoted "
        "as model performance."
    )
    if judge.get("model"):
        st.caption(f"Recorded judge: {judge['model']} — {judge.get('reason', '')}")


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
    """Render the app."""
    st.set_page_config(page_title="SentinelIQ", page_icon="🛡️", layout="wide")
    st.title("SentinelIQ")
    st.caption("Evidence-backed vendor due diligence")

    if not st.session_state.get("token"):
        sign_in_form()
        st.info("Sign in from the sidebar to continue.")
        return

    st.sidebar.success(f"Signed in as {st.session_state['username']}")
    if st.sidebar.button("Sign out"):
        st.session_state.clear()
        st.rerun()

    pages = {
        "Dashboard": page_dashboard,
        "Start an investigation": page_new_investigation,
        "Investigation report": page_investigation,
        "AI reliability": page_reliability,
    }
    choice = st.sidebar.radio("Page", list(pages))
    pages[choice]()


main()
