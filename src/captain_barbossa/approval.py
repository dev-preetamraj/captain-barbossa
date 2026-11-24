"""Native approval policy: one pure verdict on one tool request against an assignment's grants.

Shell text is lexed with shlex and never evaluated or run. Reads always pass. A risky
effect is flagged by class and denied with a reason, never asked: the crew runs captain
ask and the captain may grant the exact command once. Anything else the lexer cannot
classify defers to the native permission mode; nothing here grants more than one call.
"""

import fnmatch
import glob
import os
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Verdict(Enum):
    ALLOW_ONCE = "allow-once"
    DENY = "deny"
    # No verdict: the native CLI's own permission mode decides, as if no gate ran.
    DEFER = "defer"


FLAG = "Do not try another route; run captain ask."
# The decision table: each code carries exactly one verdict.
REASONS = {
    "granted": (Verdict.ALLOW_ONCE, "every command and path is inside the assignment's grants"),
    "captain-grant": (Verdict.ALLOW_ONCE, "the captain granted this exact command once"),
    "action-not-granted": (
        Verdict.DENY,
        "the assignment does not grant this action; run captain ask if the work needs it",
    ),
    "unknown-tool": (Verdict.DEFER, "the tool has no policy"),
    "unparsed": (Verdict.DEFER, "the request is empty or the shell text does not lex"),
    "unsupported-syntax": (
        Verdict.DEFER,
        "expansion, substitution, glob, subshell, heredoc, background or multi-line shell",
    ),
    "shell": (Verdict.DEFER, "a nested shell, interpreter or eval runs code this cannot read"),
    "wrapper": (Verdict.DEFER, "an env prefix or wrapper changes how the command runs"),
    "unknown-command": (Verdict.DEFER, "the command or one of its flags has no policy"),
    "flagged-loss": (
        Verdict.DENY,
        f"Flagged: loses work this assignment did not create (delete, overwrite or discard). {FLAG}",
    ),
    "flagged-history": (
        Verdict.DENY,
        f"Flagged: rewrites history or a shared ref (amend, rebase, reset, force push, "
        f"branch or tag change). {FLAG}",
    ),
    "flagged-outward": (
        Verdict.DENY,
        f"Flagged: leaves this machine (push, PR, publish, deploy, install, network write). {FLAG}",
    ),
    "flagged-boundary": (
        Verdict.DENY,
        f"Flagged: crosses the crew boundary (privilege, a protected file, a write outside the "
        f"checkout and temp, captain state, processes or services). {FLAG}",
    ),
    "flagged-crew": (Verdict.DENY, f"Flagged: another live crew owns this path. {FLAG}"),
    "captain-verb": (
        Verdict.DENY,
        "Flagged: crew run captain only as the protocol verbs, from the interpreter their "
        f"instructions name. {FLAG}",
    ),
    "captain-inline": (
        Verdict.DENY,
        "captain named inside other shell text; run the crew protocol verbs as a standalone command",
    ),
    "inline-write": (
        Verdict.DENY,
        "edit owned files with your Edit tool, not shell or inline scripts",
    ),
    "captain-do": (Verdict.DENY, "`captain do` is captain-only; report back to the captain"),
    "plugin-denied": (
        Verdict.DENY,
        "a captain plugin forbids crew this command; report back to the captain instead",
    ),
    "outside-grant": (
        Verdict.DENY,
        "a write outside the owned paths: use your edit tool, which claims an unowned "
        "checkout file, or run captain ask",
    ),
}
FLAGGED = {code for code in REASONS if code.startswith("flagged-")} | {"captain-verb"}
# Shell text split into words wherever a quote, operator or expansion could join or hide one.
# pi_history embeds this same source, so the pi gate and the Python one split identically.
WORD_BREAKS = r"[\s;&|()<>`$'\"{}=\\,]+"
# The shell joins quoted and escaped pieces into one word, so the matcher drops them first.
QUOTING = r"['\"\\]"
SEVERITY = {Verdict.ALLOW_ONCE: 0, Verdict.DEFER: 1, Verdict.DENY: 2}

READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch"}
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash"}
# The only shells whose -c payload is unwrapped; any other shell escalates.
UNWRAPPED_SHELLS = {"sh", "bash", "zsh"}

SEPARATORS = {"&&", "||", ";", "|", "|&"}
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
FD_DUPS = {">&", "<&"}
PUNCTUATION = set("();<>|&")
GLOB = set("*?[")
NULL_DEVICE = "/dev/null"

