"""Native approval policy: one pure verdict on one tool request against an assignment's grants.

Shell text is lexed with shlex and never evaluated, expanded or run. Anything the lexer
cannot classify escalates to the user; nothing here grants more than one call.
"""

import posixpath
import shlex
import sys
from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    ALLOW_ONCE = "allow-once"
    DENY = "deny"
    ESCALATE = "escalate"


# The decision table: each code carries exactly one verdict.
REASONS = {
    "granted": (Verdict.ALLOW_ONCE, "every command and path is inside the assignment's grants"),
    "action-not-granted": (Verdict.DENY, "the assignment does not grant this action"),
    "unknown-tool": (Verdict.ESCALATE, "the tool has no policy"),
    "unparsed": (Verdict.ESCALATE, "the request is empty or the shell text does not lex"),
    "unsupported-syntax": (
        Verdict.ESCALATE,
        "expansion, substitution, glob, subshell, heredoc, background or multi-line shell",
    ),
    "shell": (Verdict.ESCALATE, "a nested shell, interpreter or eval runs code this cannot read"),
    "wrapper": (Verdict.ESCALATE, "an env prefix or wrapper changes how the command runs"),
    "unknown-command": (Verdict.ESCALATE, "the command or one of its flags has no policy"),
    "destructive": (
        Verdict.ESCALATE,
        "destructive verbs always need the user, whatever the grants",
    ),
    "outside-grant": (
        Verdict.ESCALATE,
        "a path is outside the project, inside .git, or a write outside the owned paths",
    ),
}
SEVERITY = {Verdict.ALLOW_ONCE: 0, Verdict.ESCALATE: 1, Verdict.DENY: 2}

READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash"}
# The only shells whose -c payload is unwrapped; any other shell escalates.
UNWRAPPED_SHELLS = {"sh", "bash", "zsh"}

SEPARATORS = {"&&", "||", ";", "|", "|&"}
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
FD_DUPS = {">&", "<&"}
PUNCTUATION = set("();<>|&")
# shlex drops quoting, so these are refused in the raw text wherever they appear.
RAW_UNSUPPORTED = ("$", "`", "\n", "\r")
GLOB = set("*?[{~")
NULL_DEVICE = "/dev/null"

READ_VERBS = {
    "basename", "cat", "cmp", "comm", "cut", "diff", "dirname", "du", "echo", "false",
    "file", "grep", "head", "ls", "nl", "printf", "pwd", "realpath", "stat", "tail",
    "test", "tr", "true", "wc", "which", "[",
}  # fmt: skip
SHELLS = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "eval", "source",
    ".", "exec", "python", "python3", "node", "perl", "ruby", "osascript", "awk", "xargs",
}  # fmt: skip
WRAPPERS = {
    "env", "sudo", "doas", "su", "nohup", "timeout", "gtimeout", "nice", "time", "command",
    "builtin", "stdbuf", "watch", "caffeinate",
}  # fmt: skip
DESTRUCTIVE = {
    "rm", "rmdir", "unlink", "shred", "srm", "dd", "truncate", "mkfs", "chmod", "chown",
    "chgrp", "kill", "pkill", "killall", "ln", "rsync", "launchctl", "crontab", "diskutil",
}  # fmt: skip
GIT_READ = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "grep", "describe"}
GIT_DESTRUCTIVE = {
    "push", "reset", "clean", "rebase", "checkout", "restore", "rm", "mv", "stash", "switch",
    "merge", "cherry-pick", "revert", "am", "apply", "tag", "worktree", "filter-branch", "gc",
    "prune", "reflog", "update-ref",
}  # fmt: skip
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
UV_RUN_FLAGS = {"--locked", "--frozen", "--offline", "--no-sync"}
COMMIT_VALUE_FLAGS = {"-m", "--message", "-F", "--file"}
COMMIT_FLAGS = {"-q", "--quiet", "-s", "--signoff", "-v", "--verbose"}
# Crew protocol verbs: they only validate and record through captain's own state. `do` and
# every other verb write or run work, so they stay behind the interpreter rule.
CAPTAIN_VERBS = {"done", "ask", "check", "inbox", "inspect"}
# `python -m` imports from its cwd first, so a writable module there would run as captain.
CAPTAIN_MODULE = ("captain_barbossa", "captain_barbossa.py")


@dataclass(frozen=True)
class Request:
    """One native approval prompt; project and cwd are canonical, owned is project-relative."""

    tool: str
    project: str
    cwd: str
    owned: tuple = ()
    actions: tuple = ()
    command: str | None = None
    argv: tuple | None = None
    paths: tuple = ()


