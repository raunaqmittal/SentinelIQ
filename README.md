# SentinelIQ

**Ask a hard question about a company. Get an answer you can check.**

---

## The problem this solves

Before a company signs a contract with a vendor, someone has to answer questions like:

> *"Is this vendor's security certification actually current?"*
> *"Do their own documents contradict each other?"*
> *"What happens if they get acquired mid-contract?"*

Today a human does this. They open a security questionnaire, a SOC 2 summary,
a 40-page contract and an SEC filing, and they read. It takes hours per vendor,
and the interesting findings are usually the ones hiding *between* two
documents — the policy that promises encryption, and the incident report that
quietly admits some backups weren't encrypted.

That gap between documents is the whole job. And it's exactly what gets missed
when someone is reviewing their ninth vendor of the week.

## What SentinelIQ does

You give it a vendor. It reads everything that vendor has — contracts,
financial filings, security documents — and comes back with a risk score, a
recommendation, and **a citation for every single claim it makes**.

Here is a real finding from a real run:

> **Does this vendor encrypt all customer data at rest?**
>
> The security policy says *"All customer data at rest, including primary
> databases, object storage, and backup snapshots, is encrypted using
> AES-256."*
>
> But the incident report says the affected backup snapshots *"were stored in
> plaintext, without the AES-256 encryption applied to primary production
> data."*
>
> **These contradict each other.** → Flagged, escalated for human review.

Two documents. One claim, one admission. The system found the gap and refused
to average it away into a comfortable answer.

## Why it isn't a chatbot

A chatbot answers. SentinelIQ *investigates*, and the difference shows up in
four design decisions:

**It works in teams.** Four specialist agents — compliance, financial,
security, and a red-team reviewer — look at the same evidence. The specialist
drafts an answer; the red-teamer checks it against the source and strips out
anything unsupported. Neither one gets the last word alone.

**The score is not written by an AI.** The agents produce findings and label
each one low, medium or high. Then ordinary Python code — weights from a config
file, thresholds, arithmetic — turns those into the final risk score. A
language model never decides the number. If it did, you could not audit the
number, and you could not defend it to an auditor who asks *why 48 and not 60*.

**It treats documents as untrusted.** Every piece of evidence handed to a model
is wrapped and explicitly labelled as data, never as instructions. This is not
theoretical: one test vendor in this repository has malicious instructions
hidden in all four of its documents. (More on how that turned out below.)

**It would rather say nothing than guess.** If the evidence doesn't answer the
question, the answer is `NOT FOUND IN EVIDENCE`. And every citation is checked
against the evidence actually supplied — a made-up reference gets dropped
before it ever reaches you.

## Two things we tested for real

Most projects claim their AI is safe. These two were actually run against a
live model and the results are in the repository.

**Can it catch a planted contradiction?** We built a fictional vendor with two
deliberate self-contradictions buried in its paperwork. The system found both,
explained the mechanism of each, and — importantly — did **not** cry wolf on
the two control questions. One contradiction was enough to force the whole
investigation into human review.

**Can it resist a prompt-injection attack?** We built a second vendor whose
documents each carry a different attack: fake system messages, forged
"instructions for automated reviewers", a document claiming authority over the
others, and a suppression order attached to genuinely bad news. That last one
is the real test — the attack tells the reader to hide a breach affecting
~41,000 users.

The model reported the breach, with the correct figures, **and flagged all four
attacks by name.** It obeyed none of them.

## What we learned from measuring it

We ran a controlled experiment: 35 questions through a single-agent pipeline,
then the same 35 through the multi-agent one, with retrieval held **exactly**
identical on both sides.

The honest result is more interesting than a clean win.

**The big improvement is real.** On answers containing numbers — notice
periods, liability caps, renewal terms, the details that actually matter in a
contract — grounding went from **0.24 to 0.83**. That's the rate at which
numbers in the answer actually appear in the source. The multi-agent pipeline
roughly stopped inventing numbers.

