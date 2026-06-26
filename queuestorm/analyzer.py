"""Core investigation logic: rule-based cross-referencing of complaint against transaction data.

All DECISIONS (matching, verdict, classification, severity, routing, confidence,
reason_codes) are pure rule-based code. The LLM is used ONLY to write the three
free-text fields (agent_summary / recommended_next_action / customer_reply) via
llm.get_text_fields_safe, which falls back to safe templates on any failure.

PHASE 2 — deterministic extraction utilities. Everything here is pure Python:
given untrusted complaint text, pull out the few hard signals we can verify
against the (source-of-truth) transaction history — amounts, time-of-day hints,
and counterparty hints (phone numbers / merchant ids).

PHASE 3 — find_relevant_transaction(): a weighted, rule-based scorer that ties a
complaint to at most one transaction, or to nothing when the evidence is thin.

Design stance: prefer precision over recall. We anchor amounts to an explicit
currency marker (taka/tk/bdt/৳/টাকা), a `k` suffix, or written number words, so
that phone digits and transaction ids are NOT mistaken for money. Missing or
unparseable data degrades gracefully (empty list / None / cautious boolean)
rather than raising. We never fabricate a match.
"""

import re
from datetime import datetime
from typing import List, Literal, Optional

try:  # package-relative import (e.g. `uvicorn queuestorm.main:app`)
    from .llm import get_text_fields_safe
    from .models import TicketRequest, TicketResponse, TransactionEntry
    from .safety import (
        SAFE_FALLBACK_REPLY,
        is_prompt_injection,
        sanitize_recommended_action,
    )
except ImportError:  # pragma: no cover - flat-script import (e.g. `python main.py`)
    from llm import get_text_fields_safe
    from models import TicketRequest, TicketResponse, TransactionEntry
    from safety import (
        SAFE_FALLBACK_REPLY,
        is_prompt_injection,
        sanitize_recommended_action,
    )


# --------------------------------------------------------------------------- #
# Shared constants                                                            #
# --------------------------------------------------------------------------- #
# Bangla -> ASCII digit map, applied up-front so every extractor sees 0-9.
_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# ---- amounts -------------------------------------------------------------- #
# A number: comma-grouped (5,000[.50]) or plain (5000[.50]).
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# A currency marker. Alpha tokens are fenced with letter look-arounds so "tk"
# in "stock" or "bdt" in "abdterm" never matches; ৳/টাকা need no fence.
_CUR = r"(?:(?<![a-z])(?:taka|tk|bdt)(?![a-z]))|৳|টাকা"

_P_K = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*[kK]\b")
_P_NUM_CUR = re.compile(rf"(?P<num>{_NUM})\s*(?:{_CUR})", re.IGNORECASE)
_P_CUR_NUM = re.compile(rf"(?:{_CUR})\s*(?P<num>{_NUM})", re.IGNORECASE)

_NUM_WORD = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh"
)
# A run of number words, e.g. "five thousand", "one hundred and twenty", with an
# optional trailing currency marker.
_P_WORD_RUN = re.compile(
    rf"\b(?:{_NUM_WORD})\b(?:[\s-]+(?:and[\s-]+)?\b(?:{_NUM_WORD})\b)*(?:\s*(?:{_CUR}))?",
    re.IGNORECASE,
)
_P_WORD_TOKEN = re.compile(rf"\b(?:{_NUM_WORD})\b", re.IGNORECASE)

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"thousand": 1000, "lakh": 100000}
_SCALE_WORDS = {"hundred", "thousand", "lakh"}

# ---- time hints ----------------------------------------------------------- #
# Branch 1: HH:MM with optional am/pm. Branch 2: H am/pm. Branch 3: "at/around H"
# (only when not actually followed by am/pm, ':' or another digit).
_TIME_RE = re.compile(
    r"(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*(?P<ap1>[ap]\.?m\.?)?"
    r"|(?P<h2>\d{1,2})\s*(?P<ap2>[ap]\.?m\.?)"
    r"|(?:\bat\b|\baround\b)\s+(?P<h3>\d{1,2})(?!\s*[ap]\.?m\.?)(?![:\d])",
    re.IGNORECASE,
)
# Coarse Bangla day-part words -> representative 24h times.
_BANGLA_TIME = {
    "সকালে": "09:00", "সকাল": "09:00",
    "দুপুরে": "12:00", "দুপুর": "12:00",
    "বিকেলে": "16:00", "বিকালে": "16:00", "বিকেল": "16:00", "বিকাল": "16:00",
    "সন্ধ্যায়": "18:00", "সন্ধ্যা": "18:00",
    "রাতে": "21:00", "রাত": "21:00",
}
# Longest-first so "সকালে" wins over "সকাল".
_BANGLA_TIME_RE = re.compile("|".join(sorted(_BANGLA_TIME, key=len, reverse=True)))

