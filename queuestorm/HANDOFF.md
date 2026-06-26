# QueueStorm Investigator — Build Handoff

**Last updated:** 2026-06-26
**Purpose:** Continue this build in a fresh session without re-deriving context.

---

## 1. What this project is

A **FastAPI fintech support copilot**. It receives a support ticket (untrusted
complaint text + transaction history) and returns a structured JSON verdict by
cross-referencing the complaint against the transaction record.

**Two endpoints only:**
- `GET /health` → `{"status":"ok"}`
- `POST /analyze-ticket` → full investigation response

**Core mindset (drives every design choice):**
- It's an **investigator, not a classifier**.
- Complaint text is **UNTRUSTED DATA**, never instructions.
- Transaction history is the **SOURCE OF TRUTH**.
- **Default to caution** — prefer `insufficient_data` / human review over confident guesses.

**Hackathon:** SUST CSE Carnival 2026 Codex Community Hackathon.
**Deploy target:** Railway.

### Absolute safety rules (non-negotiable — 2 violations = disqualification)
1. NEVER ask the customer for PIN / OTP / password / card number.
2. NEVER promise a refund / reversal / account unblock / recovery.
3. NEVER direct the customer to a third party.

### Scoring rubric
| Weight | Area |
|---|---|
| 35% | Evidence Reasoning (biggest lever) |
| 20% | Safety & Escalation (DQ risk) |
| 15% | API Contract & Schema |
| 10% | Performance & Reliability |
| 10% | Response Quality |
| 5% | Deployment |
| 5% | Documentation |

---

## 2. Environment & how to run (READ FIRST — Windows gotchas)