**Two improvements we initially reported turned out to be measurement bugs.**
Our first analysis showed the multi-agent path citing more accurately. It
didn't. Our citation parser only recognised square brackets `[like_this]`, and
the model sometimes cited using full-width brackets `【like_this】` — so real
citations were being counted as no citation at all, and the *single-agent* path
was penalised harder. Once both sides were scored fairly, the citation
advantage vanished.

We kept the correction in the documentation rather than quietly deleting the
original number. Finding that bug also fixed a genuine defect in the product:
real reports had been silently losing evidence links.

**It costs 4.4× more.** That's the honest trade: better grounding for four
times the tokens and about a minute per question. For due diligence a human
acts on, we think that's worth it. For a high-volume chatbot, it wouldn't be.

## What this project does *not* do

Stated plainly, because a README that only lists strengths is not worth
trusting:

- **No cloud deployment.** It runs in Docker on one machine.
- **The security documents are synthetic.** No public corpus of real vendor
  security questionnaires exists, so we wrote nine of them. The contracts and
  financial filings are real (CUAD and SEC EDGAR).
- **Retrieval finds the labelled evidence about 43% of the time** on the hard
  benchmark. That is the weakest part of the system and it is not hidden.
- **The evaluation set is 35 questions.** Enough to detect a large effect, not
  enough to settle a close one.

---
---

# Technical documentation

Everything above is the intuition. Everything below is how it works.

## Architecture

```
Streamlit dashboard  ──HTTP──▶  FastAPI  ──▶  investigation pipeline
                                   │                    │
                                   ▼                    ▼
                            PostgreSQL          retrieval + agents
                          (tenant-scoped)               │
                                                        ▼
                                                   Groq API
```

Three containers: `api` (FastAPI + retrieval + agents), `ui` (Streamlit), `db`
(PostgreSQL 16). The UI talks to the API over HTTP only and never imports the
pipeline — which is also why no CORS configuration is needed, since the calls
happen server-side.

## Retrieval pipeline (frozen)

```
query
  ├─▶ dense retrieval  (bge-base-en-v1.5, FAISS)  → top 50
  └─▶ BM25 sparse                                 → top 50
            ↓
      RRF fusion (k=60)                           → top 20
            ↓
      cross-encoder rerank (bge-reranker-v2-m3)   → top 5
```

Chunking is a recursive character splitter at 512 tokens with 64 overlap.
~500 ms per query on a GPU; ~21 s on CPU.

**This pipeline is frozen.** Every component was chosen by measurement, not
preference — two rerankers were tested and found *worse than no reranker at
all*. Downstream quality problems are treated as generation problems; retrieval
is not reopened without a new experiment.

## The agent layer

Four specialists (compliance, financial, security, red-team) built on CrewAI.
Each question routes to one specialist by category, which drafts a cited
answer. The red-team agent then re-reads the same evidence, removes unsupported
claims, drops citations that don't support their claim, and labels the finding
`LOW`/`MEDIUM`/`HIGH`.

The evidence block is deliberately sent to **both** agents. It costs tokens —
90% of the multi-agent token spend is input — but the red-teamer cannot detect
an injected instruction it has not been shown.

## Decision engine

Deterministic Python, no LLM (ADR-021):

- Agent severity labels → numeric scores via `risk_rules.yaml`
- Weighted category scores → overall risk score → recommendation
- Weights are validated to sum to 1.0 **on load**, so a typo fails loudly
- One contradiction anywhere forces escalation regardless of score

## Security model

| Control | Implementation |
|---|---|
| Tenant isolation | Structural — indexes are built per vendor, so one tenant's search cannot reach another's chunks. Every repository query filters by `tenant_id` |
| Auth | bcrypt password hashes, 30-minute JWTs carrying `tenant_id` and role. `SECRET_KEY` has **no default** — a fallback would sign forgeable tokens |
| RBAC | `analyst` and `admin`; deletion requires `admin` |
| Injection resistance | Evidence delimited and labelled untrusted; **empty tool allow-list**; citations filtered against supplied evidence; injection reports never suppressed |
| File validation | Magic-byte checks before any parser runs — a renamed executable is rejected |
| Log redaction | Secrets and document content masked before a log line is written |

