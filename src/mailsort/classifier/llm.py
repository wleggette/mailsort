"""LLM classifier: Anthropic API call with structured prompt and error handling."""

from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from mailsort.classifier.features import ContactInfo, redact_preview
from mailsort.config import ClassificationConfig
from mailsort.jmap.models import Classification, EmailFeatures

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = """You are an email classifier. Given an email's metadata,
classify it into exactly one of the following folders. Respond with JSON only.

## Folder Hierarchy

{folder_descriptions}

## Email to Classify

From: {from_line}
Subject: {subject}
List-Id: {list_id}
Date: {received_at}
Preview: {preview}

## Response Format

Respond with ONLY a JSON object, no markdown, no explanation:
{{
  "folder": "INBOX/Affairs/Banks",
  "confidence": 0.92,
  "reasoning": "Chase bank transaction alert"
}}

Rules:
- "folder" must be an exact path from the list above
- "confidence" is 0.0 to 1.0 (1.0 = certain)
- If you're unsure, set confidence below 0.7
- If the email doesn't fit any folder well, use "INBOX" with low confidence
"""


class LLMClassifier:
    """Classifies emails via Anthropic's API."""

    def __init__(
        self,
        api_key: str,
        config: ClassificationConfig,
        valid_folder_paths: set[str],
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._config = config
        self._valid_folder_paths = valid_folder_paths | {"INBOX"}

    def should_call(
        self,
        features: EmailFeatures,
        contacts: dict[str, ContactInfo],
    ) -> tuple[bool, Optional[str]]:
        """Check privacy gates before invoking the LLM."""
        skip_senders = getattr(self._config, "llm_skip_senders", None) or []
        if features.from_address in skip_senders:
            return False, "llm_skip_sender"

        skip_domains = getattr(self._config, "llm_skip_domains", None) or []
        if features.from_domain in skip_domains:
            return False, "llm_skip_domain"

        allow_contacts = getattr(self._config, "llm_allow_known_contacts", True)
        if features.from_address in contacts and not allow_contacts:
            return False, "llm_skip_known_contact"

        return True, None

    def classify(
        self,
        features: EmailFeatures,
        folder_descriptions: str,
        contact: Optional[ContactInfo] = None,
    ) -> Classification:
        """Call the LLM to classify an email. Returns a Classification (never raises)."""
        from_line = features.from_address
        if contact:
            from_line = f"{features.from_address} [known contact: \"{contact.label()}\"]"

        preview = features.preview[: self._config.llm_max_preview_chars]
        redact_patterns = getattr(self._config, "llm_redact_patterns", None) or []
        if redact_patterns:
            preview = redact_preview(preview, redact_patterns)

        use_preview = getattr(self._config, "llm_use_preview", True)
        if not use_preview:
            preview = "(preview disabled)"

        prompt = CLASSIFICATION_PROMPT.format(
            folder_descriptions=folder_descriptions,
            from_line=from_line,
            subject=features.subject,
            list_id=features.list_id or "(none)",
            received_at=str(features.received_at),
            preview=preview,
        )

        try:
            response = self._client.messages.create(
                model=self._config.llm_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
        except Exception:
            logger.exception("Anthropic API call failed")
            return Classification(
                folder_path="INBOX",
                confidence=0.0,
                source="llm",
                reasoning="api_error",
            )

        return self._parse_response(raw_text)

    def _parse_response(self, raw_text: str) -> Classification:
        """Parse LLM JSON response with error handling and folder validation."""
        # Strip markdown code fences if present (LLM sometimes wraps in ```json...```)
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]  # remove first line (```json)
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            result = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("LLM returned unparseable response: %.200s", raw_text)
            return Classification(
                folder_path="INBOX",
                confidence=0.0,
                source="llm",
                reasoning="parse_error",
            )

        folder = result.get("folder", "INBOX")
        confidence = result.get("confidence", 0.0)

        if folder not in self._valid_folder_paths:
            logger.warning("LLM returned unknown folder '%s', falling back to INBOX", folder)
            folder = "INBOX"
            confidence = 0.0

        return Classification(
            folder_path=folder,
            confidence=float(confidence),
            source="llm",
            reasoning=result.get("reasoning", ""),
        )
