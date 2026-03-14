"""Classification pipeline orchestrator.

Resolves classification for each email through: thread context → rules → LLM.
"""

from __future__ import annotations

import logging
from typing import Optional

from mailsort.classifier.features import ContactInfo, get_contact_for_sender
from mailsort.classifier.llm import LLMClassifier
from mailsort.classifier.rules import RuleEngine
from mailsort.db.database import Database
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import Classification, EmailFeatures

logger = logging.getLogger(__name__)


class ClassificationPipeline:
    """Runs the tiered classification for a single email.

    Resolution order:
      1. Thread context (audit_log + JMAP fallback)
      2. Rule engine
      3. LLM classifier (gated by privacy checks)
    """

    def __init__(
        self,
        db: Database,
        rule_engine: RuleEngine,
        llm_classifier: Optional[LLMClassifier],
        jmap_client: JMAPClient,
        mailbox_tree: MailboxTree,
        contacts: dict[str, ContactInfo],
        folder_descriptions: str,
    ):
        self._db = db
        self._rules = rule_engine
        self._llm = llm_classifier
        self._jmap = jmap_client
        self._tree = mailbox_tree
        self._contacts = contacts
        self._folder_descriptions = folder_descriptions

    def classify(self, features: EmailFeatures) -> tuple[Optional[Classification], Optional[str]]:
        """Classify a single email.

        Returns:
            (classification, skip_reason) — classification is None only if
            all tiers failed or were gated, in which case skip_reason explains why.
        """
        # 1. Thread context
        clf = self._resolve_thread_context(features)
        if clf:
            logger.debug("Thread context hit for %s → %s", features.email_id, clf.folder_path)
            return clf, None

        # 2. Rule engine
        clf = self._rules.classify(features)
        if clf:
            logger.debug("Rule hit for %s → %s (rule %s)", features.email_id, clf.folder_path, clf.rule_id)
            return clf, None

        # 3. LLM classifier
        if self._llm is None:
            return None, "llm_unavailable"

        allowed, skip_reason = self._llm.should_call(features, self._contacts)
        if not allowed:
            logger.debug("LLM gated for %s: %s", features.email_id, skip_reason)
            return None, skip_reason

        contact = get_contact_for_sender(features, self._contacts)
        clf = self._llm.classify(features, self._folder_descriptions, contact=contact)

        if clf.reasoning == "api_error":
            return None, "llm_api_error"

        return clf, None

    # ------------------------------------------------------------------
    # Thread context resolution
    # ------------------------------------------------------------------

    def _resolve_thread_context(self, features: EmailFeatures) -> Optional[Classification]:
        """Check if a sibling in this thread has already been sorted."""
        if not features.thread_id:
            return None

        # 1. Audit log lookup
        try:
            row = self._db.execute(
                """SELECT target_folder, COUNT(*) as n
                   FROM audit_log
                   WHERE thread_id = ?
                     AND moved = 1
                     AND email_id != ?
                   GROUP BY target_folder
                   ORDER BY n DESC, created_at DESC
                   LIMIT 1""",
                (features.thread_id, features.email_id),
            ).fetchone()
        except Exception:
            logger.exception("Thread context DB lookup failed for %s", features.thread_id)
            row = None

        if row:
            return Classification(
                folder_path=row["target_folder"],
                confidence=0.95,
                source="thread",
                reasoning=f"Thread sibling already sorted here ({row['n']} prior message(s))",
            )

        # 2. JMAP fallback — check live mailboxIds of thread siblings
        return self._thread_jmap_fallback(features)

    def _thread_jmap_fallback(self, features: EmailFeatures) -> Optional[Classification]:
        """Check JMAP for thread siblings already filed outside inbox."""
        try:
            thread_email_ids = self._jmap.get_thread_email_ids(features.thread_id)
        except Exception:
            logger.debug("Thread/get failed for %s, skipping fallback", features.thread_id)
            return None

        siblings = [eid for eid in thread_email_ids if eid != features.email_id]
        if not siblings:
            return None

        try:
            sibling_emails = self._jmap.get_emails(siblings[:10], ["id", "mailboxIds"])
        except Exception:
            logger.debug("Email/get for thread siblings failed, skipping fallback")
            return None

        inbox_id = self._tree.inbox_id
        for sibling in sibling_emails:
            non_inbox = [mid for mid in sibling.mailbox_ids if mid != inbox_id]
            if non_inbox:
                folder_path = self._tree.path_for(non_inbox[0])
                if folder_path:
                    return Classification(
                        folder_path=folder_path,
                        confidence=0.90,
                        source="thread",
                        reasoning="Thread sibling found in non-inbox folder via JMAP",
                    )

        return None
