"""Confidence gate and batch email mover."""

from __future__ import annotations

import logging
from typing import Optional

from mailsort.classifier.features import ContactInfo
from mailsort.config import ThresholdsConfig
from mailsort.jmap.models import Classification, EmailFeatures, MoveDecision

logger = logging.getLogger(__name__)


def should_move(
    classification: Classification,
    features: EmailFeatures,
    contacts: dict[str, ContactInfo],
    thresholds: ThresholdsConfig,
) -> tuple[bool, Optional[str]]:
    """Apply the appropriate confidence threshold based on sender and source.

    Returns:
        (should_move, skip_reason) — skip_reason is None if should_move is True.
    """
    # Thread context bypasses thresholds — sibling sort is reliable
    if classification.source == "thread":
        return True, None

    # Rule-based: use the rule threshold
    if classification.source == "rule":
        if classification.confidence >= thresholds.rule_move:
            return True, None
        return False, "below_threshold"

    # LLM: stricter threshold for known contacts
    if classification.source == "llm":
        is_known_contact = features.from_address in contacts
        threshold = (
            thresholds.llm_move_known_contact
            if is_known_contact
            else thresholds.llm_move
        )
        if classification.confidence >= threshold:
            return True, None
        reason = "below_threshold_known_contact" if is_known_contact else "below_threshold"
        return False, reason

    return False, "unknown_source"


def build_move_decision(
    features: EmailFeatures,
    classification: Optional[Classification],
    contacts: dict[str, ContactInfo],
    thresholds: ThresholdsConfig,
    skip_reason: Optional[str] = None,
) -> MoveDecision:
    """Build a MoveDecision for an email given its classification result.

    If classification is None (all tiers failed/gated), produces a skip decision.
    """
    if classification is None:
        return MoveDecision(
            email_id=features.email_id,
            features=features,
            classification=Classification(
                folder_path="INBOX",
                confidence=0.0,
                source="system",
                reasoning=skip_reason or "no_classification",
            ),
            should_move=False,
            skip_reason=skip_reason or "no_classification",
        )

    move, reason = should_move(classification, features, contacts, thresholds)

    return MoveDecision(
        email_id=features.email_id,
        features=features,
        classification=classification,
        should_move=move,
        skip_reason=reason,
    )