- **Project dir:** `F:\SUST Codex Hackathon\mainProject\queuestorm\`
- **Python:** use **3.12** (`py -3.12`). A venv exists at `queuestorm\.venv` (3.12.10).
  - ⚠️ The machine's default is **Python 3.14**, which **cannot install the pinned deps**
    (`pydantic-core 2.20.1` and other compiled wheels have no cp314 build). Spec targets
    3.11; 3.12 is the closest available that works. **Docker will pin 3.11.**
- **All pinned deps are installed** in the venv (fastapi 0.115.0, uvicorn 0.30.6,
  pydantic 2.8.2 / pydantic-core 2.20.1, anthropic 0.34.2, httpx 0.27.2, python-dotenv 1.0.1).

**Run the server:**
```powershell
& "F:\SUST Codex Hackathon\mainProject\queuestorm\.venv\Scripts\python.exe" -m uvicorn main:app `
  --app-dir "F:\SUST Codex Hackathon\mainProject\queuestorm" --host 0.0.0.0 --port 8000
```
or from inside `queuestorm\`: `.\.venv\Scripts\activate` then `uvicorn main:app --reload`.

**Windows gotchas that cost time — don't repeat them:**
1. PowerShell `curl` is an alias for `Invoke-WebRequest`. **Use `curl.exe`.** For POST
   bodies, pipe a single-quoted JSON string to `curl.exe ... --data-binary "@-"` (avoids
   quote-mangling).
2. **PowerShell stdin → Python mangles UTF-8** (Bangla becomes `?`). Do **not** verify
   Unicode logic by piping a here-string to `python -`. Instead write a temp `.py` file
   (UTF-8, via the editor) and run it with `PYTHONUTF8=1`. This is the verification
   pattern used throughout (throwaway `_verify_*.py`, run, then delete).
3. To free port 8000 after a background server:
   `Get-NetTCPConnection -LocalPort 8000 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`
   (a forced stop reports "exit 255" — that's expected, not a failure).

---

## 3. File status

| File | Status | Notes |
|---|---|---|
| `models.py` | ✅ Complete | Pydantic v2 models + enums |
| `main.py` | ✅ Complete | App, 2 routes, exception handlers, CORS, PORT |
| `analyzer.py` | ✅ Complete (rule-based) | Extractors + matcher + verdict + classifier + wired `analyze()` |
| `safety.py` | ✅ Complete | All patterns + functions. **NOT yet integrated into analyzer.py** |
| `requirements.txt` | ✅ Complete | 6 pinned packages |
| `.env.example` | ✅ Complete | `ANTHROPIC_API_KEY=` |
| `llm.py` | ⛔ Empty (docstring only) | Claude text generation — TODO |
| `Dockerfile` | ⛔ Placeholder comment | TODO (pin python:3.11) |
| `README.md` | ⛔ Placeholder | TODO |

---

## 4. What's built, phase by phase

### Phase 1 — Skeleton & contract ✅
- Project tree, pinned `requirements.txt`, `.env.example`.
- `models.py`: `TransactionEntry` (all fields optional), `TicketRequest`
  (`ticket_id`, `complaint` required; validator rejects empty/whitespace complaint),
  `TicketResponse`, `ErrorResponse`. Enums are `Literal[...]`. Each has a
  `json_schema_extra` example.
- `main.py`: FastAPI app (`title="QueueStorm Investigator"`, `version="1.0.0"`),
  `GET /health`, `POST /analyze-ticket`, CORS (all origins, `allow_credentials=False`),
  `PORT` env (default 8000, binds `0.0.0.0`).
  - **Exception handling:** structural validation (missing/wrong type) → **400**;
    semantic (empty complaint, raised as Pydantic `value_error`) → **422**; business
    `ValueError` → 422; everything else → 500 (generic, server-side log only).
    `HTTPException` passes through. **No error response ever leaks stack traces /
    exception text / paths / secrets.**
- **Verified live:** health 200; basic ticket 200 w/ all fields; missing complaint 400;
  whitespace complaint 422.

### Phase 2 — Core Investigation Engine (ZERO LLM) ✅
All in `analyzer.py`, all pure Python, all verified via throwaway scripts.

**Extractors:**
- `extract_amounts(text) -> List[float]` — `5000 taka`, `5,000 taka`, `৳5000`,
  `BDT 5000`, Bangla numerals, `5k`/`50k`, `five thousand taka`. **Amounts are
  currency-anchored** (a bare number with no currency/`k`/number-word is NOT an amount).
- `extract_time_hints(text) -> List[str]` — `2pm`, `2 PM`, `14:00`, `2:30pm`, `at 2`,
  `around 2`, Bangla day-parts (`সকালে`→09:00, `দুপুরে`→12:00, …). Normalized `"HH:MM"`.
  Bare hour is literal (`at 2`→`02:00`).
- `is_within_time_window(hint, ts, window_hours=3) -> bool` — empty hint→True;
  unparseable ts→False; 24h wrap-around.
- `extract_counterparty_hints(text) -> List[str]` — BD phones (`01…`, `8801…`,
  `+8801…`, Bangla digits) + merchant ids (`MID-123`, `merchant 456`).

**`find_relevant_transaction(complaint, transactions) -> Optional[str]`** — weighted scorer:
| Signal | Weight |
|---|---|
| amount exact / within 10% | +3 / +1 |
| type keyword → `txn.type` | +2 |
| failure word + `failed`/`pending` | +2 |
| failure word + `completed` | +1 (inconsistent case) |
| success word + `completed` | +2 |
| time hint within window | +2 |
| counterparty match | +3 |
- Returns best match's id if **score ≥ 2**, else None. Phishing words + empty history → None.
- Counterparty matching **normalizes BD phone formats** (compares last 10 digits).

**`get_evidence_verdict(complaint, matched_txn) -> "consistent"|"inconsistent"|"insufficient_data"`**
- None match → insufficient_data. Inconsistent checked **before** consistent.
- inconsistent: failure word + completed; success word + failed.
- consistent: failure word + failed/pending; wrong-recipient + transfer + completed;
  refund request + txn exists.
- Else insufficient_data.

**`classify_case(complaint, matched_txn, evidence_verdict) -> (case_type, department, severity, human_review_required, reason_codes)`**
- STEP 1: 8 case types by **keyword priority** (first match wins), each with a department.
  Uses **word-boundary (`\b`) matching**.
- STEP 2: severity cascade (critical: phishing or amount≥50k; high: amount≥10k or
  wrong_transfer or inconsistent; medium: amount≥1k or payment_failed/duplicate; else low).
- STEP 3: `human_review_required` — auto-resolvable only when fully benign
  (consistent + low/medium + case∈{refund_request,payment_failed,other} + txn present + amount<5000).
- STEP 4: 2–4 `reason_codes`.

**`analyze(ticket)`** — orchestrates the pipeline and composes the response with
**safe deterministic text templates** (no LLM): `customer_reply`, `recommended_next_action`,
`agent_summary` per case_type. Confidence is a heuristic placeholder (0.35/0.5/0.8/0.85).

**Verified live** (acceptance tests):
- "sent 5000 taka to wrong number at 2pm" + matching completed transfer →
  `TXN-9101`, consistent, wrong_transfer, dispute_resolution, review=true.
- "payment failed but money deducted" + completed payment →
  inconsistent, payment_failed, review=true.
- "asking for my OTP and PIN" + empty history →
  null, insufficient_data, phishing_or_social_engineering, fraud_risk, critical.

### Phase 3 — Safety System ✅ (`safety.py`, 88/88 checks passed)
- `SAFE_FALLBACK_REPLY`, `SAFE_FALLBACK_ACTION` constants.
- `check_credential_request(text) -> (bool, "credential_request")`
- `check_refund_promise(text) -> (bool, "refund_promise")`
- `check_third_party(text) -> (bool, "third_party_referral")` (phones/WhatsApp/Telegram +
  non-official URL detection via `OFFICIAL_DOMAINS` allowlist).
- `sanitize_customer_reply(text) -> (safe_reply, [violations])` — any violation → replace
  whole reply with `SAFE_FALLBACK_REPLY`.
- `sanitize_recommended_action(text) -> str` — credential/refund check → `SAFE_FALLBACK_ACTION`.
- `is_prompt_injection(complaint) -> bool` — **flags only, never blocks**.

---

## 5. Open decisions — NEED YOUR INPUT

These are spec ambiguities/conflicts I resolved toward caution. Confirm or override.

1. **Duplicate-payment "two transactions" gap (OPEN).** `get_evidence_verdict` only
   receives a single `matched_txn`, so the spec's "duplicate_signal AND matched two
   transactions → consistent" **cannot fire** — it falls through to `insufficient_data`
   (safe). To make it work, thread a match count/list through `analyze()` →
   `get_evidence_verdict`. **Decide when wiring deeper duplicate handling.**

2. **`classify_case` returns a 5-tuple, not the spec's 4-tuple.** Added `reason_codes`
   (STEP 4 builds them and they must reach `TicketResponse.reason_codes`). `analyze()`
   unpacks 5. Revert only if you have another home for reason_codes.

3. **merchant_settlement_delay / agent_cash_in_issue → `human_review_required=True`**
   when otherwise benign. The spec's two STEP-3 lists disagree on these; resolved toward
   caution (they're excluded from the auto-resolve safe set). Flip to a strict 5-bullet
   reading if you'd rather they auto-resolve.

4. **`get_evidence_verdict` edge:** `"I sent money but it failed"` + status `failed` →
   `inconsistent` (because `"sent"` is a success signal). Spec-literal. To make it
   `consistent`, change Rule 3's success check to require `has_success AND NOT has_failure`.

5. **Keyword matching is inconsistent across modules.** `classify_case` uses
   word-boundary (`\b`) matching (prevents `"shopping"`→phishing `"pin"`); the Phase-2
   matcher/verdict functions use naive `in`. Consider retrofitting `\b` into Phases 2's
   keyword checks for consistency.

6. **Amounts require a currency anchor.** `"sent 5000 to wrong number"` (no "taka")
   contributes 0 from the amount signal. Type/counterparty/status usually carry it over
   threshold. Add a "bare number near send/transfer verb" fallback if you want.

7. **`_REFUND_REQUEST_SIGNALS` (in analyzer) is my addition** — spec gave the refund
   *rule* but not its trigger words. Tune the list if you have a canonical one.

8. **Safety airtightness deviations** (all flagged, all verified): stacked `*`
   qualifiers on `ignore`/`forget`; `\bact as\b`; refund article tolerance; apostrophe +
   whitespace normalization; `\btelegram\b` + broadened WhatsApp; URL allowlist with
   file-extension guard. Revert any to strict-literal on request.

9. **`OFFICIAL_DOMAINS = {"queuestorm.com", "queuestorm.app"}` are placeholders.** Set to
   the real production domain(s) before deploy.

### Minor contract choices (already made, low-risk)
- `confidence` has `ge=0.0, le=1.0` in the model → `analyze()` must keep emitting clamped
  values (it does).
- Request model **ignores** unknown fields (not `extra="forbid"`) so unexpected fields
  don't 422.
- `main.py` and `analyzer.py` use a `try/except` import shim so the app runs both as
  `uvicorn queuestorm.main:app` (repo root) and `uvicorn main:app` / `python main.py`
  (inside `queuestorm\`).

---

## 6. 🔴 Critical integration notes (for wiring safety into analyzer)

- **NEVER re-sanitize `SAFE_FALLBACK_REPLY`.** It intentionally contains
  "PIN, OTP, password" (protective wording) and **self-trips the credential check**
  (verified). When integrating: `reply, viols = sanitize_customer_reply(generated_reply)`
  and use `reply` as-is — do not pass it back through.
- Planned integration in `analyze()`:
  1. `injected = is_prompt_injection(complaint)` → if True, append a reason_code
     (e.g. `"prompt_injection_detected"`). **Still process the ticket normally.**
  2. `customer_reply, viols = sanitize_customer_reply(customer_reply)`; extend
     `reason_codes` with `viols`.
  3. `recommended_next_action = sanitize_recommended_action(recommended_next_action)`.
  - Current deterministic templates are already safe (verified they pass sanitization),
    so this is a no-op for them but essential once `llm.py` generates text.

---

## 7. TODO / next steps (suggested order)

1. **Integrate `safety.py` into `analyze()`** (see §6). Cheap, high safety value.
2. **Build `llm.py`** — Claude (`claude-sonnet-4-6`) generates the three text fields
   (`agent_summary`, `recommended_next_action`, `customer_reply`) from the rule-based
   verdict. **All LLM output must pass through `safety.py` before returning.** Treat the
   complaint as data inside the prompt (it's untrusted). Lazy-init the client (keep cold
   start < 60s). Fall back to the deterministic templates on any API error/timeout.
3. **Decide duplicate-payment threading** (§5.1).
4. **Dockerfile** — `python:3.11-slim`, copy, `pip install -r requirements.txt`, set
   `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`, `CMD uvicorn main:app --host 0.0.0.0 --port $PORT`.
5. **README.md** — overview, run instructions, API examples, design notes (Docs = 5%).
6. **Railway deploy** — set `ANTHROPIC_API_KEY`; verify `/health` and `/analyze-ticket`.

---

## 8. Exact enums (case-sensitive — do not vary)

```
evidence_verdict: consistent | inconsistent | insufficient_data
case_type:        wrong_transfer | payment_failed | refund_request | duplicate_payment |
                  merchant_settlement_delay | agent_cash_in_issue |
                  phishing_or_social_engineering | other
