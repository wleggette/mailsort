"""Rule engine: SQLite-backed rule matching in specificity order."""

from __future__ import annotations

import logging
import re
from typing import Optional

from mailsort.config import ThresholdsConfig
from mailsort.db.database import Database
from mailsort.jmap.models import Classification, EmailFeatures

logger = logging.getLogger(__name__)


class RuleEngine:
    """Matches emails against stored rules and manages rule CRUD."""

    def __init__(self, db: Database, thresholds: ThresholdsConfig):
        self._db = db
        self._thresholds = thresholds

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(self, features: EmailFeatures) -> Optional[Classification]:
        """Try rules in specificity order. Return first match above threshold.

        Order:
          1. list_id       — most stable for newsletters/lists
          2. exact_sender  — high specificity
          3. sender_domain — broad, only if coherent
          4. subject_regex — lowest trust
        """
        threshold = self._thresholds.rule_move

        # 1. List-Id
        if features.list_id:
            rule = self._find_rule("list_id", features.list_id)
            if rule and rule["confidence"] >= threshold:
                self._record_hit(rule["id"])
                return self._to_classification(rule)

        # 2. Exact sender
        rule = self._find_rule("exact_sender", features.from_address)
        if rule and rule["confidence"] >= threshold:
            self._record_hit(rule["id"])
            return self._to_classification(rule)

        # 3. Sender domain
        rule = self._find_rule("sender_domain", features.from_domain)
        if rule and rule["confidence"] >= threshold:
            self._record_hit(rule["id"])
            return self._to_classification(rule)

        # 4. Subject regex — scan all active regex rules
        for rule in self._find_rules_by_type("subject_regex"):
            try:
                if re.search(rule["condition_value"], features.subject):
                    self._record_hit(rule["id"])
                    return self._to_classification(rule)
            except re.error:
                logger.warning("Bad regex in rule %d: %s", rule["id"], rule["condition_value"])

        return None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def _find_rule(self, rule_type: str, value: str) -> Optional[dict]:
        row = self._db.execute(
            """SELECT * FROM rules
               WHERE rule_type = ? AND condition_value = ? AND active = 1
               LIMIT 1""",
            (rule_type, value),
        ).fetchone()
        return dict(row) if row else None

    def _find_rules_by_type(self, rule_type: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM rules WHERE rule_type = ? AND active = 1",
            (rule_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _record_hit(self, rule_id: int) -> None:
        self._db.execute(
            "UPDATE rules SET hit_count = hit_count + 1, last_hit_at = datetime('now') WHERE id = ?",
            (rule_id,),
        )
        self._db.commit()

    @staticmethod
    def _to_classification(rule: dict) -> Classification:
        return Classification(
            folder_path=rule["target_folder_path"],
            folder_id=rule.get("target_folder_id"),
            confidence=rule["confidence"],
            source="rule",
            rule_id=rule["id"],
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_rule(
        self,
        *,
        rule_type: str,
        condition_value: str,
        target_folder_path: str,
        confidence: float = 0.90,
        source: str = "auto",
        active: bool = True,
        target_folder_id: str | None = None,
    ) -> int:
        """Insert a new rule and return its ID."""
        cursor = self._db.execute(
            """INSERT INTO rules
                   (rule_type, condition_value, target_folder_path, target_folder_id,
                    confidence, source, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (rule_type, condition_value, target_folder_path, target_folder_id,
             confidence, source, int(active)),
        )
        self._db.commit()
        return cursor.lastrowid

    def deactivate_rule(self, rule_id: int) -> None:
        self._db.execute(
            "UPDATE rules SET active = 0, updated_at = datetime('now') WHERE id = ?",
            (rule_id,),
        )
        self._db.commit()

    def find_existing_rule(self, rule_type: str, condition_value: str) -> Optional[dict]:
        """Find an active rule by type and condition value."""
        return self._find_rule(rule_type, condition_value)

    def reconcile_folders(self, live_folder_paths: set[str]) -> int:
        """Deactivate rules whose target folder no longer exists. Returns count."""
        rows = self._db.execute(
            "SELECT id, target_folder_path FROM rules WHERE active = 1"
        ).fetchall()
        deactivated = 0
        for row in rows:
            if row["target_folder_path"] not in live_folder_paths:
                self.deactivate_rule(row["id"])
                logger.warning(
                    "Deactivated rule %d: target folder '%s' no longer exists",
                    row["id"], row["target_folder_path"],
                )
                deactivated += 1
        return deactivated