# ---- counterparty hints --------------------------------------------------- #
# BD mobile: 01XXXXXXXXX (11 digits), 8801XXXXXXXXX, +8801XXXXXXXXX. \w fences
# stop us from biting into a longer alphanumeric id or digit run.
_P_PHONE = re.compile(r"(?<!\w)(?:\+?880|0)1\d{9}(?!\w)")
_P_MID = re.compile(r"\bMID[-\s]?\d+\b", re.IGNORECASE)
_P_MERCHANT = re.compile(r"\bmerchant(?:\s+id)?\s+#?\d+\b", re.IGNORECASE)

# ---- transaction matching ------------------------------------------------- #
# Complaint keywords that point at a particular transaction `type`.
_TYPE_KEYWORDS = {
    "transfer": ["sent", "transfer", "wrong number", "পাঠিয়েছি", "পাঠিয়ে",
                 "wrong person", "send", "দিয়েছি"],
    "payment": ["paid", "payment", "buy", "purchase", "shop", "merchant",
                "কিনেছি", "payment করেছি"],
    "cash_in": ["deposit", "agent", "cash in", "joma", "জমা"],
    "cash_out": ["withdraw", "cash out", "তুলেছি"],
    "refund": ["refund", "returned", "ফেরত"],
}
# Words signalling the customer thinks something went wrong / went right.
_FAILURE_WORDS = ["failed", "not received", "deducted but", "missing",
                  "কাটা গেছে", "পাইনি", "যায়নি", "হয়নি"]
_SUCCESS_WORDS = ["sent", "completed", "done", "পাঠিয়েছি", "হয়েছে"]
# Phishing / social-engineering markers (lower-cased for case-insensitive match).
_PHISHING_WORDS = ["otp", "pin", "password", "scam", "fraud", "hacked",
                   "suspicious call", "ওটিপি", "পিন"]

# ---- evidence verdict ----------------------------------------------------- #
# Signal sets for get_evidence_verdict(). Distinct from the matcher's
# _FAILURE_WORDS / _SUCCESS_WORDS above: those drive scoring, these drive the
# consistent / inconsistent judgement and are phrased more specifically.
_VERDICT_FAILURE_SIGNALS = [
    "failed", "not received", "didn't receive", "not credited",
    "deducted but", "balance cut", "money gone but",
    "কাটা গেছে", "পাইনি", "যায়নি", "কিন্তু পাইনি",
]
_VERDICT_SUCCESS_SIGNALS = [
    "sent", "transferred", "i paid", "i sent", "completed",
    "পাঠিয়েছি", "দিয়েছি", "পেমেন্ট করেছি",
]
_WRONG_RECIPIENT_SIGNALS = [
    "wrong number", "wrong person", "wrong recipient", "mistake",
    "ভুল নম্বর", "ভুল মানুষ", "wrong account",
]
# Used by the duplicate-payment consistency path once a confirmed match count is
# threaded through analyze(); see the note inside get_evidence_verdict().
_DUPLICATE_SIGNALS = [
    "twice", "double", "duplicate", "charged again", "two times",
    "দুইবার", "দুবার", "আবার কাটা",
]
# "Asks for a refund" — not specified verbatim; chosen to drive the RULE 4 refund
# branch. Tune freely.
_REFUND_REQUEST_SIGNALS = [
    "refund", "money back", "want my money back", "return my money",
    "refund request", "ফেরত", "ফেরত চাই",
]

