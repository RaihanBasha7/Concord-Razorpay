"""
Day 4.3 — Proposal contract.

The typed structured response returned by the LLM provider boundary. This
schema deliberately contains only the model's proposal and nothing about how
the application should route or act on it. Routing and trust-boundary
validation are intentionally out of scope for 4.3 (see 4.4).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MatchProposal(BaseModel):
    """
    The structured match proposal returned by the Groq provider boundary.

    Attributes:
        proposed_match_ids: Candidate record IDs the model proposes as a
            match for the unresolved case members. An empty list means the
            model proposes no match (evidence insufficient).
        confidence: Model confidence in [0.0, 1.0].
        rationale: Concise, evidence-based explanation for the proposal.
    """

    proposed_match_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


PROPOSAL_JSON_SCHEMA: dict = {
    "name": "match_proposal",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "proposed_match_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["proposed_match_ids", "confidence", "rationale"],
        "additionalProperties": False,
    },
}
