"""
Day 4.3 — Proposal service and prompt boundary.

Consumes the Layer 2 context (a reconstructed Layer2Case plus the retrieved
candidates) and produces a typed MatchProposal via the provider boundary.

Prompt boundary rules (enforced here):
  * Uses ONLY production record fields and retrieved candidates.
  * Excludes evaluation metadata: ground-truth category, has_valid_relationship,
    is_true_exception, synthetic scenario descriptions, and any evaluation
    metadata carried in raw_payload.
  * Excludes synthetic scenario_id prefixes (e.g. FEE-001, DUP-001).
  * Explicitly permits an empty proposed_match_ids when evidence is weak.
  * Does not ask the model to invent missing information.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.groq_provider import (
    GroqClientConfig,
    GroqProviderError,
    StructuredCompletionProvider,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.proposal import (
    PROPOSAL_JSON_SCHEMA,
    MatchProposal,
)
from reconciliation.retrieval import RetrievalResult


def _record_repr(record: NormalizedRecord) -> dict:
    """Render a record using only production-visible fields (never raw_payload)."""
    return {
        "record_id": record.record_id,
        "source_type": record.source_type.value,
        "order_id_hint": record.order_id_hint,
        "amount_paise": record.amount_paise,
        "date": record.date.isoformat(),
        "narration": record.narration,
    }


@dataclass(frozen=True)
class PromptResult:
    system_prompt: str
    user_prompt: str


class PromptBuilder:
    """
    Builds the minimal Groq request payload from the Layer 2 context only.

    The rendered prompts contain no scenario identifiers, no evaluation
    labels, and no raw payloads, satisfying the prompt-boundary constraint.
    """

    SYSTEM_PROMPT = (
        "You are a settlement reconciliation analyst. You are given a set of "
        "unresolved source records (the 'case') and a small set of plausible "
        "candidate records retrieved from other sources. Follow this reasoning "
        "order: (1) First examine the unresolved case records and determine "
        "whether two or more of those records themselves form a plausible "
        "reconciliation relationship (for example duplicate, fee-adjusted, "
        "partial, split, or delayed relationship). (2) Then examine the "
        "retrieved records as additional possible matches or evidence. A "
        "proposed_match_ids list may contain IDs from either the unresolved "
        "case records or the retrieved candidate records. Return an empty "
        "proposed_match_ids list only if there is insufficient evidence after "
        "considering both sets. Respond ONLY with the structured schema "
        "provided. Be concise and evidence-based. Do not invent information "
        "that is not present in the records."
    )

    def build(
        self,
        case: Layer2Case,
        retrieval_result: RetrievalResult,
    ) -> PromptResult:
        case_records = [_record_repr(r) for r in case.member_records]
        candidate_records = [
            _record_repr(c.record) for c in retrieval_result.candidates
        ]

        user_prompt = (
            "Unresolved case records:\n"
            f"{case_records}\n\n"
            "Retrieved candidate records (potential matches):\n"
            f"{candidate_records}\n\n"
            "Follow this reasoning order: (1) First examine the unresolved case "
            "records and determine whether two or more of those records themselves "
            "form a plausible reconciliation relationship (for example duplicate, "
            "fee-adjusted, partial, split, or delayed relationship). (2) Then "
            "examine the retrieved records as additional possible matches or "
            "evidence. A proposed_match_ids list may contain IDs from either the "
            "unresolved case records or the retrieved candidate records. Return "
            "an empty proposed_match_ids list only if there is insufficient "
            "evidence after considering both sets. Provide a concise, "
            "evidence-based rationale and a confidence between 0.0 and 1.0."
        )
        return PromptResult(
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )


class ProposalService:
    """
    Produces a typed MatchProposal from the Layer 2 context via the provider
    boundary. Parsing provider JSON into the proposal contract happens here;
    semantic validation of proposed IDs against the candidate set is a 4.4
    concern and is intentionally not performed.
    """

    def __init__(
        self,
        provider: StructuredCompletionProvider,
        prompt_builder: PromptBuilder | None = None,
        config: GroqClientConfig | None = None,
    ) -> None:
        self._provider = provider
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._config = config or GroqClientConfig()

    def propose(
        self,
        case: Layer2Case,
        retrieval_result: RetrievalResult,
    ) -> MatchProposal:
        prompts = self._prompt_builder.build(case, retrieval_result)
        raw = self._provider.complete_structured(
            system_prompt=prompts.system_prompt,
            user_prompt=prompts.user_prompt,
            json_schema=PROPOSAL_JSON_SCHEMA,
        )
        return self._parse(raw)

    @staticmethod
    def _parse(raw: Mapping[str, Any]) -> MatchProposal:
        try:
            return MatchProposal.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - normalize to provider error
            raise GroqProviderError(
                f"Invalid structured proposal from provider: {exc}"
            ) from exc