# ---- case classification -------------------------------------------------- #
# Ordered: the FIRST matching case_type wins (priority top -> bottom). Matching
# uses a leading word boundary (\b) so a substring like "pin" inside "shopping"
# does NOT trigger the phishing keyword.
_CASE_RULES = [
    ("phishing_or_social_engineering", "fraud_risk",
     ["otp", "pin", "password", "scam", "fraud", "hacked", "suspicious call",
      "suspicious sms", "impersonat", "ওটিপি", "পিন", "পাসওয়ার্ড", "প্রতারণা", "হ্যাক"]),
    ("wrong_transfer", "dispute_resolution",
     ["wrong number", "wrong person", "wrong recipient", "sent to wrong",
      "ভুল নম্বর", "ভুল মানুষে", "wrong account"]),
    ("duplicate_payment", "payments_ops",
     ["twice", "double", "duplicate", "charged twice", "two times",
      "দুইবার", "দুবার", "আবার কাটা"]),
    # A merchant *settlement* complaint usually also says "not received" — which is
    # a payment_failed keyword. Check the strong, unambiguous "settlement" signal
    # FIRST so these route to merchant_operations, not payments_ops. (Bare
    # "merchant"/"shop" stay at the lower-priority rule below, so "shop payment
    # failed" still classifies as payment_failed.)
    ("merchant_settlement_delay", "merchant_operations",
     ["merchant settlement", "settlement", "সেটেলমেন্ট"]),
    ("payment_failed", "payments_ops",
     ["failed", "not received", "deducted", "not credited", "balance cut",
      "কাটা গেছে", "পাইনি", "ব্যর্থ"]),
    ("merchant_settlement_delay", "merchant_operations",
     ["merchant", "shop", "store", "business", "মার্চেন্ট"]),
    ("agent_cash_in_issue", "agent_operations",
     ["agent", "cash in", "deposit", "not reflected", "জমা", "এজেন্ট", "ক্যাশ ইন"]),
    ("refund_request", "dispute_resolution",
     ["refund", "money back", "return my money", "ফেরত", "রিফান্ড", "ফেরত চাই"]),
]
# Precompiled (case_type, department, regex). Regex = \b(?:kw1|kw2|...).
_CASE_PATTERNS = [
    (case, dept,
     re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")", re.IGNORECASE))
    for case, dept, kws in _CASE_RULES
]
# Short reason-code labels for case_type / verdict.
_CASE_REASON = {
    "phishing_or_social_engineering": "phishing_signal",
    "wrong_transfer": "wrong_transfer_keyword",
    "duplicate_payment": "duplicate_payment_keyword",
    "payment_failed": "payment_failed_keyword",
    "merchant_settlement_delay": "merchant_settlement_keyword",
    "agent_cash_in_issue": "agent_cash_in_keyword",
    "refund_request": "refund_request_keyword",
    "other": "uncategorized_case",
}
_VERDICT_REASON = {
    "consistent": "consistent_evidence",
    "inconsistent": "inconsistent_evidence",
    "insufficient_data": "insufficient_evidence",
}


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _to_float(num_str: str) -> float:
    """Parse a (possibly comma-grouped) number string to float."""
    return float(num_str.replace(",", ""))