@dataclass(frozen=True)
class Decision:
    code: str

    @property
    def verdict(self):
        return REASONS[self.code][0]

    @property
    def reason(self):
        return REASONS[self.code][1]


@dataclass
class _Use:
    """What one simple command needs: an action, paths it reads and paths it writes."""

    code: str | None = None
    action: str | None = None
    reads: tuple = ()
    writes: tuple = ()


def decide(request):
    if request.tool in READ_TOOLS:
        return _judge(request, [(request.cwd, _Use(action="read", reads=request.paths or (".",)))])
    if request.tool in EDIT_TOOLS:
        if not request.paths:
            return Decision("unparsed")
        return _judge(request, [(request.cwd, _Use(action="edit", writes=request.paths))])
    if request.tool not in SHELL_TOOLS:
        return Decision("unknown-tool")
    argv = list(request.argv or ())
    if argv[:1] and posixpath.basename(argv[0]) in UNWRAPPED_SHELLS and len(argv) == 3:
        if argv[1] in ("-c", "-lc"):
            return _decide_text(request, argv[2])
    if argv:
        return _judge(request, [(request.cwd, _command(argv))])
    return _decide_text(request, request.command or "")


def _decide_text(request, text):
    if not text.strip():
        return Decision("unparsed")
    if any(mark in text for mark in RAW_UNSUPPORTED):
        return Decision("unsupported-syntax")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return Decision("unparsed")
    segments = _segments(tokens)
    if segments is None:
        return Decision("unsupported-syntax")
    uses, cwd = [], request.cwd
    for words, redirects in segments:
        if words[:1] == ["cd"]:
            if len(words) != 2 or words[1] == "-":
                return Decision("unsupported-syntax")
            uses.append((cwd, _Use(action="read", reads=(words[1],))))
            cwd = _resolve(cwd, words[1])
            continue
        use = _command(words)
        for op, target in redirects:
            if op in WRITE_REDIRECTS or op == ">&":
                use.writes += (target,)
            elif op == "<":
                use.reads += (target,)
        uses.append((cwd, use))
    return _judge(request, uses)