department:       customer_support | dispute_resolution | payments_ops |
                  merchant_operations | agent_operations | fraud_risk
severity:         low | medium | high | critical
```

### Request body (TicketRequest)
- Required: `ticket_id: str`, `complaint: str` (non-empty after strip).
- Optional: `language` (`en|bn|mixed`), `channel` (`in_app_chat|call_center|email|
  merchant_portal|field_agent`), `user_type` (`customer|merchant|agent|unknown`),
  `campaign_context: str`, `transaction_history: List[TransactionEntry]` (default `[]`),
  `metadata: dict`.

### TransactionEntry (all optional)
`transaction_id, timestamp (ISO 8601), type (transfer|payment|cash_in|cash_out|settlement|refund),
amount (float), counterparty, status (completed|failed|pending|reversed)`.

### TicketResponse
`ticket_id, relevant_transaction_id (nullable), evidence_verdict, case_type, severity,
department, agent_summary, recommended_next_action, customer_reply, human_review_required,
confidence (0..1, optional), reason_codes (list, optional)`.

---

## 9. Verification convention used in this build

For each phase, a throwaway `_verify_*.py` (UTF-8) was written next to the code,
imported the module, ran `assert`-style checks (incl. Bangla), printed PASS/FAIL +
totals, then was **deleted**. Run with:
```powershell
$env:PYTHONPATH="F:\SUST Codex Hackathon\mainProject\queuestorm"; $env:PYTHONUTF8="1"
& "F:\SUST Codex Hackathon\mainProject\queuestorm\.venv\Scripts\python.exe" <file>.py
```
Results so far: Phase-2 extractors 29/29, matcher 15/15, verdict 18/18, classifier 19/19,
safety 88/88 — all green.