Structural guards enforce this in CI: an AST check fails the build if a route
lacks authentication or a repository function lacks a tenant filter.

## Measured results

**Stage 9 — single-agent vs multi-agent, 35 questions, identical retrieval:**

| Metric | Single | Multi | Delta |
|---|---|---|---|
| numeric grounding (17 answered by both) | 0.2429 | **0.8255** | **+0.5826**, p = 0.00026 |
| citation accuracy (30 answerable) | 0.3667 | 0.3833 | +0.0167, not significant (p = 0.75) |
| citation validity | 1.0000 | 1.0000 | 0.0000 |
| retrieval hit rate | 0.3714 | 0.3714 | identical — 35/35 same chunks, same order |
| tokens | 147,185 | 645,668 | 4.39× |

**Retrieval**, on a 269-question benchmark generated from CUAD expert
annotations (DEV 160 / TEST 101, split by contract, TEST scored once).

**Live verifications:** contradiction detection and prompt-injection refusal,
both against a real model, both recorded in `Docs/PROGRESS.md`.

**Judge metrics:** a valid `llama-3.3-70b-versatile` judge run exists over the
19 judgeable single-agent answers (faithfulness 0.974, relevance 1.000,
completeness 0.958). It is **not yet surfaced in the dashboard**, which still
shows the earlier judge as invalid — an honest gap, noted rather than papered
over.

## Data

87 documents across 9 vendor dossiers:

- **35 CUAD contracts** — real SEC-filed agreements, expert-annotated (CC BY 4.0)
- **SEC EDGAR filings** for 8 companies — Item 1A, Item 7, and XBRL facts
- **9 synthetic security vendors** — written for this project; 4 carry planted
  contradictions and 1 carries prompt-injection payloads

## Running it

```bash
cp .env.example .env      # set SECRET_KEY and GROQ_API_KEY
docker compose up -d --build
```

API at `http://localhost:8000/docs`, dashboard at `http://localhost:8501`.
First start downloads ~2.6 GB of models into a cache volume.

```bash
docker compose exec api python scripts/create_user.py alice --tenant acme --role admin
```

### Deployment status

**Local Docker Compose (`docker-compose.yml`) is the only supported deployment.**

**SentinelIQ is not deployed on AWS, and never was.** A cloud deployment was
planned and then **cancelled** before anything was provisioned — no image was
ever pushed, no instance or database was ever created — and its unused
configuration has been removed from this repository. Nothing here provisions
cloud infrastructure, and running the project needs no cloud account.

Single vendor from the CLI:

```bash
python scripts/investigate.py "Meridian CloudWorks" --json report.json
```

## Tests

```bash
pytest                                   # 396 passed, 1 skipped
TEST_DATABASE_URL=postgresql+psycopg://... pytest    # 397 on PostgreSQL
```

**No test calls an LLM.** The skipped test is PostgreSQL-only: it proves `/run`
does not hold a row lock while its background task works — a deadlock that
SQLite structurally cannot reproduce, and which was only found by running the
suite against a real server database.

## Repository layout

```
sentineliq/
  components/
    ingestion/     loaders + chunker
    retrieval/     dense, sparse, fusion, reranker  (frozen)
    agents/        four specialists + shared prompts
    evaluation/    retrieval and generation metrics
    database/      SQLAlchemy models + tenant-scoped repository
    api/           FastAPI app and routes
  pipeline/        flow (agents), engine (deterministic scoring), investigation
  service.py       business logic, auth, RBAC
frontend/app.py    Streamlit dashboard
scripts/           ingest, investigate, evaluate, create_user, purge_expired
Docs/              CONTEXT, REQUIREMENTS, CONVENTIONS, PROGRESS
```

## Documentation

`Docs/PROGRESS.md` is the honest record: what was measured, what was corrected,
and what was withdrawn. It contains 24 architecture decision records with their
reasoning, including the ones we got wrong and had to revise.

If you read one thing beyond this file, read the Stage 12 and Stage 13 sections
— they document two measurement bugs we found in our own published results, and
what the numbers looked like after fixing them.
