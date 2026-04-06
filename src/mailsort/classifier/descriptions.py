"""Folder description generation and regeneration — LLM-based with fallback.

Generates a concise one-sentence description of a folder based on its name
and sample emails. Uses the LLM when available, falls back to a simple
name-based placeholder otherwise.

Initial generation only creates descriptions for folders that don't have one.
Regeneration (user-initiated) replaces existing descriptions with fresh
LLM-generated ones based on recent email samples.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from mailsort.db.database import Database
from mailsort.jmap.models import JMAPEmail

logger = logging.getLogger(__name__)


@dataclass
class RegenerationResult:
    """Result of a folder description regeneration attempt."""

    folder_path: str
    old_description: str | None = None
    new_description: str | None = None
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.new_description) and not self.error and not self.skipped


@dataclass
class BulkRegenerationReport:
    """Summary of a bulk regeneration operation."""

    results: list[RegenerationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.error)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)


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


# ---------------------------------------------------------------------------
# Regeneration (user-initiated)
# ---------------------------------------------------------------------------


def regenerate_folder_description(
    db: Database,
    folder_path: str,
    emails: list[JMAPEmail],
    *,
    anthropic_api_key: str = "",
    llm_model: str = "claude-haiku-4-5-20251001",
    folder_description_overrides: dict[str, str] | None = None,
) -> RegenerationResult:
    """Regenerate the description for a single folder.

    Unlike initial generation, this replaces an existing description.
    If the LLM call fails, the old description is kept and the error is
    reported — no fallback to a generic placeholder.

    Returns a RegenerationResult with old/new descriptions and status.
    """
    result = RegenerationResult(folder_path=folder_path)

    # Skip if manual override exists in config
    if folder_description_overrides and folder_path in folder_description_overrides:
        result.skipped = True
        result.skip_reason = "manual override in config"
        return result

    # Fetch the old description (if any)
    existing = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = ?",
        (folder_path,),
    ).fetchone()
    if existing:
        result.old_description = existing["description"]

    # Require an API key for regeneration (no fallback)
    if not anthropic_api_key:
        result.error = "no Anthropic API key configured"
        return result

    if not emails:
        result.error = "no sample emails available"
        return result

    # Generate via LLM
    try:
        new_desc = _generate_via_llm(
            folder_path, emails,
            api_key=anthropic_api_key,
            model=llm_model,
        )
    except Exception as e:
        logger.warning("LLM regeneration failed for %s: %s", folder_path, e)
        result.error = str(e)
        return result

    if not new_desc:
        result.error = "LLM returned empty description"
        return result

    # Store (replace existing or insert new)
    try:
        db.execute(
            "INSERT OR REPLACE INTO folder_descriptions "
            "(folder_path, description, source, generated_at, updated_at) "
            "VALUES (?, ?, 'auto', datetime('now'), datetime('now'))",
            (folder_path, new_desc),
        )
        db.commit()
    except Exception as e:
        logger.exception("Failed to store regenerated description for %s", folder_path)
        result.error = f"database error: {e}"
        return result

    result.new_description = new_desc
    logger.info(
        "Regenerated description for %s: %r → %r",
        folder_path, result.old_description, new_desc,
    )
    return result


def regenerate_descriptions_for_folders(
    db: Database,
    jmap: "JMAPClient",
    tree: "MailboxTree",
    folder_paths: list[str],
    *,
    anthropic_api_key: str = "",
    llm_model: str = "claude-haiku-4-5-20251001",
    folder_description_overrides: dict[str, str] | None = None,
    sample_limit: int = 50,
) -> BulkRegenerationReport:
    """Regenerate descriptions for multiple folders.

    Fetches fresh email samples via JMAP for each folder and regenerates
    the description. Returns a report with per-folder results.
    """
    from mailsort.jmap.client import JMAPClient
    from mailsort.jmap.mailbox_tree import MailboxTree

    report = BulkRegenerationReport()

    for folder_path in sorted(folder_paths):
        mailbox_id = tree.id_for(folder_path)
        if not mailbox_id:
            report.results.append(RegenerationResult(
                folder_path=folder_path,
                skipped=True,
                skip_reason="folder not found in mailbox tree",
            ))
            continue

        # Fetch sample emails
        try:
            email_ids = jmap.query_folder_emails(mailbox_id, limit=sample_limit)
            emails = jmap.get_emails(email_ids) if email_ids else []
        except Exception as e:
            logger.warning("Failed to fetch emails for %s: %s", folder_path, e)
            report.results.append(RegenerationResult(
                folder_path=folder_path,
                error=f"JMAP fetch failed: {e}",
            ))
            continue

        result = regenerate_folder_description(
            db, folder_path, emails,
            anthropic_api_key=anthropic_api_key,
            llm_model=llm_model,
            folder_description_overrides=folder_description_overrides,
        )
        report.results.append(result)

    return report
