# QueueStorm Investigator

A FastAPI fintech support copilot. It receives a customer support ticket
(complaint text + transaction history), **investigates** what actually happened
by cross-referencing the complaint against the transaction record, and returns a
structured JSON verdict.

**Core stance:** it is an *investigator, not a classifier*. The complaint is
**untrusted data**, the transaction history is the **source of truth**, and the
service defaults to caution (`insufficient_data` / human review) over confident
guesses.

---

## Quick Start

> Run these from inside the `queuestorm/` directory (Python 3.11).

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your GROQ_API_KEY into .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then check it's live:

```bash
curl http://localhost:8000/health        # -> {"status":"ok"}
```

**Or with Docker:**

```bash
docker build -t queuestorm .
docker run -p 8000:8000 -e GROQ_API_KEY=gsk_your_key_here queuestorm
```

> The service runs **without** a `GROQ_API_KEY` too — it just falls back to
> safe, deterministic template text instead of LLM-generated prose.

---

## API Reference

### `GET /health`

Liveness probe. No auth, no body.

```json
{ "status": "ok" }
```

### `POST /analyze-ticket`

Investigate a ticket and return the structured verdict.

**Request**

```json
{
  "ticket_id": "TKT-FULL-001",
  "complaint": "I sent 5000 taka to wrong number at 2pm",
  "language": "en",
  "transaction_history": [
    {
      "transaction_id": "TXN-9101",
      "timestamp": "2026-04-14T14:08:22Z",
      "type": "transfer",
      "amount": 5000,
      "counterparty": "+8801719876543",
      "status": "completed"
    }
  ]
}
```

Only `ticket_id` and `complaint` are required. `transaction_history` defaults to
`[]`. Optional fields: `language`, `channel`, `user_type`, `campaign_context`,
`metadata`.

**Response** `200 OK`

```json
{
  "ticket_id": "TKT-FULL-001",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "Customer transferred 5000 Taka to an incorrect phone number (+8801719876543) on 2026-04-14 at 14:08 UTC; the transfer is completed and classified as a wrong_transfer with high severity.",
  "recommended_next_action": "Escalate the case to the dispute_resolution team for manual review, attach the transaction details, and open a formal dispute request while informing the customer that any eligible resolution will be handled through official channels.",
  "customer_reply": "We're sorry to hear about the mistaken transfer. Our dispute resolution team is reviewing the transaction, and any eligible resolution will be handled through official channels.",
  "human_review_required": true,
  "confidence": 0.9,
  "reason_codes": ["wrong_transfer_keyword", "consistent_evidence", "elevated_value", "amount_match"]
}
```

**Error envelope** — every error returns a safe, leak-free body (never a stack
trace, secret, or internal path):

```json
{ "error": "unprocessable_entity", "detail": "complaint must not be empty" }
```

| Condition | Status |
|---|---|
| Missing field / wrong type | `400` |
| Empty/whitespace complaint (semantic) | `422` |
| Unhandled server error | `500` (generic message) |

Interactive docs are served at `/docs` (Swagger) and `/openapi.json`.

---

## Tech Stack

- **Python 3.11**
- **FastAPI** + **Uvicorn** (ASGI server)
- **Pydantic v2** — request/response schema, enum validation
- **Groq Python SDK** — LLM text generation
- **python-dotenv** — local env loading
- **httpx** — HTTP client (Groq SDK dependency / TestClient)
- **Docker** + **Railway** — containerization & deployment

---

## MODELS

| Model | Where it runs | Used for | Why chosen |
|---|---|---|---|
| `openai/gpt-oss-120b` | **Remote — Groq API** (LPU inference) | Writing the three free-text fields **only** (`agent_summary`, `recommended_next_action`, `customer_reply`) | Very fast inference (fits the 10s budget with room to spare), strong instruction-following / JSON adherence, cost-effective, open-weight |

**No other ML models.** There are **no embeddings, no local weights, and nothing
downloaded at build or runtime** — which is why the image stays small and cold
start is fast. All investigation logic is deterministic Python.

LLM call parameters (see `llm.py`): `max_tokens=500`, `temperature=0.3`,
`reasoning_effort=low`, **hard 10-second timeout** via `asyncio.wait_for`.

---

## AI Approach

**The LLM writes text. The code makes every decision.**

```
complaint + transaction_history
        │
        ▼
┌─────────────────────────── deterministic rule-based code ───────────────────────────┐
│  find_relevant_transaction → get_evidence_verdict → classify_case →                  │
│  severity → department → calculate_confidence → reason_codes                          │
└──────────────────────────────────────────────────────────────────────────────────────┘
        │  (all decisions fixed)
        ▼
   LLM (Groq)  ──►  agent_summary / recommended_next_action / customer_reply
        │           (receives the computed facts as TRUSTED; complaint as UNTRUSTED)
        ▼
   safety sanitization  ──►  structured JSON response
```

- Every **decision** (which transaction matched, the verdict, case type,
  severity, routing department, confidence, reason codes) is computed by
  deterministic regex/rule code. The complaint never influences these — only its
  *keywords* feed matching; the text is data, not instructions.
- The LLM receives the already-computed facts and only turns them into readable
  prose. It cannot change a verdict or an enum.
- **Graceful degradation:** on any LLM failure — missing key, API error, timeout
  (>10s), or malformed JSON — the service returns deterministic, always-safe
  template text instead. The API contract is identical either way.

---

## Safety Logic

