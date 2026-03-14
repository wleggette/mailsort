"""Folder description generation — LLM-based with fallback.

Generates a concise one-sentence description of a folder based on its name
and sample emails. Uses the LLM when available, falls back to a simple
name-based placeholder otherwise.

Descriptions are only generated for folders that don't already have one.
Existing descriptions are never overwritten automatically.
"""

from __future__ import annotations

import logging
from typing import Optional

from mailsort.db.database import Database
from mailsort.jmap.models import JMAPEmail

logger = logging.getLogger(__name__)

FOLDER_DESCRIPTION_PROMPT = """You are helping configure an email classifier.
Given a folder name and a sample of email subjects and senders stored in it,
write a single concise sentence (under 20 words) describing what kind of emails
belong in this folder.

Folder path: {folder_path}

Sample emails (sender — subject):
{samples}

Respond with ONLY the description sentence, no quotes, no punctuation at the end"""


def generate_folder_description(
    db: Database,
    folder_path: str,
    emails: list[JMAPEmail],
    *,
    anthropic_api_key: str = "",
    llm_model: str = "claude-haiku-4-5-20251001",
    folder_description_overrides: dict[str, str] | None = None,
) -> Optional[str]:
    """Generate and store a description for a folder if one doesn't exist.

    Skips if:
    - The folder already has a description in the DB
    - The folder has a manual override in config

    Returns the description if one was generated, None if skipped.
    """
    # Skip if manual override exists
    if folder_description_overrides and folder_path in folder_description_overrides:
        return None

    # Skip if already populated
    existing = db.execute(
        "SELECT 1 FROM folder_descriptions WHERE folder_path = ?",
        (folder_path,),
    ).fetchone()
    if existing:
        return None

    # Try LLM generation
    description = None
    if anthropic_api_key and emails:
        try:
            description = _generate_via_llm(
                folder_path, emails,
                api_key=anthropic_api_key,
                model=llm_model,
            )
        except Exception:
            logger.warning("LLM description generation failed for %s, using fallback", folder_path)

    # Fallback to name-based placeholder
    if not description:
        description = _fallback_description(folder_path)

    # Store
    try:
        db.execute(
            "INSERT INTO folder_descriptions (folder_path, description, source) "
            "VALUES (?, ?, ?)",
            (folder_path, description, "auto"),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to insert description for %s", folder_path)
        return None

    return description


def _generate_via_llm(
    folder_path: str,
    emails: list[JMAPEmail],
    *,
    api_key: str,
    model: str,
) -> Optional[str]:
    """Ask the LLM to describe a folder based on sample emails."""
    import anthropic

    samples = "\n".join(
        f"  {e.from_address} — {e.subject or '(no subject)'}"
        for e in emails[:15]
    )

    if not samples.strip():
        return None

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": FOLDER_DESCRIPTION_PROMPT.format(
                folder_path=folder_path,
                samples=samples,
            ),
        }],
    )
    text = response.content[0].text.strip()
    return text if text else None


def _fallback_description(folder_path: str) -> str:
    """Simple name-based fallback when LLM is not available."""
    leaf = folder_path.rsplit("/", 1)[-1] if "/" in folder_path else folder_path
    return f"Emails filed under {leaf}"


def generate_descriptions_for_new_folders(
    db: Database,
    known_folder_paths: set[str],
    emails_by_folder: dict[str, list[JMAPEmail]] | None = None,
    *,
    anthropic_api_key: str = "",
    llm_model: str = "claude-haiku-4-5-20251001",
    folder_description_overrides: dict[str, str] | None = None,
) -> int:
    """Generate descriptions for any folders that don't have one yet.

    Used by the orchestrator to handle newly discovered folders.
    Returns count of descriptions generated.
    """
    count = 0
    for folder_path in sorted(known_folder_paths):
        sample_emails = (emails_by_folder or {}).get(folder_path, [])
        result = generate_folder_description(
            db, folder_path, sample_emails,
            anthropic_api_key=anthropic_api_key,
            llm_model=llm_model,
            folder_description_overrides=folder_description_overrides,
        )
        if result:
            count += 1
            logger.debug("Generated description for %s: %s", folder_path, result)
    return count
