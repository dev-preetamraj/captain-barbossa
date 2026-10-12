"""Native approval policy: one pure verdict on one tool request against an assignment's grants.

Shell text is lexed with shlex and never evaluated or run. Reads always pass. Anything
the lexer cannot classify defers to the native permission mode unless it names a
destructive or outward verb; nothing here grants more than one call.
"""

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
    ESCALATE = "escalate"
    # No verdict: the native CLI's own permission mode decides, as if no gate ran.
    DEFER = "defer"


# The decision table: each code carries exactly one verdict.
REASONS = {
    "granted": (Verdict.ALLOW_ONCE, "every command and path is inside the assignment's grants"),
    "action-not-granted": (Verdict.DENY, "the assignment does not grant this action"),
    "unknown-tool": (Verdict.ESCALATE, "the tool has no policy"),
    "unparsed": (Verdict.DEFER, "the request is empty or the shell text does not lex"),
    "unsupported-syntax": (
        Verdict.DEFER,
        "expansion, substitution, glob, subshell, heredoc, background or multi-line shell",
    ),
    "shell": (Verdict.DEFER, "a nested shell, interpreter or eval runs code this cannot read"),
    "wrapper": (Verdict.DEFER, "an env prefix or wrapper changes how the command runs"),
    "unknown-command": (Verdict.DEFER, "the command or one of its flags has no policy"),
    "destructive": (
        Verdict.ESCALATE,
        "destructive verbs always need the user, whatever the grants",
    ),
    "outward": (Verdict.ESCALATE, "network, push or publish acts outside this machine"),
    "captain-verb": (
        Verdict.ESCALATE,
        "only the crew protocol verbs may run captain, from the interpreter it named",
    ),
    "outside-grant": (Verdict.ESCALATE, "a write outside the owned paths or inside .git"),
}
SEVERITY = {Verdict.ALLOW_ONCE: 0, Verdict.DEFER: 1, Verdict.ESCALATE: 2, Verdict.DENY: 3}

READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
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
    "prune", "update-ref", "pull",
}  # fmt: skip
OUTWARD = {
    "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet", "ftp", "push", "publish",
    "upload", "deploy",
}  # fmt: skip
# Words that make an unclassified command escalate instead of deferring.
PRIVILEGE = {"sudo", "doas", "su"}
# Interpreter payloads (python -c, node -e) are not lexed; these names in them escalate.
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
    make: tuple | None = None


def decide(request):
    decision = _decide(request)
    if decision.verdict == Verdict.DEFER and request.tool in SHELL_TOOLS:
        text = request.command if request.argv is None else shlex.join(request.argv)
        return _flagged(text or "") or decision
    return decision


def _flagged(text):
    """Escalate unclassified shell or a recipe that names a destructive, outward or captain
    verb, or an interpreter delete/write/network API, anywhere in its text."""
    paths = re.split(r"[\s;&|()<>`$'\"{}=\\,]+", text)
    names = {posixpath.basename(word) for word in paths}
    if any(name == "captain" or name.startswith("captain_barbossa") for name in names):
        return Decision("captain-verb")
    words = names | {part for word in paths for part in re.split(r"[./:]+", word)}
    if words & (DESTRUCTIVE | PRIVILEGE | DELETE_APIS) or WRITE_OPEN.search(text):
        return Decision("destructive")
    if "git" in words and (
        words & GIT_DESTRUCTIVE
        or "reflog" in words
        and words & {"expire", "delete", "drop", "write"}
    ):
        return Decision("destructive")
    if words & (OUTWARD | NETWORK_APIS):
        return Decision("outward")
    return None


def _decide(request):
    if request.tool in READ_TOOLS:
        return Decision("granted")
    if request.tool in EDIT_TOOLS:
        if not request.paths:
            return Decision("unparsed")
        return _judge(request, [(request.cwd, _Use(action="edit", writes=request.paths))])
    if request.tool not in SHELL_TOOLS:
        return Decision("unknown-tool")
    argv = list(request.argv or ())
    if argv[:1] and argv[0] in UNWRAPPED_SHELLS and len(argv) == 3:
        if argv[1] in ("-c", "-lc"):
            return _decide_text(request, argv[2])
    if argv:
        return _judge(request, [(request.cwd, _command(argv))])
    return _decide_text(request, request.command or "")


def _decide_text(request, text):
    if not text.strip():
        return Decision("unparsed")
    if _expands(text):
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


def _expands(text):
    """Multi-line text, or `$`/backtick outside single quotes; shlex drops the quoting."""
    quote, escaped = None, False
    for char in text:
        if char in "\n\r":
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