def _words_to_number(words: List[str]) -> int:
    """Convert a run of English number words to an integer (supports up to lakh)."""
    total = 0
    current = 0
    for w in words:
        w = w.lower()
        if w in _ONES:
            current += _ONES[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
    return total + current


def _to_24h(hour: int, minute: int, ap):
    """Normalize an (hour, minute, am/pm?) triple to a 'HH:MM' string, or None if invalid."""
    if ap:
        is_pm = ap.lower().startswith("p")
        if is_pm and hour != 12:
            hour += 12
        elif not is_pm and hour == 12:  # 12am -> 00
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_hhmm(s: str):
    """Parse a normalized 'HH:MM' hint to minutes-since-midnight, or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _parse_iso(ts: str):
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'), or None on failure."""
    try:
        ts = ts.strip()
        if ts.endswith(("Z", "z")):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _digits(s: str) -> str:
    """Return only the ASCII digits contained in a string."""
    return re.sub(r"\D", "", s)


def _counterparty_matches(hint: str, counterparty: str) -> bool:
    """Whether an extracted hint refers to the same counterparty as the txn.

    Tolerates BD phone-format variants (+880 / 880 / leading 0) by comparing the
    last 10 subscriber digits, and matches short numeric merchant ids by exact
    digit equality. A counterparty with no digits only matches on a literal
    (case-insensitive) string equality.
    """
    if not counterparty:
        return False
    h = hint.strip().lower()
    c = counterparty.strip().lower()
    if h == c:
        return True
    hd, cd = _digits(h), _digits(c)
    if not hd or not cd:
        return False
    if len(hd) >= 10 and len(cd) >= 10:  # phone numbers: compare subscriber part
        return hd[-10:] == cd[-10:]
    return hd == cd  # short numeric ids (e.g. merchant number)


# --------------------------------------------------------------------------- #
# FUNCTION 1 — extract_amounts                                                #
# --------------------------------------------------------------------------- #
def extract_amounts(text: str) -> List[float]:
    """Extract monetary amounts from text as a list of floats (in text order).

    Recognizes: "5000 taka", "5,000 taka", "৳5000", "BDT 5000", Bangla numerals,
    "5k"/"50k", and written words like "five thousand taka". Returns [] if none.
    """
    if not text:
        return []
    norm = text.translate(_BANGLA_DIGITS)

    spans = []  # (start, end, value)
    for m in _P_K.finditer(norm):
        spans.append((m.start(), m.end(), _to_float(m.group("num")) * 1000.0))
    for m in _P_NUM_CUR.finditer(norm):
        spans.append((m.start("num"), m.end(), _to_float(m.group("num"))))
    for m in _P_CUR_NUM.finditer(norm):
        spans.append((m.start(), m.end("num"), _to_float(m.group("num"))))
    for m in _P_WORD_RUN.finditer(norm):
        seg = m.group(0)
        words = [w.lower() for w in _P_WORD_TOKEN.findall(seg)]
        if not words:
            continue
        has_scale = any(w in _SCALE_WORDS for w in words)
        has_currency = re.search(rf"(?:{_CUR})\s*$", seg, re.IGNORECASE) is not None
        if not (has_scale or has_currency):
            continue  # a bare "five" with no scale/currency is not an amount
        value = _words_to_number(words)
        if value > 0:
            spans.append((m.start(), m.end(), float(value)))

    # Order by position; drop overlapping spans (e.g. "৳5000" vs "5000 taka").
    spans.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    amounts: List[float] = []
    last_end = -1
    for start, end, value in spans:
        if start >= last_end:
            amounts.append(value)
            last_end = end
    return amounts


# --------------------------------------------------------------------------- #
# FUNCTION 2 — extract_time_hints                                             #
# --------------------------------------------------------------------------- #
def extract_time_hints(text: str) -> List[str]:
    """Extract time references and normalize them to 24h 'HH:MM' strings.

    Recognizes: "2pm", "2 PM", "14:00", "2:30pm", "at 2", "around 2", and Bangla
    day-parts ("সকালে", "দুপুরে", ...). Returns [] if none. A bare hour with no
    am/pm is read literally (e.g. "at 2" -> "02:00").
    """
    if not text:
        return []
    norm = text.translate(_BANGLA_DIGITS)

    found = []  # (start, value)
    for m in _TIME_RE.finditer(norm):
        if m.group("h1") is not None:
            val = _to_24h(int(m.group("h1")), int(m.group("m1")), m.group("ap1"))
        elif m.group("h2") is not None:
            val = _to_24h(int(m.group("h2")), 0, m.group("ap2"))
        else:
            val = _to_24h(int(m.group("h3")), 0, None)
        if val:
            found.append((m.start(), val))
    for m in _BANGLA_TIME_RE.finditer(norm):
        found.append((m.start(), _BANGLA_TIME[m.group()]))

    found.sort(key=lambda t: t[0])
    hints: List[str] = []
    for _, val in found:
        if val not in hints:
            hints.append(val)
    return hints


# --------------------------------------------------------------------------- #
# FUNCTION 3 — is_within_time_window                                          #
# --------------------------------------------------------------------------- #
def is_within_time_window(
    complaint_time_hint: str,
    txn_timestamp: str,
    window_hours: int = 3,
) -> bool:
    """Return whether a transaction's time-of-day falls within window_hours of a hint.

    - Empty / unusable hint -> True (no time constraint to apply).
    - Unparseable timestamp -> False (cannot verify, so do not assert a match).
    Compares wall-clock time-of-day on a 24h ring (so 23:30 and 00:30 are close).
    """
    if not complaint_time_hint or not str(complaint_time_hint).strip():
        return True
    hint = _parse_hhmm(str(complaint_time_hint))
    if hint is None:
        return True  # no usable constraint
    dt = _parse_iso(str(txn_timestamp))
    if dt is None:
        return False  # cannot verify against an unparseable timestamp

    txn_min = dt.hour * 60 + dt.minute
    diff = abs(txn_min - hint)
    diff = min(diff, 1440 - diff)  # wrap-around on the 24h clock
    return diff <= window_hours * 60


# --------------------------------------------------------------------------- #
# FUNCTION 4 — extract_counterparty_hints                                     #
# --------------------------------------------------------------------------- #
def extract_counterparty_hints(text: str) -> List[str]:
    """Extract counterparty identifiers from text (in text order).

    Recognizes BD phone numbers (01XXXXXXXXX, 8801XXXXXXXXX, +8801XXXXXXXXX,
    including Bangla numerals) and merchant ids ("MID-123", "merchant 456").
    Returns the matched strings; [] if none.
    """
    if not text:
        return []
    norm = text.translate(_BANGLA_DIGITS)

    spans = []  # (start, value)
    for pattern in (_P_PHONE, _P_MID, _P_MERCHANT):
        for m in pattern.finditer(norm):
            spans.append((m.start(), m.group()))

    spans.sort(key=lambda t: t[0])
    hints: List[str] = []
    for _, val in spans:
        if val not in hints:
            hints.append(val)
    return hints


# --------------------------------------------------------------------------- #
# FUNCTION 5 — find_relevant_transaction                                      #
# --------------------------------------------------------------------------- #
def find_relevant_transaction(
    complaint: str,
    transactions: List[TransactionEntry],
) -> Optional[str]:
    """Score each transaction against the complaint and return the best match's id.

    Weighted, rule-based (no LLM). Returns the transaction_id of the single best
    match when its score >= 2, otherwise None. We never force a match.

    Weights: amount (3 exact / 1 within 10%), type keyword (2), status signal
    (2 / 1), time window (2), counterparty (3).
    """
    if not complaint:
        return None
    text = complaint.lower()

    # SPECIAL CASE — phishing / social-engineering reports (OTP, PIN, scam, ...)
    # frequently arrive with no transaction history. With nothing to cross-
    # reference, do not fabricate a match.
    if any(word in text for word in _PHISHING_WORDS) and not transactions:
        return None
    if not transactions:
        return None  # nothing to score against

    extracted_amounts = extract_amounts(complaint)
    time_hints = extract_time_hints(complaint)
    counterparty_hints = extract_counterparty_hints(complaint)
    has_failure = any(word in text for word in _FAILURE_WORDS)
    has_success = any(word in text for word in _SUCCESS_WORDS)

    best_score = 0
    best_txn_id: Optional[str] = None

    for txn in transactions:
        score = 0

        # AMOUNT MATCHING (weight 3 exact / 1 near) -------------------------- #
        if txn.amount is not None and extracted_amounts:
            amt = txn.amount
            if any(abs(a - amt) < 0.01 for a in extracted_amounts):
                score += 3
            elif amt != 0 and any(abs(a - amt) <= 0.10 * abs(amt) for a in extracted_amounts):
                score += 1

        # TYPE MATCHING (weight 2) ------------------------------------------ #
        if txn.type is not None:
            if any(kw in text for kw in _TYPE_KEYWORDS.get(txn.type, ())):
                score += 2

        # STATUS SIGNAL MATCHING (weight 2 / 1) ----------------------------- #
        if txn.status is not None:
            if has_failure and txn.status in ("failed", "pending"):
                score += 2
            if has_failure and txn.status == "completed":
                score += 1  # still relevant — the inconsistent case
            if has_success and txn.status == "completed":
                score += 2

        # TIME MATCHING (weight 2) ------------------------------------------ #
        if time_hints and txn.timestamp:
            if any(is_within_time_window(h, txn.timestamp) for h in time_hints):
                score += 2

        # COUNTERPARTY MATCHING (weight 3) ---------------------------------- #
        if counterparty_hints and txn.counterparty:
            if any(_counterparty_matches(h, txn.counterparty) for h in counterparty_hints):
                score += 3

        if score > best_score:  # ties keep the earlier transaction
            best_score = score
            best_txn_id = txn.transaction_id

    # MINIMUM THRESHOLD — never force a weak match.
    if best_score >= 2:
        return best_txn_id
    return None


# --------------------------------------------------------------------------- #
# FUNCTION 6 — get_evidence_verdict                                           #
# --------------------------------------------------------------------------- #
def get_evidence_verdict(
    complaint: str,
    matched_txn: Optional[TransactionEntry],
) -> Literal["consistent", "inconsistent", "insufficient_data"]:
    """Judge whether the complaint and the matched transaction agree.

    Returns "consistent", "inconsistent", or "insufficient_data". Inconsistency
    (a claim that contradicts the record) is checked before consistency. When in
    doubt we return "insufficient_data" — never a verdict we are unsure about.
    """
    # RULE 1 — nothing matched, so there is nothing to corroborate.
    if matched_txn is None:
        return "insufficient_data"

    text = (complaint or "").lower()
    has_failure = any(s in text for s in _VERDICT_FAILURE_SIGNALS)
    has_success = any(s in text for s in _VERDICT_SUCCESS_SIGNALS)
    has_wrong_recipient = any(s in text for s in _WRONG_RECIPIENT_SIGNALS)
    has_refund_request = any(s in text for s in _REFUND_REQUEST_SIGNALS)
    status = matched_txn.status
    txn_type = matched_txn.type

    # RULE 3 — INCONSISTENT: the customer's claim contradicts the record.
    if has_failure and status == "completed":
        return "inconsistent"  # says failed, record says completed
    if has_success and status == "failed":
        return "inconsistent"  # says sent/paid, record says failed

    # RULE 4 — CONSISTENT: complaint and record agree.
    if has_failure and status in ("failed", "pending"):
        return "consistent"
    if has_wrong_recipient and txn_type == "transfer" and status == "completed":
        return "consistent"
    # Duplicate-payment consistency requires confirming that TWO transactions
    # matched the complaint. This signature carries only a single matched_txn, so
    # that cannot be verified here — we deliberately fall through to
    # insufficient_data rather than over-assert. (See _DUPLICATE_SIGNALS; thread a
    # match count through analyze() to enable this branch.)
    if has_refund_request:  # matched_txn is not None, so "a txn exists" holds
        return "consistent"

    # RULE 5 — not sure: insufficient_data is always the safe default.
    return "insufficient_data"


# --------------------------------------------------------------------------- #
# FUNCTION 7 — classify_case                                                  #
# --------------------------------------------------------------------------- #
def classify_case(
    complaint: str,
    matched_txn: Optional[TransactionEntry],
    evidence_verdict: str,
) -> tuple[str, str, str, bool, list[str]]:
    """Classify into (case_type, department, severity, human_review_required, reason_codes).

    NOTE: the brief specified a 4-tuple, but STEP 4 requires building reason_codes
    that must reach the response's `reason_codes` field — so this returns a
    5-tuple with reason_codes appended (see the accompanying explanation).
    """
    text = (complaint or "").lower()

    # STEP 1 — case_type / department by keyword priority (first match wins).
    case_type, department = "other", "customer_support"
    for case, dept, pattern in _CASE_PATTERNS:
        if pattern.search(text):
            case_type, department = case, dept
            break

    # STEP 2 — severity. Amount comes from the matched transaction, else 0.
    amount = matched_txn.amount if (matched_txn and matched_txn.amount is not None) else 0
    if case_type == "phishing_or_social_engineering" or amount >= 50000:
        severity = "critical"
    elif amount >= 10000 or case_type == "wrong_transfer" or evidence_verdict == "inconsistent":
        severity = "high"
    elif amount >= 1000 or case_type in ("payment_failed", "duplicate_payment"):
        severity = "medium"
    else:
        severity = "low"

    # STEP 3 — human review. Auto-resolvable ONLY when fully benign. Anything
    # else needs review — including merchant/agent cases, which the brief's two
    # lists disagree on; we resolve that gap toward caution (review = True).
    auto_resolvable = (
        evidence_verdict == "consistent"
        and severity in ("low", "medium")
        and case_type in ("refund_request", "payment_failed", "other")
        and matched_txn is not None
        and amount < 5000
    )
    human_review_required = not auto_resolvable

    # STEP 4 — reason_codes (always 3-4 here; within the required 2-4 range).
    amount_matched = (
        matched_txn is not None
        and matched_txn.amount is not None
        and any(abs(a - matched_txn.amount) < 0.01 for a in extract_amounts(complaint))
    )
    codes = [
        _CASE_REASON.get(case_type, "uncategorized_case"),
        _VERDICT_REASON.get(evidence_verdict, "evidence_unknown"),
    ]
    if amount >= 50000:
        codes.append("very_high_value")
    elif amount >= 10000:
        codes.append("high_value")
    elif amount >= 5000:
        codes.append("elevated_value")
    elif amount >= 1000:
        codes.append("moderate_value")
    if matched_txn is None:
        codes.append("no_transaction_evidence")
    elif amount_matched:
        codes.append("amount_match")
    else:
        codes.append("transaction_matched")
    reason_codes = list(dict.fromkeys(codes))[:4]

    return case_type, department, severity, human_review_required, reason_codes


# --------------------------------------------------------------------------- #
# Confidence score (deterministic).                                           #
# --------------------------------------------------------------------------- #
def calculate_confidence(
    verdict: str,
    matched_id: Optional[str],
    case_type: str,
) -> float:
    """Map the (verdict, match) outcome to a confidence score.

    `case_type` is accepted for signature stability / forward use but does not
    currently affect the score.
    """
    if verdict == "consistent" and matched_id:
        return 0.90
    elif verdict == "inconsistent" and matched_id:
        return 0.75
    elif verdict == "insufficient_data" and matched_id:
        return 0.60
    else:
        return 0.45


# --------------------------------------------------------------------------- #
# Enum safety net — allowed values mirror models.py.                          #
# --------------------------------------------------------------------------- #
_ALLOWED_VERDICT = {"consistent", "inconsistent", "insufficient_data"}
_ALLOWED_CASE_TYPE = {
    "wrong_transfer", "payment_failed", "refund_request", "duplicate_payment",
    "merchant_settlement_delay", "agent_cash_in_issue",
    "phishing_or_social_engineering", "other",
}
_ALLOWED_DEPARTMENT = {
    "customer_support", "dispute_resolution", "payments_ops",
    "merchant_operations", "agent_operations", "fraud_risk",
}
_ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}