def _segments(tokens):
    """Split tokens into (words, redirects) per simple command, or None on unsupported ops."""
    segments, words, redirects = [], [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not set(token) <= PUNCTUATION:
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


def _command(words):
    if not words:
        return _Use()
    if "=" in words[0] and not words[0].startswith(("-", "=")):
        return _Use("wrapper")
    verb, args = posixpath.basename(words[0]), words[1:]
    if words[0] == sys.executable and args[:2] == ["-m", "captain_barbossa"]:
        return _captain(args[2:])
    if verb == "uv":
        return _uv(args)
    if verb in ("python", "python3") and args[:1] == ["-m"]:
        return _tests(args[1:2], args[2:])
    if verb == "pytest":
        return _tests(["pytest"], args)
    if verb == "make":
        return _Use(action="test") if args == ["test"] else _Use("unknown-command")
    if verb == "git":
        return _git(args)
    if verb == "find":
        return _find(args)
    if verb in DESTRUCTIVE:
        return _Use("destructive")
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
        return _Use(action="read", reads=_path_like(args))
    if verb == "ruff":
        return _ruff(args)
    if verb in READ_VERBS:
        return _Use(action="read", reads=_path_like(args))
    return _Use("unknown-command")


def _captain(args):
    if args[:1] == ["--session"] and len(args) > 1:
        args = args[2:]
    verb, rest = (args[0], args[1:]) if args else ("", [])
    # Only the legacy session report; any flag could name the committed repo scope.
    report = rest[:1] == ["add"] and len(rest) == 4 and not any(a.startswith("-") for a in rest)
    memory = verb == "memory" and (rest[:1] == ["show"] or report)
    return _Use(action="captain") if verb in CAPTAIN_VERBS or memory else _Use("shell")


def _uv(args):
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
    selectors, rest = [], list(args)
    while rest:
        arg = rest.pop(0)
        if arg in value_flags:
            if not rest:
                return _Use("unparsed")
            value = rest.pop(0)
            if arg in ("-s", "-t"):
                selectors.append(value)
        elif arg in TEST_FLAGS or (unittest and arg == "discover"):
            continue
        elif arg.startswith("-"):
            return _Use("unknown-command")
        else:
            selectors.append(arg)
    return _Use(action="test", reads=_path_like(selectors))


def _git(args):
    if not args or args[0].startswith("-"):
        return _Use("wrapper")
    sub, rest = args[0], args[1:]
    if sub in GIT_READ:
        if any(arg.startswith(("--output", "--ext-diff")) for arg in rest):
            return _Use("unknown-command")
        return _Use(action="read", reads=_path_like(rest))
    if sub == "branch":
        return _Use(action="read") if not rest else _Use("destructive")
    if sub in GIT_DESTRUCTIVE:
        return _Use("destructive")
    if sub == "add":
        return _git_add(rest)
    if sub == "commit":
        return _git_commit(rest)
    return _Use("unknown-command")


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
            return _Use("destructive")
        elif arg.startswith("-"):
            return _Use("unknown-command")
        else:
            paths.append(arg)
    # The index may hold other crew's hunks, so only an explicit owned pathspec is allowed.
    return _Use(action="commit", writes=tuple(paths) or (".",))


def _find(args):
    if any(arg in FIND_DESTRUCTIVE for arg in args):
        return _Use("destructive")
    if any(arg in FIND_SHELL for arg in args):
        return _Use("shell")
    roots = []
    for arg in args:
        if arg.startswith(("-", "(", "!")):
            break
        roots.append(arg)
    return _Use(action="read", reads=tuple(roots) or (".",))


def _edit(verb, args):
    flags = [arg for arg in args if arg.startswith("-")]
    paths = [arg for arg in args if not arg.startswith("-")]
    if set(flags) - EDIT_FLAGS[verb] or (not paths and verb != "tee"):
        return _Use("unknown-command")
    if verb == "cp":
        if len(paths) < 2:
            return _Use("unknown-command")
        return _Use(action="edit", reads=tuple(paths[:-1]), writes=(paths[-1],))
    return _Use(action="edit", writes=tuple(paths))


def _sed(args):
    # Only `sed -n 'N[,M]p' FILE...`: other sed scripts can write (w) or execute (e).
    if len(args) < 2 or args[0] != "-n":
        return _Use("unknown-command")
    script = args[1].split(",")
    if len(script) > 2 or not script[-1].endswith("p"):
        return _Use("unknown-command")
    script[-1] = script[-1][:-1]
    if not all(part.isdigit() for part in script):
        return _Use("unknown-command")
    return _Use(action="read", reads=tuple(args[2:]))


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
    return _Use(action="test", reads=paths)


def _path_like(args):
    return tuple(
        arg
        for arg in args
        if not arg.startswith("-") and ("/" in arg or arg in (".", "..") or arg.startswith("~"))
    )


def _resolve(cwd, value):
    return posixpath.normpath(posixpath.join(cwd, value))


def _inside(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _readable(request, cwd, value):
    path = _resolve(cwd, value)
    if path == NULL_DEVICE:
        return True
    project = posixpath.normpath(request.project)
    if not _inside(path, project):
        return False
    return ".git" not in posixpath.relpath(path, project).split("/")


def _writable(request, cwd, value):
    path = _resolve(cwd, value)
    if path == NULL_DEVICE:
        return True
    project = posixpath.normpath(request.project)
    owned = (_resolve(project, item) for item in request.owned)
    return _readable(request, cwd, value) and any(_inside(path, item) for item in owned)


def _judge(request, uses):
    """Most severe outcome across every simple command; deny outranks escalate."""
    codes = []
    if not _inside(posixpath.normpath(request.cwd), posixpath.normpath(request.project)):
        codes.append("outside-grant")
    for cwd, use in uses:
        codes.append(_judge_one(request, cwd, use))
    if not uses:
        codes.append("unparsed")
    return Decision(max(codes, key=lambda code: SEVERITY[REASONS[code][0]], default="granted"))


def _judge_one(request, cwd, use):
    if use.code:
        return use.code
    if use.action is None:
        return "unparsed"
    if use.action == "captain":
        project = posixpath.normpath(request.project)
        owned = [_resolve(project, item) for item in request.owned]
        modules = [_resolve(cwd, name) for name in CAPTAIN_MODULE]
        if any(_inside(m, o) or _inside(o, m) for m in modules for o in owned):
            return "shell"
        return "granted"
    if any(GLOB & set(path) for path in (*use.reads, *use.writes)):
        return "unsupported-syntax"
    granted = {"read", "search", *request.actions}
    writes = [path for path in use.writes if path != NULL_DEVICE]
    needed = {use.action, *(["edit"] if writes and use.action in ("read", "test") else [])}
    if needed - granted:
        return "action-not-granted"
    if not all(_readable(request, cwd, path) for path in use.reads):
        return "outside-grant"
    if not all(_writable(request, cwd, path) for path in writes):
        return "outside-grant"
    return "granted"