READ_VERBS = {
    "basename", "cat", "cmp", "comm", "cut", "diff", "dirname", "du", "echo", "false",
    "file", "grep", "head", "ls", "nl", "printf", "pwd", "realpath", "stat", "tail",
    "test", "tr", "true", "type", "wc", "which", "whereis", "[",
}  # fmt: skip
SHELLS = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "eval", "source",
    ".", "exec", "python", "python3", "node", "perl", "ruby", "osascript", "awk", "xargs",
}  # fmt: skip
WRAPPERS = {
    "env", "sudo", "doas", "su", "nohup", "timeout", "gtimeout", "nice", "time", "command",
    "builtin", "stdbuf", "watch", "caffeinate",
}  # fmt: skip
LOSS_VERBS = {"rm", "rmdir", "unlink", "shred", "srm", "dd", "truncate", "rsync"}
BOUNDARY_VERBS = {
    "mkfs", "chmod", "chown", "chgrp", "kill", "pkill", "killall", "ln", "launchctl",
    "crontab", "diskutil",
}  # fmt: skip
DESTRUCTIVE = LOSS_VERBS | BOUNDARY_VERBS
# Deletes judged path by path: owned and recoverable (or delete-granted) passes.
DELETE_VERBS = {"rm": {"r", "R", "f", "d", "v"}, "rmdir": {"p", "v"}, "unlink": set()}
GIT_READ = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "grep", "describe"}
GIT_HISTORY = {
    "rebase", "filter-branch", "filter-repo", "update-ref", "replace", "cherry-pick",
    "revert", "gc", "prune", "tag",
}  # fmt: skip
GIT_LOSS = {
    "reset", "clean", "checkout", "restore", "rm", "mv", "stash", "switch", "merge", "am",
    "apply", "pull", "worktree",
}  # fmt: skip
GIT_DESTRUCTIVE = GIT_HISTORY | GIT_LOSS | {"push"}
FORCE_PUSH = {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "-d"}
FORCE_PUSH |= {"--delete", "--prune"}
OUTWARD = {
    "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet", "ftp", "push", "publish",
    "upload", "deploy",
}  # fmt: skip
# Package managers: these subcommands install, publish or change what is installed.
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "pip", "pip3", "gem", "cargo", "brew", "bun", "go"}
PACKAGE_INSTALL = {
    "install", "i", "add", "ci", "update", "upgrade", "uninstall", "remove", "publish",
    "link", "get",
}  # fmt: skip
# Cluster tools whose other subcommands change remote state.
CLUSTER_READS = {"get", "describe", "logs", "explain", "version", "top", "api-resources"}
CLUSTER_TOOLS = {"kubectl", "helm", "terraform"}
# Words that flag an unclassified command instead of deferring it.
PRIVILEGE = {"sudo", "doas", "su"}
# Only interpreter payloads are scanned for these APIs; a grep for "rename" is a read.
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "deno", "bun", "php", "osascript"}
# Interpreter payloads (python -c, node -e) are not lexed; these names in them are flagged.
DELETE_APIS = {
    "remove", "unlink", "rmtree", "rmdir", "removedirs", "rename", "renames",
    "truncate", "write_text", "write_bytes", "unlinkSync", "rmSync", "rmdirSync",
    "writeFile", "writeFileSync", "appendFileSync", "renameSync", "copyfile", "move",
}  # fmt: skip
NETWORK_APIS = {"urlopen", "urlretrieve", "requests", "httpx", "socket", "urllib", "fetch"}
WRITE_OPEN = re.compile(r"open\([^)]*,\s*\\?['\"][wax]")
GH_READ = {"view", "list", "status", "diff", "checks", "search", "token"}
FIND_DESTRUCTIVE = {"-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
FIND_SHELL = {"-exec", "-execdir", "-ok", "-okdir"}
# Allowed flags per editing verb; any other flag (cp -t, mv -f ...) has no policy.
EDIT_FLAGS = {
    "touch": set(),
    "mkdir": {"-p"},
    "cp": {"-r", "-R", "-p"},
    "mv": {"-n"},
    "tee": {"-a"},
}
TEST_FLAGS = {"-v", "-q", "-x", "-f", "-b", "-s", "--failfast"}
# Routine local builds and tests a writer runs unasked: subcommands per tool, script names
# for `<manager> run`, and flags that move output or run another program.
BUILD_SUBCOMMANDS = {
    "npm": {"test", "t"}, "pnpm": {"test", "t"}, "yarn": {"test"}, "bun": {"test"},
    "cargo": {"build", "b", "test", "t", "check", "c", "clippy", "bench", "doc"},
    "go": {"build", "test", "vet"},
}  # fmt: skip
BUILD_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
BUILD_UNSAFE_FLAGS = {
    "-o", "-exec", "-toolexec", "--target-dir", "--config", "-C", "--manifest-path", "--fix",
    "--prefix", "--global", "-g",
}  # fmt: skip
# A writer may also run these without their own grant; its writes are still judged.
WRITER_ACTIONS = {"test", "format"}
UV_RUN_FLAGS = {"--locked", "--frozen", "--offline", "--no-sync"}
COMMIT_VALUE_FLAGS = {"-m", "--message", "-F", "--file"}
COMMIT_FLAGS = {"-q", "--quiet", "-s", "--signoff", "-v", "--verbose"}
# Crew protocol verbs: they only validate and record through captain's own state. `do`,
# `grant` and every other verb write or run work, so they stay behind the interpreter rule.
CAPTAIN_VERBS = {"done", "ask", "check", "inbox", "inspect"}
# Checkout files never auto-claimed: they steer agents, hooks or the gate itself.
AGENT_CONFIG = {".claude", ".codex", ".pi", ".grok", ".agents", ".mcp.json"}
PROTECTED_NAMES = {
    ".git", ".captain", "agents.md", "claude.md", "makefile", "gnumakefile", ".envrc",
    ".pre-commit-config.yaml",
} | AGENT_CONFIG  # fmt: skip
# Tools whose `<words> --help|--version` is a read, with how many words may precede the flag.
PROBE_VERBS = {"herdr": 4, "codex": 4, "claude": 4, "git": 1, "uv": 1, "python3": 0}
# `python -m` imports from its cwd first, so a writable module there would run as captain.
CAPTAIN_MODULE = ("captain_barbossa", "captain_barbossa.py")


@dataclass(frozen=True)
class Request:
    """One native approval prompt; project and cwd are canonical, owned is project-relative
    or absolute outside the project."""

    tool: str
    project: str
    cwd: str
    owned: tuple = ()
    actions: tuple = ()
    command: str | None = None
    argv: tuple | None = None
    paths: tuple = ()
    # Absolute paths a native edit tool removes (a patch's Delete File or Move source).
    deletes: tuple = ()
    # Absolute paths a native edit tool replaces whole (a patch's Add File or Move target).
    replaces: tuple = ()
    # Owned paths that came from an auto-claim; they never confer the delete grant.
    claimed: tuple = ()
    # Absolute paths safe to delete: tracked and clean, or created by this assignment.
    recoverable: tuple = ()
    # Canonical temp roots writable with the edit grant, and captain/native state inside them.
    scratch: tuple = ()
    fenced: tuple = ()
    # One-shot exact command texts the captain granted this assignment.
    grants: tuple = ()


@dataclass(frozen=True)
class Decision:
    code: str
    # For flagged-loss: deletion targets whose recoverability the caller may still prove.
    paths: tuple = ()
    # The assignment this decision was bound to, so a cached grant never crosses a handoff.
    assignment: str = ""

    @property
    def verdict(self):
        return REASONS[self.code][0]

    @property
    def reason(self):
        return REASONS[self.code][1]


@dataclass
class _Use:
    """What one simple command needs: an action and its write targets."""

    code: str | None = None
    action: str | None = None
    writes: tuple = ()
    copy_sources: tuple = ()
    recursive_copy: bool = False
    make: tuple | None = None
    text: str = ""
    # A help/version/schema probe: its output is never run, so the whole-text scan skips it.
    inert: bool = False
    deletes: tuple = ()
    # Targets lost only if they already exist as files.
    replaces: tuple = ()
    # Targets written whole: an existing claimed file must be recoverable or this
    # assignment's own; an assigned one is the crew's to overwrite.
    overwrites: tuple = ()
    # The interpreter a protocol verb ran under, when it is the sibling of captain's own.
    sibling: str | None = None


def denied(denials, command):
    """The first plugin-declared command this tool input runs, or None.

    Deliberately textual and over-broad: a declared program, by basename, ignoring case,
    quoting and any extension, or a glob that names it, followed anywhere later by the rest
    of its words in order. It does not see through variables, encoded text or script files.
    """
    text = " ".join(map(str, command)) if isinstance(command, list) else command
    if not denials or not isinstance(text, str):
        return None
    text = re.sub(QUOTING, "", text.lower())
    words = [posixpath.basename(word).split(".")[0] for word in re.split(WORD_BREAKS, text)]
    for rule in denials:
        program, *rest = (word.lower() for word in rule)
        for index, word in enumerate(words):
            if _names(word, program) and _in_order(rest, words[index + 1 :]):
                return list(rule)
    return None


def _names(word, wanted):
    """A word is the wanted name, or a glob with a literal character that matches it."""
    if word == wanted:
        return True
    return (
        bool(GLOB & set(word))
        and bool(re.search("[a-z0-9]", word))
        and (fnmatch.fnmatchcase(wanted, word))
    )


def _in_order(needed, words):
    remaining = iter(words)
    return all(any(_names(word, item) for word in remaining) for item in needed)


def decide(request):
    decision = _decide(request)
    # Lexed commands were flagged one by one in _judge_one; only unlexed text is scanned whole.
    if decision.code in ("unparsed", "unsupported-syntax") and request.tool in SHELL_TOOLS:
        decision = _flagged(command_text(request)) or decision
    if (
        decision.verdict != Verdict.ALLOW_ONCE
        and request.tool in SHELL_TOOLS
        and command_text(request)
        and command_text(request) in request.grants
    ):
        return Decision("captain-grant")
    return decision


def command_text(request):
    """The shell text a request runs: what a captain grant must match exactly."""
    return (request.command if request.argv is None else shlex.join(request.argv)) or ""


def _flagged(text):
    """Flag unclassified shell or a recipe that names a risky or captain command word
    anywhere in its text, or an interpreter payload's delete/write/network API."""
    paths = re.split(WORD_BREAKS, text)
    names = {posixpath.basename(word) for word in paths}
    if re.search(r"-m\s+captain_barbossa|captain_barbossa\.", text) or any(
        name == "captain" or word == name == "captain_barbossa"
        for word, name in ((w, posixpath.basename(w)) for w in paths)
    ):
        return Decision("captain-inline")
    words = names | {part for word in paths for part in re.split(r"[./:]+", word)}
    if words & (BOUNDARY_VERBS | PRIVILEGE):
        return Decision("flagged-boundary")
    if words & LOSS_VERBS:
        return Decision("flagged-loss")
    if "git" in words:
        if "push" in words:
            return Decision("flagged-history" if words & FORCE_PUSH else "flagged-outward")
        if words & GIT_HISTORY or (
            "reflog" in words and words & {"expire", "delete", "drop", "write"}
        ):
            return Decision("flagged-history")
        if words & GIT_LOSS:
            return Decision("flagged-loss")
    if words & OUTWARD:
        return Decision("flagged-outward")
    if any(name in INTERPRETERS or re.fullmatch(r"python3\.\d+", name) for name in names):
        if words & NETWORK_APIS:
            return Decision("flagged-outward")
        if words & DELETE_APIS or WRITE_OPEN.search(text):
            return Decision("inline-write")
    return None


def _decide(request):
    if request.tool in READ_TOOLS:
        return Decision("granted")
    if request.tool in EDIT_TOOLS:
        if not request.paths and not request.deletes:
            return Decision("unparsed")
        whole = request.paths if request.tool == "Write" else request.replaces
        use = _Use(action="edit", writes=request.paths, deletes=request.deletes, overwrites=whole)
        return _judge(request, [(request.cwd, use)])
    if request.tool not in SHELL_TOOLS:
        return Decision("unknown-tool")
    argv = list(request.argv or ())
    if argv[:1] and argv[0] in UNWRAPPED_SHELLS and len(argv) == 3:
        if argv[1] in ("-c", "-lc"):
            return _decide_text(request, argv[2])
    if argv:
        cwd, words = _own_checkout(request, request.cwd, argv)
        return _judge(request, [(cwd, _command(words))])
    return _decide_text(request, request.command or "")


HEREDOC_VAR = re.compile(
    r"\s*(\w+)=\$\(\s*cat <<(['\"]?)(\w+)\2[ \t]*\n(.*?\n)\3\n\s*\)\s*[;\n]\s*(.+)", re.S
)


def _heredoc_var(text):
    """`V=$(cat <<EOF ... EOF); cmd "$V"` as plain `cmd '<body>'`, or None. An unquoted
    heredoc expands `$` and backticks, so such a body is refused."""
    match = HEREDOC_VAR.fullmatch(text)
    if not match:
        return None
    name, quote, delimiter, body, rest = match.groups()
    # A delimiter line inside the body ends the heredoc early; bash runs what follows.
    if f"\n{delimiter}\n" in f"\n{body}" or (not quote and re.search(r"[$`\\]", body)):
        return None
    for form in (f'"${name}"', f'"${{{name}}}"'):
        rest = rest.replace(form, shlex.quote(body.removesuffix("\n")))
    return rest


def _decide_text(request, text):
    if not text.strip():
        return Decision("unparsed")
    if (rest := _heredoc_var(text)) is not None:
        text = rest
    if _expands(text):
        return _unsupported(request, text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return Decision("unparsed")
    segments = _segments(tokens)
    if segments is None:
        return _unsupported(request, text)
    # Only an AND chain guarantees that a later command sees a successful cd.
    if any(words[:1] == ["cd"] for words, _ in segments) and any(
        token in SEPARATORS - {"&&"} for token in tokens
    ):
        return _unsupported(request, text)
    uses, cwd = [], request.cwd
    for words, redirects in segments:
        # Redirections happen before the command, including cd and protocol calls.
        writes = tuple(target for op, target in redirects if op in WRITE_REDIRECTS | {">&"})
        whole = tuple(target for op, target in redirects if op in TRUNCATING)
        if writes:
            uses.append((cwd, _Use(action="read", writes=writes, overwrites=whole)))
        if words[:1] == ["cd"]:
            if (
                len(words) != 2
                or words[1].startswith(("-", "~"))
                or set(words[1]) & (GLOB | {"{", "}"})
                # A bare name is searched under shell CDPATH; only explicit forms are safe.
                or not (
                    os.path.isabs(words[1])
                    or words[1] in {".", ".."}
                    or words[1].startswith(("./", "../"))
                )
                or _resolve(cwd, words[1]) != os.path.abspath(os.path.join(cwd, words[1]))
            ):
                return _unsupported(request, text)
            uses.append((cwd, _Use(action="read")))
            cwd = _resolve(cwd, words[1])
            continue
        git_cwd, words = _own_checkout(request, cwd, words)
        uses.append((git_cwd, _command(words)))
    return _judge(request, uses)


TRUNCATING = {">", ">|", "&>", ">&"}
# Heredoc bodies are data, not commands; a `>` inside one is no redirection.
HEREDOC_BODY = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(?:.*?\n)?[ \t]*\2[ \t]*(?=\n|$)", re.S)


def _unsupported(request, text):
    """Text the lexer cannot follow still has write effects it can name: redirect targets,
    tee and cp/mv destinations. They are judged, so unreadable shell never writes freely."""
    flagged = _flagged(text)
    if flagged:
        return flagged
    targets = _unlexed_writes(HEREDOC_BODY.sub(r"<< \3", text))
    if targets is None:
        return Decision("unsupported-syntax")
    moved = "cd" in targets
    targets = [path for path in targets if path not in ("cd", NULL_DEVICE)]
    if targets and "edit" not in request.actions:
        return Decision("action-not-granted")
    # A target spelled with an expansion, or relative after a cd, names no knowable file.
    if any(
        set(path) & set("$`") or (moved and not os.path.isabs(path) and path[:1] != "~")
        for path in targets
    ):
        return Decision("outside-grant")
    if not _owns(request, request.cwd, targets):
        return Decision(_refusal(request, request.cwd, targets))
    return Decision("unsupported-syntax")


def _unlexed_writes(text):
    """Write destinations named in text the full lexer refused (plus "cd" when the directory
    may change), or None when even this loose split fails."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    targets, command = [], []
    for index, token in enumerate([*tokens, ";"]):
        if token and set(token) <= PUNCTUATION | {"\n"}:
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if command and command[-1].isdigit() and token in WRITE_REDIRECTS | FD_DUPS:
                command.pop()  # the fd of `2>`, not an operand
            if token in WRITE_REDIRECTS or (token == ">&" and not following.isdigit()):
                if following and following != "-" and not set(following) <= PUNCTUATION:
                    targets.append(following)
            if token not in WRITE_REDIRECTS | FD_DUPS | {"<"}:
                targets.extend(_command_writes(command))
                command = []
            continue
        if index and tokens[index - 1] in WRITE_REDIRECTS | FD_DUPS | {"<"}:
            continue
        command.append(token)
    return targets


def _command_writes(words):
    while words and "=" in words[0] and not words[0].startswith(("-", "=")):
        words = words[1:]
    verb = posixpath.basename(words[0]) if words else ""
    operands = [word for word in words[1:] if not word.startswith("-")]
    if verb == "cd":
        return ["cd"]
    if verb == "tee":
        return operands
    if verb in ("cp", "mv", "install") and operands:
        return operands[-1:]
    return []


def _expands(text):
    """A newline outside quotes, or `$`/backtick outside single quotes; shlex drops the quoting."""
    quote, escaped = None, False
    for char in text:
        # Backslash-newline joins lines in bash but stays in the word for shlex.
        if char in "\n\r" and (quote is None or escaped):
            return True
        if escaped:
            escaped = False
        elif quote == "'":
            quote = None if char == "'" else quote
        elif char == "\\":
            escaped = True
        elif char in "$`":
            return True
        elif char == quote:
            quote = None
        elif quote is None and char in "'\"":
            quote = char
    return False


def _segments(tokens):
    """Split tokens into (words, redirects) per simple command, or None on unsupported ops."""
    segments, words, redirects = [], [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token or not set(token) <= PUNCTUATION:  # '' is an empty word
            words.append(token)
            continue
        if token in SEPARATORS:
            if not words and not redirects:
                return None
            segments.append((words, redirects))
            words, redirects = [], []
            continue
        if token not in WRITE_REDIRECTS | FD_DUPS | {"<"} or index >= len(tokens):
            return None
        target = tokens[index]
        index += 1
        if set(target) <= PUNCTUATION:
            return None
        # shlex splits `2>&1` into 2, >&, 1, so a bare fd number before an operator is its fd.
        if words and words[-1].isdigit():
            words.pop()
        if token in FD_DUPS and (target.isdigit() or target == "-"):
            continue
        if token == "<&":
            return None
        redirects.append((token, target))
    if words or redirects:
        segments.append((words, redirects))
    return segments or None


def _own_checkout(request, cwd, words):
    """`git -C <this checkout>` is plain `git` run from the checkout, so its paths are judged
    there; any other directory keeps its wrapper verdict. Returns (cwd, words)."""
    if words[:2] == ["git", "-C"] and len(words) > 3 and words[2][:1] != "-":
        if _resolve(cwd, words[2]) == posixpath.normpath(request.project):
            return request.project, ["git", *words[3:]]
    return cwd, words


def _command(words):
    use = _classify(words)
    use.text = shlex.join(words)
    return use


def _classify(words):
    if not words:
        return _Use()
    if "=" in words[0] and not words[0].startswith(("-", "=")):
        return _Use("wrapper")
    verb, args = words[0], words[1:]
    if (verb == sys.executable or _sibling(verb)) and args[:2] == ["-m", "captain_barbossa"]:
        use = _captain(args[2:])
        use.sibling = verb if verb != sys.executable else None
        return use
    if posixpath.basename(verb) == "captain" or args[:2] == ["-m", "captain_barbossa"]:
        use = _captain(args[2:] if args[:1] == ["-m"] else args)
        return use if use.code == "captain-do" else _Use("captain-verb")
    if verb == "command" and args[:1] in (["-v"], ["-V"]):
        return _Use(action="read")
    if "/" in verb:
        return _Use("unknown-command")
    if _probe(verb, args):
        return _Use(action="read", inert=True)
    if verb == "uv":
        return _uv(args)
    if verb in ("python", "python3") and args[:1] == ["-m"]:
        return _tests(args[1:2], args[2:])
    if verb == "pytest":
        return _tests(["pytest"], args)
    if verb == "make":
        if args == ["test"]:
            return _Use(action="test", make=args)
        return _Use("unknown-command", make=args)
    if _build(verb, args):
        return _Use(action="test")
    if verb == "git":
        return _git(args)
    if verb == "gh":
        return _gh(args)
    if verb == "find":
        return _find(args)
    if verb in DELETE_VERBS:
        return _delete(verb, args)
    if verb in LOSS_VERBS:
        return _Use("flagged-loss")
    if verb in BOUNDARY_VERBS | PRIVILEGE:
        return _Use("flagged-boundary")
    if verb in OUTWARD:
        return _Use("flagged-outward")
    if verb in PACKAGE_MANAGERS and set(args[:2]) & PACKAGE_INSTALL:
        return _Use("flagged-outward")
    if verb in CLUSTER_TOOLS and args and args[0] not in CLUSTER_READS:
        return _Use("flagged-outward")
    if verb in SHELLS:
        return _Use("shell")
    if verb in WRAPPERS:
        return _Use("wrapper")
    if verb in EDIT_FLAGS:
        return _edit(verb, args)
    if verb == "sed":
        return _sed(args)
    if verb == "rg":
        if any(arg.startswith("--pre") for arg in args):
            return _Use("shell")
        return _Use(action="read")
    if verb == "ruff":
        return _ruff(args)
    if verb == "file":
        # A preceding value-taking flag may consume `--`, so scan every argument.
        for arg in args:
            if (arg.startswith("--") and "--compile".startswith(arg.split("=", 1)[0])) or (
                arg.startswith("-") and not arg.startswith("--") and "C" in arg
            ):
                return _Use("unknown-command")
    if verb in READ_VERBS:
        return _Use(action="read")
    return _Use("unknown-command")


def _probe(verb, args):
    """A help/version read of a known tool, or `herdr api schema`; nothing here runs a script."""
    plain = lambda words: all(re.fullmatch(r"[\w][\w.-]*", word) for word in words)  # noqa: E731
    if verb == "herdr" and args[:2] == ["api", "schema"]:
        return plain(args[2:])
    if args[-1:] not in (["--help"], ["--version"]):
        return False
    limit = PROBE_VERBS.get(verb)
    return limit is not None and len(args) <= limit + 1 and plain(args[:-1])


def _captain(args):
    if args[:1] == ["--session"] and len(args) > 1:
        args = args[2:]
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb == "do":
        return _Use("captain-do")
    # The CLI limits crew memory to showing repo scope and adding session scope.
    memory = verb == "memory" and rest[:1] == ["show"]
    if verb == "memory" and rest[:1] == ["add"]:
        # argparse accepts any unambiguous prefix of --scope, with = or a separate value.
        scopes = [
            value.partition("=")[2] if "=" in value else following
            for value, following in zip(rest, [*rest[1:], ""])
            if len(value.partition("=")[0]) > 3 and "--scope".startswith(value.partition("=")[0])
        ]
        memory = all(scope == "session" for scope in scopes)
    return _Use(action="captain") if verb in CAPTAIN_VERBS or memory else _Use("captain-verb")


def _build(verb, args):
    """A routine local build or test: `npm test`, `npm run build`, `cargo test`, `go vet`."""
    subcommands = BUILD_SUBCOMMANDS.get(verb)
    if not subcommands or not args:
        return False
    if any(arg.split("=", 1)[0] in BUILD_UNSAFE_FLAGS for arg in args):
        return False
    if verb in ("npm", "pnpm", "yarn", "bun") and args[0] == "run":
        return args[1:2] != [] and args[1] in BUILD_SCRIPTS
    if verb in ("yarn", "pnpm", "bun") and args[0] in BUILD_SCRIPTS:
        return True
    return args[0] in subcommands


def _sibling(verb):
    """Whether verb is python/python3 beside captain's own interpreter, by string only: the
    launcher may spell either, and resolving links would follow a crew-writable one."""
    directory, name = posixpath.split(verb)
    return name in ("python", "python3") and directory == posixpath.dirname(sys.executable)


def _delete(verb, args):
    """rm, rmdir and unlink: each target is judged by ownership and recoverability."""
    paths, options = [], True
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-") and arg != "-":
            if arg.startswith("--") or set(arg[1:]) - DELETE_VERBS[verb]:
                return _Use("flagged-loss")
        else:
            paths.append(arg)
    if not paths:
        return _Use("unparsed")
    return _Use(action="edit", deletes=tuple(paths))


def _uv(args):
    if args[:1] in (["add"], ["remove"], ["publish"]) or args[:2] in (
        ["pip", "install"],
        ["tool", "install"],
    ):
        return _Use("flagged-outward")
    if args[:1] != ["run"]:
        return _Use("unknown-command")
    rest = args[1:]
    while rest and rest[0] in UV_RUN_FLAGS:
        rest = rest[1:]
    if rest[:1] and rest[0].startswith("-"):
        return _Use("unknown-command")
    return _command(rest)


def _tests(module, args):
    if module not in (["unittest"], ["pytest"]):
        return _Use("shell")
    unittest = module == ["unittest"]
    # unittest discover takes -s/-t directories and a -p pattern; -k is a name filter.
    value_flags = {"-k", *(["-s", "-t", "-p"] if unittest else [])}
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg in value_flags:
            if not rest:
                return _Use("unparsed")
            rest.pop(0)
        elif arg in TEST_FLAGS or (unittest and arg == "discover"):
            continue
        elif arg.startswith("-"):
            return _Use("unknown-command")
    return _Use(action="test")


def _git(args):
    if not args or args[0].startswith("-"):
        return _Use("wrapper")
    sub, rest = args[0], args[1:]
    if sub in GIT_READ:
        options = rest[: rest.index("--")] if "--" in rest else rest
        executable = {"--ext-diff", "--textconv", "--open-files-in-pager"}
        for arg in options:
            flag = arg.split("=", 1)[0]
            if arg.startswith("--output"):
                return _Use("unknown-command")
            if (
                flag.startswith("--")
                and len(flag) > 2
                and any(option.startswith(flag) for option in executable)
            ) or (
                sub == "grep" and arg.startswith("-") and not arg.startswith("--") and "O" in arg
            ):
                return _Use("shell")
        return _Use(action="read")
    if sub == "reflog":
        if rest[:1] in (["expire"], ["delete"], ["drop"], ["write"]):
            return _Use("flagged-history")
        return _git(["log", *rest])
    if sub in ("stash", "worktree") and rest[:1] in (["list"], ["show"]):
        if sub == "stash" or rest[0] == "list":
            return _git(["log", *rest[1:]])
    if sub in ("branch", "tag"):
        listing = {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--show-current"}
        return (
            _Use(action="read")
            if not rest
            or (sub == "branch" and all(arg in listing for arg in rest))
            or rest[:1] in (["--list"], ["-l"])
            and all(not arg.startswith("-") for arg in rest[1:])
            else _Use("flagged-history")
        )
    if sub == "push":
        force = any(arg in FORCE_PUSH or arg.startswith("+") for arg in rest) or any(
            re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", arg) for arg in rest
        )
        return _Use("flagged-history" if force else "flagged-outward")
    if sub == "reset":
        hard = any(arg in ("--hard", "--merge", "--keep") for arg in rest)
        return _Use("flagged-loss" if hard else "flagged-history")
    if sub in ("checkout", "restore") and "--" in rest:
        # The pathspec form rewrites only the named paths; owned ones are the crew's own.
        index = rest.index("--")
        if any(not arg.startswith("-") for arg in rest[:index]) and sub == "checkout":
            return _Use("flagged-loss")
        if rest[index + 1 :] and all(
            arg in ("-q", "--quiet", "-W", "--worktree") for arg in rest[:index]
        ):
            return _Use(action="edit", writes=tuple(rest[index + 1 :]))
        return _Use("flagged-loss")
    if sub in ("rm", "mv"):
        return _git_move(sub, rest)
    if sub == "worktree":
        return _Use("flagged-boundary")
    if sub in GIT_HISTORY:
        return _Use("flagged-history")
    if sub in GIT_LOSS:
        return _Use("flagged-loss")
    if sub == "add":
        return _git_add(rest)
    if sub == "commit":
        return _git_commit(rest)
    return _Use("unknown-command")


def _git_move(sub, args):
    """git rm deletes its paths; git mv deletes its sources and writes its destination."""
    allowed = {"rm": {"-r", "-q", "--quiet", "--cached"}, "mv": {"-v", "-k", "-n"}}[sub]
    paths, options = [], True
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-"):
            if arg not in allowed:
                return _Use("flagged-loss")
        else:
            paths.append(arg)
    if sub == "rm":
        return _Use(action="edit", deletes=tuple(paths)) if paths else _Use("unparsed")
    if len(paths) < 2:
        return _Use("unparsed")
    return _Use(action="edit", writes=(paths[-1],), deletes=tuple(paths[:-1]))


def _gh(args):
    if len(args) >= 2 and args[1] in GH_READ and not args[0].startswith("-"):
        return _Use(action="read")
    return _Use("flagged-outward")


def _git_add(args):
    if any(arg in ("-A", "--all", "-u", "--update") for arg in args):
        return _Use(action="commit", writes=(".",))
    paths = [arg for arg in args if arg != "--"]
    if any(arg.startswith("-") for arg in paths):
        return _Use("unknown-command")
    # No pathspec stages nothing knowable, so it is treated as the whole tree.
    return _Use(action="commit", writes=tuple(paths) or (".",))


def _git_commit(args):
    paths, rest = [], list(args)
    while rest:
        arg = rest.pop(0)
        if arg in COMMIT_VALUE_FLAGS:
            if not rest:
                return _Use("unparsed")
            rest.pop(0)
        elif arg.startswith(("--message=", "--file=")) or arg in COMMIT_FLAGS or arg == "--":
            continue
        elif arg == "--amend":
            return _Use("flagged-history")
        elif arg == "--all" or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", arg):
            # -a stages every tracked change, other crew's and the user's included.
            return _Use("flagged-loss")
        elif arg.startswith("-"):
            return _Use("unknown-command")
        else:
            paths.append(arg)
    # The index may hold other crew's hunks, so only an explicit owned pathspec is allowed.
    return _Use(action="commit", writes=tuple(paths) or (".",))


def _find(args):
    if any(arg in FIND_DESTRUCTIVE for arg in args):
        return _Use("flagged-loss")
    if any(arg in FIND_SHELL for arg in args):
        return _Use("shell")
    return _Use(action="read")


def _edit(verb, args):
    flags = [arg for arg in args if arg.startswith("-")]
    paths = [arg for arg in args if not arg.startswith("-")]
    recursive = verb == "cp" and any(
        any(option.startswith(flag.split("=", 1)[0]) for option in ("--archive", "--recursive"))
        if flag.startswith("--") and len(flag) > 2
        else not flag.startswith("--") and bool(set(flag[1:]) & {"a", "r", "R"})
        for flag in flags
    )
    if set(flags) - EDIT_FLAGS[verb] or (not paths and verb != "tee"):
        return _Use(
            "unknown-command",
            writes=tuple(paths[-1:] if verb in ("cp", "mv") else paths),
            recursive_copy=recursive,
        )
    if verb == "cp":
        if len(paths) < 2:
            return _Use("unknown-command")
        return _Use(
            action="edit",
            copy_sources=tuple(paths[:-1]),
            writes=(paths[-1],),
            recursive_copy=recursive,
            overwrites=(paths[-1],),
        )
    if verb == "mv" and len(paths) >= 2:
        # The sources leave their place, and a plain mv replaces an existing file target.
        replaced = () if "-n" in flags else (paths[-1],)
        return _Use(
            action="edit", writes=tuple(paths), deletes=tuple(paths[:-1]), replaces=replaced
        )
    whole = tuple(paths) if verb == "tee" and "-a" not in flags else ()
    return _Use(action="edit", writes=tuple(paths), overwrites=whole)


def _sed(args):
    use = _sed_parse(args)
    short = [arg for arg in args if arg[:1] == "-" and arg[1:2] != "-"]
    if use.code and (any("i" in arg for arg in short) or any("--in-place" in a for a in args)):
        operands = [arg for arg in args if arg and not arg.startswith("-")]
        # Without -e the first operand is the script; any other one may be rewritten.
        use.writes = tuple(operands if "-e" in args else operands[1:])
    return use


def _sed_parse(args):
    scripts, operands, suffix, rest = [], [], None, list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "-e" and rest:
            scripts.append(rest.pop(0))
        elif arg == "-i":
            # BSD sed reads the next word as the backup suffix; only '' means the same to both.
            if rest[:1] != [""]:
                return _Use("unknown-command")
            suffix = rest.pop(0)
        elif arg.startswith("-i"):
            suffix = arg[2:]
        elif arg in ("-n", "-E", "-r"):
            continue
        elif arg.startswith("-"):
            return _Use("unknown-command")
        else:
            operands.append(arg)
    if not scripts and operands:
        scripts.append(operands.pop(0))
    if not scripts or not all(_sed_script(script) for script in scripts):
        return _Use("unknown-command")
    if suffix is None:
        return _Use(action="read")
    if not operands:
        return _Use("unknown-command")
    backups = tuple(path + suffix for path in operands) if suffix else ()
    return _Use(action="edit", writes=(*operands, *backups))


def _sed_script(script):
    """Only addressed p, d and s///[gpiI0-9]: w, r, e and s///w|e write files or execute."""
    index = 0
    while index < len(script):
        for _ in range(2):
            index = _sed_address(script, index)
            if index is None:
                return False
            if script[index : index + 1] != ",":
                break
            index += 1
        command = script[index : index + 1]
        index += 1
        if command == "s":
            delimiter = script[index : index + 1]
            if not delimiter or delimiter.isalnum() or delimiter in "\\ ;":
                return False
            index = _sed_delimited(script, index + 1, delimiter, brackets=False)
            if index is not None:
                index = _sed_delimited(script, index, delimiter, brackets=True)
            if index is None:
                return False
            while script[index : index + 1] and script[index] in "gpiI0123456789":
                index += 1
        elif command not in ("p", "d"):
            return False
        if index < len(script):
            if script[index] != ";":
                return False
            index += 1
    return True


def _sed_address(script, index):
    char = script[index : index + 1]
    if char == "$":
        return index + 1
    if char.isdigit():
        while script[index : index + 1].isdigit():
            index += 1
        return index
    if char == "/":
        return _sed_delimited(script, index + 1, "/", brackets=False)
    return index


def _sed_delimited(script, index, delimiter, brackets):
    """Index past the closing delimiter, or None. `brackets` is True for a replacement, where
    `[` is literal; a regex bracket may hide the delimiter, so it is refused."""
    while index < len(script):
        char = script[index]
        if char == "\\":
            index += 2
            continue
        if char == delimiter:
            return index + 1
        if char == "[" and not brackets:
            return None  # ponytail: no bracket expressions in sed regexes, parse them if missed
        index += 1
    return None


def _ruff(args):
    sub, rest = (args[0], args[1:]) if args else ("", [])
    fixing = {"check": "--fix", "format": None}
    if sub not in fixing:
        return _Use("unknown-command")
    flags = {arg for arg in rest if arg.startswith("-")}
    paths = tuple(arg for arg in rest if not arg.startswith("-"))
    if sub == "check":
        if flags - {"--fix"}:
            return _Use("unknown-command")
        writes = "--fix" in flags
    else:
        if flags - {"--check", "--diff"}:
            return _Use("unknown-command")
        writes = not flags
    if writes:
        return _Use(action="format", writes=paths or (".",))
    return _Use(action="test")


def _resolve(cwd, value):
    try:
        return str((Path(cwd) / value).resolve())
    except (OSError, RuntimeError):
        return ""  # An unreadable path or symlink loop is never inside a grant.


def _inside(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _writable(request, cwd, value):
    path = _resolve(cwd, value)
    if path == NULL_DEVICE:
        return True
    project = posixpath.normpath(request.project)
    # Ownership was canonicalized at assignment time; a new symlink cannot move it. Owned
    # paths may be absolute, outside the project. As in protocol.owned_path, no .git or
    # .captain anywhere is ever writable: .captain holds settings and the repo rulebook.
    owned = [posixpath.normpath(posixpath.join(project, item)) for item in request.owned]
    parts = {part.casefold() for part in path.split("/")}
    if not path or ".git" in parts or ".captain" in parts:
        return False
    if _inside(path, project) and protected(posixpath.relpath(path, project).split("/")):
        # Only a protected path the captain assigned by name, never one under an owned parent.
        return any(
            _inside(path, item)
            and _inside(item, project)
            and protected(posixpath.relpath(item, project).split("/"))
            for item in owned
        )
    return any(_inside(path, item) for item in owned) or _scratch(request, path)


def _assigned(request, path):
    """Inside a path the captain assigned, not one the crew auto-claimed."""
    project = posixpath.normpath(request.project)
    return any(
        _inside(path, posixpath.normpath(posixpath.join(project, item)))
        for item in request.owned
        if item not in request.claimed
    )


def _scratch(request, path):
    """A temp path outside captain and native state. A checkout inside temp makes temp no
    boundary at all, so its scratch roots are ignored."""
    project = posixpath.normpath(request.project)
    return any(
        _inside(path, root) and not _inside(project, root) for root in request.scratch
    ) and not any(_inside(path, fence) or _inside(fence, path) for fence in request.fenced)


def protected(parts):
    """Checkout-relative parts naming a file that steers agents, hooks, CI or the gate."""
    folded = [part.casefold() for part in parts]
    return bool(set(folded) & PROTECTED_NAMES) or any(
        a == ".github" and b == "workflows" for a, b in zip(folded, folded[1:])
    )


def _escape(request, path):
    """Whether a write the grants refuse also crosses the boundary: outside the checkout
    and temp, or a protected checkout file. Anything else is claimable."""
    project = posixpath.normpath(request.project)
    if not path or not _inside(path, project):
        return True
    return protected(posixpath.relpath(path, project).split("/"))


def _refusal(request, cwd, writes):
    """outside-grant, or flagged-boundary when a refused target crosses the boundary."""
    for value in writes:
        for match in _expand(cwd, value):
            if match is None:
                continue
            path = _resolve(cwd, match)
            if (
                path != NULL_DEVICE
                and not _writable(request, cwd, match)
                and _escape(request, path)
            ):
                return "flagged-boundary"
    return "outside-grant"


def _judge(request, uses):
    """Most severe outcome across every simple command; a flag outranks a plain deny."""
    codes, pending = [], []
    probes = any(use.inert for _, use in uses)
    scan = " ".join(use.text for _, use in uses if not use.inert) if probes else None
    for cwd, use in uses:
        code = _judge_one(request, cwd, use, scan)
        deletes = (*use.deletes, *(p for p in use.replaces if os.path.isfile(os.path.join(cwd, p))))
        copied = _copy_writes(cwd, use) if code == "granted" and use.copy_sources else ()
        overwrites = (*use.overwrites, *(copied or ()))
        if code == "granted" and (deletes or overwrites):
            code, waiting = _judge_deletes(request, cwd, deletes, overwrites)
            if waiting:
                pending.append(waiting)
        codes.append(code)
    if not uses:
        codes.append("unparsed")
    code = max(codes, key=_rank, default="granted")
    # Only targets awaiting a recoverability check are returned; any other flag is final.
    resolvable = code == "flagged-loss" and codes.count(code) == len(pending) > 0
    return Decision(code, tuple(p for group in pending for p in group) if resolvable else ())


def _rank(code):
    return (SEVERITY[REASONS[code][0]], code in FLAGGED)


def _judge_deletes(request, cwd, deletes, overwrites=()):
    """(code, waiting): granted, a flag, or flagged-loss waiting on owned targets the
    caller may still prove recoverable. An overwrite loses only an existing file the
    captain did not assign."""
    project = posixpath.normpath(request.project)
    claimed = [_resolve(project, item) for item in request.claimed]
    waiting = []
    for value, whole in (*((v, False) for v in deletes), *((v, True) for v in overwrites)):
        for match in _expand(cwd, value):
            if match is None:
                return "flagged-loss", ()
            path = _resolve(cwd, match)
            if whole and (not os.path.isfile(path) or _assigned(request, path)):
                continue
            if not path or path == project or not _writable(request, cwd, match):
                return ("flagged-boundary" if _escape(request, path) else "flagged-loss"), ()
            if (
                not os.path.lexists(path)
                or _scratch(request, path)
                or any(_inside(path, item) for item in request.recoverable)
                or (
                    "delete" in request.actions and not any(_inside(path, item) for item in claimed)
                )
            ):
                continue
            waiting.append(path)
    return ("flagged-loss", tuple(waiting)) if waiting else ("granted", ())


def recipe_flag(cwd, args):
    """The escalation code `make ARGS` earns from the recipes it reaches, or None."""
    recipes = _make_recipes(cwd, args)
    if recipes is None:
        return "unparsed"
    flagged = _flagged(recipes)
    if flagged:
        return flagged.code
    return "unparsed" if "$" in recipes else None


def _judge_one(request, cwd, use, scan=None):
    if use.make is not None:
        flagged = recipe_flag(cwd, use.make)
        if flagged:
            return flagged
    if use.code:
        if REASONS[use.code][0] == Verdict.DEFER:
            # Even a command with no policy may not write outside the owned paths.
            if not _owns(request, cwd, use.writes, use.recursive_copy):
                return _refusal(request, cwd, use.writes)
            # A shell or wrapper may run any other part of the text, piped in as stdin.
            whole = use.code != "unknown-command" and request.tool in SHELL_TOOLS
            text = command_text(request) if scan is None else scan
            flagged = _flagged(text if whole else use.text)
            if flagged:
                return flagged.code
        return use.code
    if use.action is None:
        return "unparsed"
    if use.action == "captain":
        project = posixpath.normpath(request.project)
        owned = [_resolve(project, item) for item in request.owned]
        modules = [_resolve(cwd, name) for name in CAPTAIN_MODULE]
        if any(_inside(m, o) or _inside(o, m) for m in modules for o in owned):
            return "captain-verb"
        # The sibling spelling is captain's own install only when it lives outside the checkout.
        if use.sibling and _inside(_resolve(cwd, posixpath.dirname(use.sibling)), project):
            return "captain-verb"
        return "granted"
    granted = {"read", "search", *request.actions}
    if "edit" in granted:
        granted |= WRITER_ACTIONS
    writes = [path for path in use.writes if _resolve(cwd, path) != NULL_DEVICE]
    needed = {use.action, *(["edit"] if writes and use.action in ("read", "test") else [])}
    if use.deletes and not writes and "delete" in granted:
        needed.discard("edit")
    if needed - granted:
        return "action-not-granted"
    if use.copy_sources:
        copied = _copy_writes(cwd, use)
        if copied is None:
            return "outside-grant"
        writes.extend(copied)
    if _owns(request, cwd, writes, use.recursive_copy):
        return "granted"
    return _refusal(request, cwd, writes)


def _copy_writes(cwd, use):
    """Files a cp into an existing directory writes, or None when a name cannot be known."""
    written = []
    for destination in _expand(cwd, use.writes[0]):
        if destination is None:
            return None
        if not os.path.isdir(os.path.join(cwd, destination)):
            continue
        for source in use.copy_sources:
            for match in _expand(cwd, source):
                if match is None:
                    return None
                written.append(posixpath.join(destination, posixpath.basename(match.rstrip("/"))))
    return written


def _owns(request, cwd, writes, recursive_copy=False):
    targets = [match for path in writes if path != NULL_DEVICE for match in _expand(cwd, path)]
    if None in targets:
        return False
    # Existing trees can contain writable symlink descendants we cannot bound.
    if recursive_copy and any(os.path.lexists(os.path.join(cwd, path)) for path in targets):
        return False
    return all(_writable(request, cwd, path) for path in targets)


def _make_recipes(cwd, args):
    """Reachable recipe text, or None when the inspected makefile is uncertain."""
    directory, makefile, goals, rest = cwd, "Makefile", [], list(args)
    while rest:
        arg = rest.pop(0)
        if arg in ("-C", "--directory", "-f", "--file", "--makefile") and rest:
            value = rest.pop(0)
            if arg in ("-C", "--directory"):
                directory = _resolve(directory, value)
            else:
                makefile = value
        elif not arg.startswith("-") and "=" not in arg:
            goals.append(arg)
    try:
        # Compare actual names: makefile aliases Makefile on case-insensitive volumes.
        if {entry.name for entry in Path(directory).iterdir()} & {"GNUmakefile", "makefile"}:
            return None
        text = (Path(directory) / makefile).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    rules, target, default = {}, None, None
    for line in text.splitlines():
        if line.startswith("\t") and target:
            for name in target:
                rules[name][1].append(line)
            continue
        match = re.match(r"([\w./%-]+(?:\s+[\w./%-]+)*)\s*::?(?!=)([^#;]*)", line)
        if match:
            target = match[1].split()
            for name in target:
                prerequisites, lines = rules.setdefault(name, ([], []))
                prerequisites.extend(match[2].split())
                remainder = line[match.end() :]
                if remainder.startswith(";"):
                    lines.append(remainder[1:])
            default = default or next((n for n in target if not n.startswith(".")), None)
        elif goal := re.match(r"\.DEFAULT_GOAL\s*:?=\s*(\S+)", line):
            default = goal[1]
        elif line.strip():
            target = None
    recipes, queue, seen = [], goals or [default], set()
    while queue:
        name = queue.pop()
        if name in seen or name not in rules:
            continue
        seen.add(name)
        prerequisites, lines = rules[name]
        recipes.extend(lines)
        recipes.extend(word for word in prerequisites if "$" in word)
        # Prerequisites and recursive $(MAKE) targets run too.
        queue += prerequisites + [word for line in lines for word in line.split() if word in rules]
    return "\n".join(recipes)


def _expand(cwd, value):
    """What a shell word can name: tilde and glob matches. Brace lists cannot be known."""
    if value.startswith("~"):
        value = os.path.expanduser(value)
    if "{" in value and ("," in value or ".." in value):
        return [None]
    if not GLOB & set(value):
        return [value]
    # bash <5.2 lets `.*` match `..`, which glob.glob never returns.
    if any(part.startswith(".") and GLOB & set(part) for part in value.split("/")):
        return [None]
    return glob.glob(value, root_dir=cwd) or [value]
