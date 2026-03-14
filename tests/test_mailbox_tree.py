"""Tests for mailbox tree building and path resolution."""

from __future__ import annotations

import pytest

from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPMailbox


def test_build_tree_paths(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)

    assert tree.path_for("mb-banks") == "INBOX/Affairs/Banks"
    assert tree.path_for("mb-orders") == "INBOX/Shopping/Orders"
    assert tree.path_for("mb-github") == "INBOX/Tech/GitHub"
    assert tree.path_for("mb-travel") == "INBOX/Travel"


def test_reverse_lookup(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)

    assert tree.id_for("INBOX/Affairs/Banks") == "mb-banks"
    assert tree.id_for("INBOX/Shopping/Orders") == "mb-orders"
    assert tree.id_for("INBOX/Tech/GitHub") == "mb-github"


def test_inbox_id(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)
    assert tree.inbox_id == "mb-inbox"


def test_system_folders_excluded(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)
    # Sent, Trash are system folders and should not appear as targets
    assert tree.path_for("mb-sent") is None
    assert tree.path_for("mb-trash") is None
    assert "INBOX/Sent" not in tree.all_folder_paths()
    assert "INBOX/Trash" not in tree.all_folder_paths()


def test_inbox_not_a_target(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)
    assert tree.path_for("mb-inbox") is None


def test_intermediate_folder_not_target(sample_mailboxes: list[JMAPMailbox]):
    # "Affairs" and "Shopping" are intermediate folders with no emails —
    # they appear in the tree but path resolution still works for their children
    tree = MailboxTree.build(sample_mailboxes)
    assert tree.path_for("mb-affairs") == "INBOX/Affairs"
    assert tree.path_for("mb-shopping") == "INBOX/Shopping"


def test_unknown_id_returns_none(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)
    assert tree.path_for("nonexistent-id") is None
    assert tree.id_for("INBOX/Does/Not/Exist") is None


def test_all_folder_paths_count(sample_mailboxes: list[JMAPMailbox]):
    tree = MailboxTree.build(sample_mailboxes)
    paths = tree.all_folder_paths()
    # Should contain all non-system, non-inbox folders
    assert "INBOX/Affairs/Banks" in paths
    assert "INBOX/Tech/GitHub" in paths
    assert len(paths) > 0


def test_cycle_detection():
    """A mailbox tree with a parentId cycle should not crash."""
    mailboxes = [
        JMAPMailbox(id="mb-inbox", name="Inbox", parentId=None, role="inbox"),
        JMAPMailbox(id="a", name="A", parentId="b", role=None),
        JMAPMailbox(id="b", name="B", parentId="a", role=None),
    ]
    # Should not raise; cyclic entries are simply skipped
    tree = MailboxTree.build(mailboxes)
    assert tree.path_for("a") is None
    assert tree.path_for("b") is None