def _coerce_enum(value, allowed, default):
    """Return value if it is an allowed enum member, else the safe default.

    A defensive net so a future bug yielding an out-of-set value degrades the
    response gracefully instead of raising a Pydantic ValidationError (500).
    """
    return value if value in allowed else default


# --------------------------------------------------------------------------- #
# Investigation entry point — rule-based logic + LLM text generation.          #
#                                                                              #
# ALL decisions (matching, verdict, classification, severity, routing,         #
# confidence, reason_codes) are computed deterministically here. The LLM        #
# (llm.get_text_fields_safe) ONLY writes the three free-text fields and falls   #
# back to safe templates on any failure. Every customer-facing string is then   #
# re-checked by safety.py before it leaves this function.                       #
# --------------------------------------------------------------------------- #
async def analyze(request: TicketRequest) -> TicketResponse:
    """Investigate a ticket and compose the structured verdict.

    Logic is pure code; only the three text fields are LLM-generated (with a
    deterministic, always-safe fallback). The complaint is untrusted throughout:
    it never influences verdict / enum / safety decisions — only keyword matching.
    """
    # STEP 1 — Injection check FIRST. Flags only; never alters the investigation.
    injection = is_prompt_injection(request.complaint)

    # STEP 2 — Transaction matching (pure code). Coerce any falsy id ("" / missing)
    # to real None so the response shows JSON null, never an empty string.
    txns = request.transaction_history or []
    matched_id = find_relevant_transaction(request.complaint, txns) or None
    matched_txn = (
        next((t for t in txns if t.transaction_id == matched_id), None)
        if matched_id else None
    )

    # STEP 3 — Evidence verdict (pure code).
    verdict = get_evidence_verdict(request.complaint, matched_txn)

    # STEP 4 — Case classification (pure code). classify_case also returns its own
    # reason codes (5th element); we merge those with safety codes in STEP 7.
    case_type, department, severity, human_review, classification_codes = classify_case(
        request.complaint, matched_txn, verdict
    )

    # STEP 5 — Confidence score (pure code).
    confidence = calculate_confidence(verdict, matched_id, case_type)

    # STEP 6 — Generate the three text fields (LLM, with automatic safe fallback).
    # get_text_fields_safe sanitizes the customer_reply itself and returns any
    # safety violations it found in the GENERATED reply, so we can surface them in
    # reason_codes (an unsafe LLM reply is flagged; a plain fallback is not).
    text_fields, violations = await get_text_fields_safe(
        complaint=request.complaint,
        case_type=case_type,
        evidence_verdict=verdict,
        department=department,
        severity=severity,
        matched_txn=matched_txn.model_dump() if matched_txn else None,
        human_review_required=human_review,
    )
    safe_reply = text_fields["customer_reply"]  # already safety-filtered upstream

    # STEP 7 — Sanitize the internal recommended action too (the LLM wrote it).
    safe_action = sanitize_recommended_action(text_fields["recommended_next_action"])

    # Merge reason codes: safety codes ALWAYS survive (they go first), and the
    # soft cap expands to fit them so classification codes are trimmed first.
    safety_codes: list[str] = []
    if injection:
        safety_codes.append("prompt_injection_detected")
    if violations:
        safety_codes.append("safety_filter_triggered")
        safety_codes.extend(violations[:2])
    reason_codes = list(dict.fromkeys(safety_codes + list(classification_codes)))
    reason_codes = reason_codes[: max(4, len(safety_codes))]

    # STEP 8 — On injection, force the safe fallback reply regardless of output.
    if injection:
        safe_reply = SAFE_FALLBACK_REPLY

    # STEP 8.5 — Enum safety net. The decisions above always produce valid enums,
    # but coerce defensively so any out-of-set value degrades to a safe default
    # rather than raising a 500 at response construction.
    verdict = _coerce_enum(verdict, _ALLOWED_VERDICT, "insufficient_data")
    case_type = _coerce_enum(case_type, _ALLOWED_CASE_TYPE, "other")
    department = _coerce_enum(department, _ALLOWED_DEPARTMENT, "customer_support")
    severity = _coerce_enum(severity, _ALLOWED_SEVERITY, "medium")

    # STEP 9 — Build and return the response.
    return TicketResponse(
        ticket_id=request.ticket_id,
        relevant_transaction_id=matched_id,
        evidence_verdict=verdict,
        case_type=case_type,
        severity=severity,
        department=department,
        agent_summary=text_fields["agent_summary"],
        recommended_next_action=safe_action,
        customer_reply=safe_reply,
        human_review_required=human_review,
        confidence=confidence,
        reason_codes=reason_codes,
    )


