#!/usr/bin/env python3
# house-style: managed by tools house-style/sync.py -- edit templates/ there, then re-sync.
"""PreToolUse guard for the sibling repos opened read-only: ~/Workspace/kaylix, ~/Workspace/tools.

The workspace's multi-root `folders` make those repos Claude working directories, so
reads there are unprompted -- which is wanted. The `ask` rules in settings.json cover
the Edit/Write/NotebookEdit tools for every sibling but kaylix, but a write that arrives
through Bash (sed -i, a redirect, cp) slips past them, and in auto mode Bash is the
preferred edit path. So this asks for any Bash command touching those paths that is not
recognizably a pure read. Unrecognized commands ask: the allowlist is the only way through.

kaylix is guarded differently, because a kaylix change is written and committed in a
worktree under `.claude/worktrees/` and approved once, at the merge:
- a command whose kaylix paths are all inside a worktree passes, and so does an Edit or
  Write there; the kaylix `ask` rules are not in settings.json, since a permission rule
  cannot exclude the worktrees and an ask outranks any allow;
- on the main checkout, a fetch, `worktree add|remove` into `.claude/worktrees/`,
  deleting a branch, and `merge --ff-only` pass: they are the steps around a change;
- `git push` to kaylix asks, whatever the branch: that prompt is the merge approval,
  fired after the committed branch was shown in chat. `push --delete` passes;
- any other write to the main checkout asks.

The command is tokenized with quotes respected and split at shell operators. Loop and
conditional keywords are skipped before the command word is read. `NAME=value`
assignments earlier in the command are substituted into later words, and `cd` into a
sibling makes the following segments count as inside it. A variable set outside the
command cannot be resolved; a writing segment that uses one asks. String inspection
only: this raises the floor; it is not a boundary.
"""

import json
import re
import shlex
import sys

# A path into a guarded sibling, anywhere in a word: ~/Workspace/<repo>, $HOME/...,
# /Users/<u>/Workspace/<repo>, ../<repo>. Group 2 is the rest of the path.
GUARDED = re.compile(
    r"(?:(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+)/Workspace|\.\.)/(kaylix|tools)"
    r"(?![\w.-])(/[^\s'\";&|()<>]*)?"
)
WORKTREES = "/.claude/worktrees/"
# Running a sibling's code is not writing to it: the program path is exempt, and these
# invocations of the tools repo's own scripts only read.
INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "perl", "ruby"}
READ_ONLY_SCRIPTS = {"sync.py": {"--check"}, "plan.py": {"check", "board", "id"},
                     "plan": {"check", "board", "id"}}
VAR = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
ASSIGN = re.compile(r"^(\w+)=(.*)$", re.S)

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
    "fetch", "merge-base", "rev-list", "ls-remote", "for-each-ref", "reflog",
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


def kinds_of(word: str) -> list:
    """The guarded repos a word points into; a kaylix worktree is "kaylix-wt"."""
    out = []
    for m in GUARDED.finditer(word):
        repo, rest = m.group(1), m.group(2) or ""
        if repo == "kaylix" and rest.startswith(WORKTREES) and len(rest) > len(WORKTREES):
            out.append("kaylix-wt")
        else:
            out.append(repo)
    return out


def program_index(words: list):
    """Index of the program being run: the command itself, or an interpreter's script."""
    if words[0].rsplit("/", 1)[-1] in INTERPRETERS:
        for i, w in enumerate(words[1:], 1):
            if w in ("-c", "-m", "-"):
                return None
            if not w.startswith("-"):
                return i
        return None
    return 0


def runs_read_only_script(words: list) -> bool:
    i = program_index(words)
    if i is None:
        return False
    wanted = READ_ONLY_SCRIPTS.get(words[i].rsplit("/", 1)[-1])
    return bool(wanted) and any(w in wanted for w in words[i + 1:i + 2] + [
        w for w in words[i + 1:] if w.startswith("--")])


def expand(word: str, env: dict) -> str:
    return VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), word)


def strip_prefix(tokens: list, env: dict) -> list:
    """Drop keywords, a `for` header and leading assignments, recording the assignments."""
    while tokens and tokens[0] in KEYWORDS:
        tokens = tokens[1:]
    if tokens and tokens[0] == "for":
        return []  # `for VAR in words`: the words are values, not a command
    while tokens and (m := ASSIGN.match(tokens[0])) and not tokens[0].startswith("-"):
        env[m.group(1)] = expand(m.group(2), env)
        tokens = tokens[1:]
    return tokens