Three non-negotiable rules, enforced in **defense-in-depth** layers
(`safety.py`):

1. **Never request credentials** — no PIN, OTP, password, card number, or CVV,
   not even "for verification."
2. **Never promise a refund / reversal / unblock / recovery** — outbound text may
   only say *"any eligible resolution will be handled through official channels."*
3. **Never direct the customer to a third party** — no external phone numbers,
   WhatsApp/Telegram handles, or non-official URLs.

**How each is enforced (every layer must pass):**

- **Input isolation** — the complaint is wrapped as untrusted data in the prompt;
  `is_prompt_injection()` *flags* manipulation attempts (e.g. "ignore previous
  instructions") in `reason_codes` but **never blocks** processing. Injection also
  forces the customer reply to the safe fallback.
- **System prompt** — the model is given the three rules as absolute constraints
  and told the complaint is untrusted.
- **Output sanitization** — `sanitize_customer_reply()` and
  `sanitize_recommended_action()` scan generated text with regex patterns
  covering **English, Bangla, and Banglish**. Any hit replaces the whole reply
  with `SAFE_FALLBACK_REPLY` and records the violation as a reason code
  (`safety_filter_triggered` + the specific violation).
- **Always-safe fallback** — the templated fallback text is safe by construction,
  so even a total LLM outage cannot produce an unsafe customer-facing reply.

The customer-facing reply is checked **after** generation, so no model output can
bypass the filter.

---

## Evidence Reasoning

**Transaction matching** (`find_relevant_transaction`) — a weighted, rule-based
scorer ties a complaint to at most one transaction (or to nothing):

| Signal | Weight |
|---|---|
| Amount exact match / within 10% | +3 / +1 |
| Type keyword → `txn.type` | +2 |
| Failure word + `failed`/`pending` status | +2 |
| Failure word + `completed` status (the inconsistent case) | +1 |
| Success word + `completed` status | +2 |
| Time hint within ±3h window | +2 |
| Counterparty match (BD phone formats normalized) | +3 |

A transaction is returned only if its score **≥ 2** — we never force a weak
match. Amounts are **currency-anchored** (a bare number with no taka/৳/`k`/number
word is not treated as money, so phone digits and IDs aren't mistaken for
amounts).

**Verdict** (`get_evidence_verdict`) → `consistent` | `inconsistent` |
`insufficient_data`:

- No match → `insufficient_data`.
- **Inconsistent is checked before consistent**: complaint says *failed* but
  record says *completed* (or says *sent* but record says *failed*) → the
  complaint contradicts the record.
- Consistent: failure claim + `failed`/`pending`; wrong-recipient + completed
  transfer; refund request with a real transaction.
- When unsure → `insufficient_data`. We never assert a verdict we can't support.

**Confidence** is then derived deterministically from `(verdict, matched)`:
`0.90` consistent+match, `0.75` inconsistent+match, `0.60` insufficient+match,
`0.45` otherwise.

---

## Known Limitations

- **Timezone-naive time matching.** Time hints like "2pm" are compared against the
  transaction timestamp's wall-clock time within a ±3h window. A complaint and a
  ledger recorded in different timezones can mis-match.
- **Keyword-based classification.** Case routing and matching rely on keyword
  patterns, not semantic understanding — novel phrasings, sarcasm, or unusual
  transliterations can be missed or misrouted.
- **Duplicate-payment verdict is conservative.** Confirming a true duplicate needs
  correlating two transactions, which the single-match verdict path doesn't fully
  thread yet — such cases fall through to `insufficient_data` (safe but cautious).
- **Bangla/Banglish coverage is pattern-based, not exhaustive.** Safety and
  matching regexes cover common spellings; uncommon transliterations may slip
  past. New patterns are easy to add but are not auto-learned.
- **`agent_summary` is not re-sanitized.** It's internal-only (never shown to the
  customer), so the safety filter is applied to the customer reply and the
  recommended action but not the internal summary — a deliberate scope choice.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | Recommended | — | Groq API key (`gsk_…`) for LLM text generation. **Without it the service still runs**, falling back to safe deterministic templates. |
| `PORT` | No | `8000` | Port the server binds to. Railway injects this automatically; the container honors `${PORT}`. |

**Local:** put these in `queuestorm/.env` (gitignored — never committed).
**Railway:** set them in the service's **Variables** tab (do not use a `.env`
file in production).

---

## Deploy to Railway

The repository's app lives in the `queuestorm/` subdirectory, and a
`railway.json` there pins the Docker build and a `/health` healthcheck.

1. **New Project → Deploy from GitHub repo**, pick this repo and the `main` branch.
2. Open the service → **Settings → Build** and set **Root Directory** to
   `queuestorm`. (This is the one required setting — it makes Railway find
   `queuestorm/Dockerfile` and `queuestorm/railway.json`.)
3. **Settings → Variables**: add `GROQ_API_KEY = gsk_…`. Do **not** set `PORT` —
   Railway injects it automatically and the container honors `${PORT}`.
4. Deploy. Railway builds the Dockerfile (`python:3.11-slim`, no secrets, no model
   weights) and runs `uvicorn main:app --host 0.0.0.0 --port ${PORT}`.
5. Once live, verify:

   ```bash
   curl https://<your-app>.up.railway.app/health        # -> {"status":"ok"}
   ```

The healthcheck (`/health`) and an `ON_FAILURE` restart policy are configured in
`railway.json`, so a failed deploy won't be promoted and crashes auto-restart.