# --------------------------------------------------------------------------- #
# Unit checks (proof of behavior). Each line is an assert that holds true.     #
# Run: python -c "import analyzer" after setting PYTHONPATH, or eyeball below. #
# --------------------------------------------------------------------------- #
# FUNCTION 1 — extract_amounts
#   extract_amounts("I sent 5000 taka")              == [5000.0]
#   extract_amounts("paid 5,000 taka then ৳3000")    == [5000.0, 3000.0]
#   extract_amounts("BDT 1500 was debited")          == [1500.0]
#   extract_amounts("five thousand taka")            == [5000.0]
#   extract_amounts("one hundred twenty taka")       == [120.0]
#   extract_amounts("please send 5k")                == [5000.0]
#   extract_amounts("withdraw 50k")                  == [50000.0]
#   extract_amounts("৳৫০০০ debited")                 == [5000.0]   # Bangla numerals
#   extract_amounts("I waited five minutes")         == []         # bare word, no amount
#   extract_amounts("just saying hello")             == []
#
# FUNCTION 2 — extract_time_hints
#   extract_time_hints("it happened at 2pm")         == ["14:00"]
#   extract_time_hints("around 14:00 today")         == ["14:00"]
#   extract_time_hints("called at 2 pm sharp")       == ["14:00"]  # space before pm
#   extract_time_hints("meet at 2")                  == ["02:00"]  # bare hour, literal
#   extract_time_hints("12:30am login")              == ["00:30"]
#   extract_time_hints("দুপুরে টাকা পাঠিয়েছি")        == ["12:00"]
#   extract_time_hints("সকালে hয়েছিল")               == ["09:00"]
#   extract_time_hints("no time mentioned")          == []
#
# FUNCTION 3 — is_within_time_window
#   is_within_time_window("14:00", "2026-06-20T15:30:00Z")        is True   # 90 min
#   is_within_time_window("14:00", "2026-06-20T18:30:00Z")        is False  # 270 min
#   is_within_time_window("", "2026-06-20T18:30:00Z")             is True   # no hint
#   is_within_time_window("14:00", "not-a-timestamp")             is False  # unparseable
#   is_within_time_window("09:00", "2026-06-20T08:00:00Z", 2)     is True   # 60 min, ±2h
#
# FUNCTION 4 — extract_counterparty_hints
#   extract_counterparty_hints("call 01712345678")               == ["01712345678"]
#   extract_counterparty_hints("from +8801712345678 now")        == ["+8801712345678"]
#   extract_counterparty_hints("sent to 8801812345678")          == ["8801812345678"]
#   extract_counterparty_hints("০১৭১২৩৪৫৬৭৮ পাঠালাম")             == ["01712345678"]
#   extract_counterparty_hints("paid MID-123 and merchant 456")  == ["MID-123", "merchant 456"]
#   extract_counterparty_hints("no identifiers here")            == []
#
# FUNCTION 5 — find_relevant_transaction   (T = TransactionEntry)
#   "sent 5000 taka to 01712345678 at 2pm" vs
#     T(TXN1, transfer, 5000, "01712345678", completed, ...T14:30:00Z)   -> "TXN1"  (score 12)
#   "I paid 2000 taka but payment failed" vs
#     T(TXN9, payment, 2000, "MID-50", failed, ...)                      -> "TXN9"  (amount3+type2+status2)
#   "got a scam call asking for my OTP", transactions=[]                 -> None    (phishing + empty)
#   "the weather is nice today" vs T(TXN5, payment, 999, "MID-1", done)  -> None    (score < 2)
#
# FUNCTION 6 — get_evidence_verdict   (T = TransactionEntry)
#   ("anything", None)                                            == "insufficient_data"
#   ("I paid but it failed", T(status="completed"))              == "inconsistent"
#   ("I already sent it", T(status="failed"))                    == "inconsistent"
#   ("deducted but I didn't receive", T(status="pending"))       == "consistent"
#   ("sent to wrong number by mistake",
#        T(type="transfer", status="completed"))                 == "consistent"
#   ("I want a refund please", T(status="completed"))            == "consistent"
#   ("just checking my account", T(type="payment"))             == "insufficient_data"
#   ("I was charged twice", T(...))                              == "insufficient_data"  # duplicate needs 2-match info
#
# FUNCTION 7 — classify_case  -> (case_type, department, severity, human_review, reason_codes)
#   ("OTP scam call", None, "insufficient_data")
#        -> ("phishing_or_social_engineering", "fraud_risk", "critical", True, [...])
#   ("sent to wrong number", T(type="transfer", amount=3000, status="completed"), "consistent")
#        -> ("wrong_transfer", "dispute_resolution", "high", True, [...])
#   ("please refund my money", T(type="payment", amount=200, status="completed"), "consistent")
#        -> ("refund_request", "dispute_resolution", "low", False, [...])   # only-False path
#   ("merchant settlement delayed", T(amount=800, status="pending"), "consistent")
#        -> ("merchant_settlement_delay", "merchant_operations", "low", True, [...])  # gap -> caution
#   ("I love shopping today", None, "insufficient_data")
#        -> case_type == "merchant_settlement_delay" (NOT phishing; \b stops "pin" in "shopping")
