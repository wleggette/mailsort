#!/usr/bin/env python3
# house-style: managed by tools house-style/sync.py -- edit templates/ there, then re-sync.
"""PreToolUse/Bash guard for the sibling repos opened read-only: ~/Workspace/kaylix, ~/Workspace/tools.

The workspace's multi-root `folders` make those repos Claude working directories, so
reads there are unprompted -- which is wanted. The `ask` rules in settings.json cover
the Edit/Write/NotebookEdit tools, but a write that arrives through Bash (sed -i, a
redirect, cp) is matched against Bash(...) rules instead and slips past them. In auto
mode Bash is the preferred edit path, so that gap is the common case, not the rare one.

This asks for anything touching those paths that is not recognizably a pure read.
Unrecognized commands ask: the allowlist is the only way through.

The command is tokenized with quotes respected, then split at shell operators, so a `|`
inside a quoted grep pattern is not a pipe and `$(...)` opens a new segment. Loop and
conditional keywords (`for f in`, `do`, `done`, `if`, `then`, `fi`) are skipped before the
command word is read; without that a read-only `for` loop asked (measured 2026-09-16).

String inspection only. An indirect path -- a variable holding the directory, a cd
into it first -- is not caught. This raises the floor; it is not a boundary.
"""

import json
import re
import shlex
import sys

# ~/Workspace/<repo>, $HOME/Workspace/<repo>, /Users/.../Workspace/<repo>, ../<repo>
GUARDED = re.compile(
    r"(?:(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+)/Workspace|\.\.)/(kaylix|tools)(?:/|\s|$|['\"])"
)

# Commands that cannot modify the filesystem on their own. Anything not listed
# here causes a prompt, so err on the side of leaving a command out.
READ_ONLY = {
    "[", "[[", "awk", "basename", "cat", "cd", "cksum", "column", "comm", "cut", "diff",
    "dirname", "du", "echo", "file", "find", "fgrep", "egrep", "grep", "head",
    "jq", "less", "ls", "md5", "md5sum", "more", "printf", "pwd", "readlink",
    "realpath", "rg", "shasum", "sha1sum", "sha256sum", "sort", "stat", "tail",
    "test", "tree", "true", "uniq", "wc", "yq",
}

# git subcommands that only read
GIT_READ_ONLY = {
    "blame", "branch", "cat-file", "config", "describe", "diff", "grep", "log",
    "ls-files", "ls-tree", "remote", "rev-parse", "shortlog", "show", "status",
    "tag",
}

# Shell words that precede a command without being one. `for VAR in` is handled apart.
KEYWORDS = {"do", "done", "then", "fi", "if", "while", "until", "else", "elif",
            "!", "{", "}", "time", "$"}
# Operator tokens that end one simple command and start another.
OPERATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")"}
# Output redirections. A following `/dev/null` is harmless; anything else is a write.
REDIRECT_OUT = {">", ">>", "&>", "&>>", ">|", ">&"}


def tokenize(command: str) -> list:
    """Shell words and operators, quotes respected. Raises ValueError on unbalanced quotes."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def writes_via_redirect(tokens: list) -> bool:
    for i, t in enumerate(tokens):
        if t in REDIRECT_OUT:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t == ">&" and nxt.isdigit():
                continue  # fd duplication, e.g. 2>&1
            if nxt != "/dev/null":
                return True
    return False


def segments(tokens: list) -> list:
    out, cur = [], []
    for t in tokens:
        if t in OPERATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def segment_is_read_only(tokens: list) -> bool:
    while tokens and tokens[0] in KEYWORDS:
        tokens = tokens[1:]
    if tokens and tokens[0] == "for":
        tokens = tokens[3:] if len(tokens) >= 3 and tokens[2] == "in" else []
        while tokens and tokens[0] in KEYWORDS:
            tokens = tokens[1:]
    # drop leading VAR=value assignments
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return True
    cmd = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]

    if cmd == "sed":
        return not any(a == "-i" or a.startswith("--in-place") or
                       (a.startswith("-") and not a.startswith("--") and "i" in a)
                       for a in args)
    if cmd == "git":
        # -C/-c and friends take a value, which must not be read as the subcommand
        takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a in takes_value:
                skip = True
                continue
            if a.startswith("-"):
                continue
            return a in GIT_READ_ONLY
        return False
    return cmd in READ_ONLY


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    match = GUARDED.search(command)
    if not match:
        return 0
    repo = match.group(1)

    try:
        tokens = tokenize(command)
    except ValueError:
        tokens = None  # unbalanced quotes -- cannot reason about it, so ask
    if tokens is not None and not writes_via_redirect(tokens) and all(
        segment_is_read_only(s) for s in segments(tokens)
    ):
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"This Bash command touches ~/Workspace/{repo}, which this workspace "
                    f"opens for reference. It is not a recognized read-only command, so it "
                    f"may modify that repo. House rule: a change there is named, shown as "
                    f"a diff, and approved before it is written; for kaylix it is prepared "
                    f"in a worktree. Approve only if that happened."
                ),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