def git_args(words: list):
    """(subcommand, the words after it) for a git command, skipping -C and friends."""
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    it = iter(range(1, len(words)))
    for i in it:
        a = words[i]
        if a in takes_value:
            next(it, None)
            continue
        if a.startswith("-"):
            continue
        return a, words[i + 1:]
    return None, []


def kaylix_step(words: list) -> bool:
    """A git step around a worktree change that may run on the kaylix main checkout."""
    if words[0].rsplit("/", 1)[-1] != "git":
        return False
    sub, rest = git_args(words)
    if sub in ("fetch", "merge-base", "rev-list", "rev-parse", "status", "log", "diff"):
        return True
    if sub == "merge":
        return "--ff-only" in rest
    if sub == "branch":
        return any(a in ("-d", "-D", "--delete") for a in rest)
    if sub == "worktree":
        action = next((a for a in rest if not a.startswith("-")), None)
        if action in ("list", "prune"):
            return True
        if action in ("add", "remove"):
            args, skip = [], False
            for a in rest[rest.index(action) + 1:]:
                if skip:
                    skip = False
                elif a in ("-b", "-B", "--reason"):
                    skip = True
                elif not a.startswith("-"):
                    args.append(a)
            return bool(args) and ("kaylix-wt" in kinds_of(args[0])
                                   or args[0].startswith(WORKTREES[1:]))
    return False


def judge(command: str):
    """None to let the command through, else (repo, why)."""
    first = GUARDED.search(command)
    if not first:
        return None
    try:
        tokens = tokenize(command)
    except ValueError:
        return first.group(1), "unparsed"
    env, cwd = {}, None
    for seg in segments(tokens):
        seg = strip_prefix(seg, env)
        if not seg:
            continue
        words = [expand(w, env) for w in seg]
        cmd = words[0].rsplit("/", 1)[-1]
        prog = program_index(words)
        kinds = [k for i, w in enumerate(words) if i != prog for k in kinds_of(w)]
        if cmd in ("cd", "pushd"):
            target = kinds_of(words[1]) if len(words) > 1 else []
            cwd = target[0] if target else None
            continue
        if cwd:
            kinds.append(cwd)
        if cmd == "git" and git_args(words)[0] == "push" and any(k.startswith("kaylix") for k in kinds):
            if "--delete" in words or "-d" in words:
                continue
            return "kaylix", "merge"
        if (segment_is_read_only(words) or runs_read_only_script(words)) \
                and not writes_via_redirect(words):
            continue
        if not kinds:
            if any("$" in w for i, w in enumerate(words) if i != prog):
                return first.group(1), "unresolved"
            continue
        rest = [k for k in kinds if k != "kaylix-wt"]
        if not rest:
            continue
        if set(rest) == {"kaylix"} and kaylix_step(words):
            continue
        return rest[0], "write"
    return None


REASONS = {
    "merge": ("This pushes to kaylix, which edwin's deploy timer publishes from main "
              "within five minutes. House rule: approve only if the committed branch, its "
              "worktree path and its diff were shown in chat. This prompt is the merge "
              "approval; the session then deploys, removes the worktree and branch, and "
              "fast-forwards the main checkout."),
    "write": ("This Bash command may modify ~/Workspace/{repo}, which this workspace opens "
              "read-only. House rule: a change there is named, shown as a diff, and approved "
              "before it is written; a kaylix change is written in a worktree under "
              ".claude/worktrees/ instead, and this command writes outside one."),
    "unresolved": ("This Bash command names ~/Workspace/{repo} and writes through a variable "
                   "this guard cannot resolve. Approve only if it stays inside what the house "
                   "rule allows for that repo."),
    "unparsed": ("This Bash command names ~/Workspace/{repo} and has unbalanced quotes, so "
                 "this guard cannot tell whether it writes there."),
    "edit": ("This edits the kaylix main checkout. House rule: a kaylix change is written in "
             "a worktree under .claude/worktrees/, committed there, and approved at the merge."),
}


def ask(repo: str, why: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": REASONS[why].format(repo=repo),
    }}, sys.stdout)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name")
    inp = payload.get("tool_input", {}) or {}
    if tool in ("Edit", "Write", "NotebookEdit"):
        # Only kaylix: the other siblings' edits are covered by the ask rules.
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if "kaylix" in kinds_of(path):
            ask("kaylix", "edit")
        return 0
    if tool != "Bash":
        return 0
    verdict = judge(inp.get("command", "") or "")
    if verdict:
        ask(*verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
