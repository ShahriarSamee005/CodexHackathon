"""Pydantic v2 models for all request and response schemas, including enums.

Contract notes:
- Transaction history is the SOURCE OF TRUTH; the complaint is UNTRUSTED text.
- Enum values below are case-sensitive and must match the problem statement exactly.
- ErrorResponse must never carry stack traces, secrets, or tokens.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionEntry(BaseModel):
    """A single transaction history record.

    All fields are optional with ``None`` defaults because real-world history
    entries may arrive partial or redacted. Missing data must degrade the
    verdict toward ``insufficient_data`` rather than cause a hard failure.
    """

    transaction_id: Optional[str] = None
    timestamp: Optional[str] = None  # ISO 8601, e.g. "2026-06-20T14:30:00Z"
    type: Optional[
        Literal["transfer", "payment", "cash_in", "cash_out", "settlement", "refund"]
    ] = None
    amount: Optional[float] = None
    counterparty: Optional[str] = None
    status: Optional[Literal["completed", "failed", "pending", "reversed"]] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "transaction_id": "TXN123456",
                "timestamp": "2026-06-20T14:30:00Z",
                "type": "transfer",
                "amount": 1500.0,
                "counterparty": "01712345678",
                "status": "completed",
            }
        }
    )


class TicketRequest(BaseModel):
    """Incoming support ticket to investigate.

    ``ticket_id`` and ``complaint`` are required. ``complaint`` is untrusted
    free text and must not be treated as instructions by any downstream logic.
    """

    ticket_id: str
    complaint: str

    language: Optional[Literal["en", "bn", "mixed"]] = None
    channel: Optional[
        Literal["in_app_chat", "call_center", "email", "merchant_portal", "field_agent"]
    ] = None
    user_type: Optional[Literal["customer", "merchant", "agent", "unknown"]] = None
    campaign_context: Optional[str] = None
    transaction_history: Optional[List[TransactionEntry]] = []
    metadata: Optional[dict] = None

    @field_validator("complaint")
    @classmethod
    def complaint_must_not_be_empty(cls, v: str) -> str:
        """Reject a complaint that is empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("complaint must not be empty")
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticket_id": "TKT-2026-0001",
                "complaint": "I sent 1500 taka to the wrong number by mistake.",
                "language": "en",
                "channel": "in_app_chat",
                "user_type": "customer",
                "campaign_context": None,
                "transaction_history": [
                    {
                        "transaction_id": "TXN123456",
                        "timestamp": "2026-06-20T14:30:00Z",
                        "type": "transfer",
                        "amount": 1500.0,
                        "counterparty": "01712345678",
                        "status": "completed",
                    }
                ],
                "metadata": {"app_version": "3.1.0"},
            }
        }
    )


class TicketResponse(BaseModel):
    """Structured investigation verdict returned by ``POST /analyze-ticket``."""

    ticket_id: str
    relevant_transaction_id: Optional[str]
    evidence_verdict: Literal["consistent", "inconsistent", "insufficient_data"]
    case_type: Literal[
        "wrong_transfer",
        "payment_failed",
        "refund_request",
        "duplicate_payment",
        "merchant_settlement_delay",
        "agent_cash_in_issue",
        "phishing_or_social_engineering",
        "other",
    ]
    severity: Literal["low", "medium", "high", "critical"]
    department: Literal[
        "customer_support",
        "dispute_resolution",
        "payments_ops",
        "merchant_operations",
        "agent_operations",
        "fraud_risk",
    ]
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool

    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason_codes: Optional[List[str]] = []

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticket_id": "TKT-2026-0001",
                "relevant_transaction_id": "TXN123456",
                "evidence_verdict": "consistent",
                "case_type": "wrong_transfer",
                "severity": "medium",
                "department": "dispute_resolution",
                "agent_summary": (
                    "Customer reports transferring 1500 to an unintended recipient. "
                    "A completed transfer of 1500 to 01712345678 was found in history, "
                    "consistent with the complaint."
                ),
                "recommended_next_action": (
                    "Open a dispute case and verify the intended recipient details "
                    "with the customer before any further action."
                ),
                "customer_reply": (
                    "Thanks for reaching out. We've located the transaction you "
                    "described and opened a case for our team to review. We'll follow "
                    "up with the next steps."
                ),
                "human_review_required": True,
                "confidence": 0.82,
                "reason_codes": ["amount_match", "recipient_in_history"],
            }
        }
    )


class ErrorResponse(BaseModel):
    """Safe, minimal error envelope.

    Never include stack traces, internal exception text, secrets, API keys, or
    tokens. ``detail`` is an optional human-readable hint only.
    """

    error: str
    detail: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "validation_error",
                "detail": "complaint must not be empty",
            }
        }
    )