def _command(words):
    if not words:
        return _Use()
    if "=" in words[0] and not words[0].startswith(("-", "=")):
        return _Use("wrapper")
    verb, args = words[0], words[1:]
    if words[0] == sys.executable and args[:2] == ["-m", "captain_barbossa"]:
        return _captain(args[2:])
    if "/" in verb:
        return _Use("unknown-command")
    if verb == "uv":
        return _uv(args)
    if verb in ("python", "python3") and args[:1] == ["-m"]:
        return _tests(args[1:2], args[2:])
    if verb == "pytest":
        return _tests(["pytest"], args)
    if verb == "make":
        return _Use(action="test") if args == ["test"] else _Use("unknown-command", make=args)
    if verb == "git":
        return _git(args)
    if verb == "gh":
        return _gh(args)
    if verb == "find":
        return _find(args)
    if verb in DESTRUCTIVE:
        return _Use("destructive")
    if verb in OUTWARD:
        return _Use("outward")
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
    return _Use(action="captain") if verb in CAPTAIN_VERBS or memory else _Use("captain-verb")


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
    if sub == "reflog":
        if rest[:1] in (["expire"], ["delete"], ["drop"], ["write"]):
            return _Use("destructive")
        return _git(["log", *rest])
    if sub in ("stash", "worktree") and rest[:1] in (["list"], ["show"]):
        if sub == "stash" or rest[0] == "list":
            return _git(["log", *rest[1:]])
    if sub in ("branch", "tag"):
        listing = {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv"}
        return (
            _Use(action="read")
            if not rest
            or (sub == "branch" and all(arg in listing for arg in rest))
            or rest[:1] in (["--list"], ["-l"])
            and all(not arg.startswith("-") for arg in rest[1:])
            else _Use("destructive")
        )
    if sub in GIT_DESTRUCTIVE:
        return _Use("destructive")
    if sub == "add":
        return _git_add(rest)
    if sub == "commit":
        return _git_commit(rest)
    return _Use("unknown-command")


def _gh(args):
    if len(args) >= 2 and args[1] in GH_READ and not args[0].startswith("-"):
        return _Use(action="read")
    return _Use("outward")


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
        return _Use("unknown-command", writes=tuple(paths[-1:] if verb in ("cp", "mv") else paths))
    if verb == "cp":
        if len(paths) < 2:
            return _Use("unknown-command")
        return _Use(action="edit", reads=tuple(paths[:-1]), writes=(paths[-1],))
    return _Use(action="edit", writes=tuple(paths))


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
        return _Use(action="read", reads=tuple(operands))
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
    return _Use(action="test", reads=paths)


def _path_like(args):
    # A bare filename can be a symlink too; conservatively check every operand.
    return tuple(arg for arg in args if not arg.startswith("-"))


def _resolve(cwd, value):
    try:
        return str((Path(cwd) / value).resolve())
    except (OSError, RuntimeError):
        return ""  # An unreadable path or symlink loop is never inside a grant.


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
    # Ownership was canonicalized at assignment time; a new symlink cannot move it.
    owned = (posixpath.normpath(posixpath.join(project, item)) for item in request.owned)
    return _readable(request, cwd, value) and any(_inside(path, item) for item in owned)


def _judge(request, uses):
    """Most severe outcome across every simple command; deny outranks escalate."""
    codes = []
    for cwd, use in uses:
        codes.append(_judge_one(request, cwd, use))
    if not uses:
        codes.append("unparsed")
    return Decision(max(codes, key=lambda code: SEVERITY[REASONS[code][0]], default="granted"))


def _judge_one(request, cwd, use):
    if use.make is not None:
        flagged = _flagged(_make_recipes(cwd, use.make))
        if flagged:
            return flagged.code
    if use.code:
        # Even a command with no policy may not write outside the owned paths.
        if REASONS[use.code][0] == Verdict.DEFER and not _owns(request, cwd, use.writes):
            return "outside-grant"
        return use.code
    if use.action is None:
        return "unparsed"
    if use.action == "captain":
        project = posixpath.normpath(request.project)
        owned = [_resolve(project, item) for item in request.owned]
        modules = [_resolve(cwd, name) for name in CAPTAIN_MODULE]
        if any(_inside(m, o) or _inside(o, m) for m in modules for o in owned):
            return "captain-verb"
        return "granted"
    granted = {"read", "search", *request.actions}
    writes = [path for path in use.writes if _resolve(cwd, path) != NULL_DEVICE]
    needed = {use.action, *(["edit"] if writes and use.action in ("read", "test") else [])}
    if needed - granted:
        return "action-not-granted"
    return "granted" if _owns(request, cwd, writes) else "outside-grant"


def _owns(request, cwd, writes):
    targets = [match for path in writes if path != NULL_DEVICE for match in _expand(cwd, path)]
    return None not in targets and all(_writable(request, cwd, path) for path in targets)


def _make_recipes(cwd, args):
    """Recipe text of the named targets and everything they reach, or "" if unreadable."""
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
                rules.setdefault(name, ([], []))[0].extend(match[2].split())
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
