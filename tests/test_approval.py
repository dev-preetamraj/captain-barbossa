import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import events, protocol, sessions, store
from captain_barbossa.approval import REASONS, Decision, Request, Verdict, decide
from captain_barbossa.crew import Crew, event_status
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase

PROJECT = "/repo"
OWNED = ("src/mine.py", "tests/test_mine.py", "docs/notes")
ALL_ACTIONS = ("edit", "test", "format", "commit")


def bash(command, actions=ALL_ACTIONS, cwd=PROJECT):
    return decide(Request("Bash", PROJECT, cwd, OWNED, actions, command=command))


class ApprovalTests(unittest.TestCase):
    def check(self, cases, run):
        for value, code in cases:
            with self.subTest(value=value):
                self.assertEqual(run(value).code, code)

    def test_copy_into_directory_checks_child_symlink_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "owned").mkdir()
            (root / "source").write_text("new")
            (root / "victim").write_text("original")
            (root / "owned" / "source").symlink_to(root / "victim")
            for command in (
                "cp source owned/",
                "cp source owned",
                "cp ./source owned/",
                "cp source victim owned/",
                "cp sour* owned/",
                "cd ./owned && cp ../source .",
                "uv run cp source owned/",
            ):
                with self.subTest(command=command):
                    request = Request(
                        "Bash", str(root), str(root), ("owned",), ("edit",), command=command
                    )
                    self.assertEqual(decide(request).code, "outside-grant")
            request = Request(
                "Bash",
                str(root),
                str(root),
                ("owned",),
                ("edit",),
                command="cp victim owned/",
            )
            self.assertEqual(decide(request).code, "granted")

    def test_recursive_copy_to_existing_destination_escalates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "source" / "sub").mkdir(parents=True)
            (root / "source" / "sub" / "file").write_text("new")
            (root / "owned" / "source" / "sub").mkdir(parents=True)
            (root / "victim").write_text("original")
            (root / "owned" / "source" / "sub" / "file").symlink_to(root / "victim")
            for command in (
                "cp -R source owned/",
                "cp -r source owned/",
                "cp -a source owned/",
                "cp --archive source owned/",
                "cp --recursive source owned/",
                "cp -aR source owned/",
                "cp -pr source owned/",
                "cp -Rp source owned/",
                "cp --arch source owned/",
                "cp --rec source owned/",
                "cp -R source/ owned/",
                "cd ./owned && cp -R ../source .",
                "uv run cp -R source owned/",
            ):
                with self.subTest(command=command):
                    request = Request(
                        "Bash", str(root), str(root), ("owned",), ("edit",), command=command
                    )
                    self.assertEqual(decide(request).verdict, Verdict.ESCALATE)
            request = Request(
                "Bash",
                str(root),
                str(root),
                ("owned",),
                ("edit",),
                command="cp -R source owned/new",
            )
            self.assertEqual(decide(request).code, "granted")

    def test_file_compilation_is_not_a_read_grant(self):
        for command in (
            "file -C -m magic",
            "file -bC -m magic",
            "file --compile -m magic",
            "file --comp -m magic",
            "file -m -- -C",
        ):
            with self.subTest(command=command):
                self.assertEqual(bash(command, actions=()).code, "unknown-command")
        self.assertEqual(bash("file -b src/mine.py", actions=()).code, "granted")

    def test_special_command_redirections_require_owned_edit_grants(self):
        protocol_call = f"{shlex.quote(sys.executable)} -m captain_barbossa inbox Will"
        for command in ("cd .", protocol_call, "git status", "mystery"):
            with self.subTest(command=command):
                self.assertEqual(
                    bash(command + " > src/mine.py", actions=()).code, "action-not-granted"
                )
                self.assertEqual(bash(command + " > other").code, "outside-grant")
        self.assertEqual(bash("cd . > src/mine.py").code, "granted")
        self.assertEqual(bash(protocol_call + " > /dev/null", actions=()).code, "granted")

    def test_shell_cd_failure_and_pipeline_never_authorize_parent_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "owned").mkdir()
            for command in (
                "cd owned | cat; echo marker > victim",
                "cd owned/missing || echo marker > victim",
                "cd owned/missing; echo marker > victim",
                "false && cd owned; echo marker > victim",
            ):
                with self.subTest(command=command):
                    request = Request(
                        "Bash", str(root), str(root), ("owned",), ("edit",), command=command
                    )
                    self.assertNotEqual(decide(request).verdict, Verdict.ALLOW_ONCE)
                    subprocess.run(
                        ["/bin/sh", "-c", command],
                        cwd=root,
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual((root / "victim").read_text(), "marker\n")
            command = "cd ./owned && echo safe > safe"
            self.assertEqual(
                decide(
                    Request("Bash", str(root), str(root), ("owned",), ("edit",), command=command)
                ).code,
                "granted",
            )

    def test_bare_cd_is_searched_under_cdpath_so_it_never_authorizes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "owned").mkdir()
            (root / "decoy" / "owned").mkdir(parents=True)
            env = {"PATH": "/usr/bin:/bin", "CDPATH": str(root / "decoy")}

            def run(command):
                subprocess.run(
                    ["/bin/bash", "-c", command],
                    cwd=root,
                    env=env,
                    check=True,
                    capture_output=True,
                    timeout=10,
                )

            def request(command):
                return Request("Bash", str(root), str(root), ("owned",), ("edit",), command=command)

            bare = "cd owned && echo marker > victim"
            self.assertEqual(decide(request(bare)).code, "unsupported-syntax")
            run(bare)
            self.assertTrue((root / "decoy" / "owned" / "victim").exists())
            self.assertFalse((root / "owned" / "victim").exists())
            explicit = "cd ./owned && echo marker > victim"
            self.assertEqual(decide(request(explicit)).code, "granted")
            run(explicit)
            self.assertTrue((root / "owned" / "victim").exists())
            self.assertEqual(decide(request(f"cd {root}/owned && echo x > y")).code, "granted")

    def test_git_executable_read_options_are_not_read_grants(self):
        for command in (
            "git grep --open-files-in-pager=helper needle",
            "git grep --open-files=helper needle",
            "git grep -Ohelper needle",
            "git grep -nOhelper needle",
            "git log --textconv",
            "git diff --ext-diff",
            "git show --ext",
        ):
            with self.subTest(command=command):
                self.assertNotEqual(bash(command, actions=()).verdict, Verdict.ALLOW_ONCE)
        self.assertEqual(bash("git grep needle -- --open-files-in-pager").code, "granted")

    def test_every_code_has_one_closed_verdict(self):
        self.assertEqual({verdict for verdict, _ in REASONS.values()}, set(Verdict))
        self.assertEqual(Decision("granted").verdict, Verdict.ALLOW_ONCE)
        self.assertEqual(Decision("destructive").verdict, Verdict.ESCALATE)
        self.assertEqual(Decision("action-not-granted").verdict, Verdict.DENY)

    def test_shell_inside_grants_is_allowed_once(self):
        self.check(
            [
                ("git status", "granted"),
                ("git diff -- src/other.py | head -50", "granted"),
                ("cat src/other.py && grep -n foo docs/x.md", "granted"),
                ("sed -n 1,40p src/other.py", "granted"),
                ("make test 2>&1 | tail -20", "granted"),
                ("uv run --locked python -m unittest tests.test_mine -v", "granted"),
                ("uv run --locked python -m unittest discover -s tests -v", "granted"),
                ("ruff check src/other.py", "granted"),
                ("ruff format src/mine.py", "granted"),
                ("echo done > docs/notes/out.txt", "granted"),
                ("ls > /dev/null 2>&1", "granted"),
                ("cp src/other.py docs/notes/", "granted"),
                ("git add src/mine.py && git commit -m 'fix: x; y' -- src/mine.py", "granted"),
                ("cd ./src && cat mine.py", "granted"),
                ("cd src && cat mine.py", "unsupported-syntax"),
                ("find src -name x.py", "granted"),
            ],
            bash,
        )

    def test_read_only_git_forms_and_null_redirects_need_no_edit_grant(self):
        for command in (
            "git reflog",
            "git reflog show HEAD",
            "git reflog -5",
            "git branch -a",
            "git branch --list feature",
            "git tag --list v1",
            "git tag",
            "git stash list",
            "git stash show",
            "git worktree list",
            "ls 2>/dev/null",
            "ls >/dev/null 2>&1",
            "ls &>/dev/null",
            "ls 2>/dev/../dev/null",
        ):
            with self.subTest(command=command):
                self.assertEqual(bash(command, actions=()).code, "granted")
        for command in (
            "git reflog expire --all",
            "git reflog delete HEAD@{0}",
            "git reflog drop HEAD",
            "git reflog write HEAD a b message",
            "git branch --list -D feature",
            "git tag --list --delete v1",
            "git branch feature",
            "git tag v1",
            "git stash pop",
            "git worktree prune",
            "git -C /repo reflog expire --all",
        ):
            with self.subTest(command=command):
                self.assertEqual(bash(command).code, "destructive")
        self.assertEqual(bash("ls 2>/tmp/output", actions=()).code, "action-not-granted")

    def test_reads_anywhere_and_routine_edits_pass_without_a_prompt(self):
        # Issue #1: these asked even in auto mode.
        self.check(
            [
                ("cat /etc/passwd", "granted"),
                ("cat .git/config", "granted"),
                ("cd .. && ls", "granted"),
                (
                    "ls /Users/x/Downloads 2>/dev/null; ls /Users/x/Downloads | grep -i OND",
                    "granted",
                ),
                ("wc -l research/report-*.md", "granted"),
                ("grep -n 'def .*' src/other.py", "granted"),
                ("grep -n 'x$' src/other.py", "granted"),
                ("cat ~/secrets", "granted"),
                ("cp src/*.py docs/notes", "granted"),
                ("sed -n '/start/,/end/p' src/other.py", "granted"),
                ("sed -n '$p' src/other.py", "granted"),
                ("sed 's/a/b/g' src/other.py", "granted"),
                ("sed -i '' 's/a/b/g;3d' src/mine.py", "granted"),
                ("sed -i.bak -e 's|a|[b]|' docs/notes/x.md", "granted"),
                ("gh issue view 1", "granted"),
                ("gh pr list", "granted"),
            ],
            bash,
        )
        self.assertEqual(bash("ls", cwd="/tmp").code, "granted")

    def test_unclassified_commands_still_escalate_writes_deletes_and_recipes(self):
        self.check(
            [
                ("sed -i s/a/b/ /etc/hosts", "outside-grant"),
                ("cp -f a /etc/hosts", "outside-grant"),
                ("jq . a > /etc/hosts", "outside-grant"),
                ("python3 -c 'import os; os.remove(\"x\")'", "destructive"),
                ("python3 -c 'import shutil; shutil.rmtree(\"x\")'", "destructive"),
                ('python3 -c \'open("x", "w").write("y")\'', "destructive"),
                ('node -e \'require("fs").unlinkSync("x")\'', "destructive"),
                ("python3 -c 'import urllib.request as u; u.urlopen(\"http://x\")'", "outward"),
                ("python3 -c 'print(open(\"x\").read())'", "shell"),
                ("jq . a > docs/notes/out.json", "unknown-command"),
            ],
            bash,
        )

    def test_make_targets_escalate_when_their_recipe_pushes_tags_or_deletes(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "Makefile").write_text(
                ".DEFAULT_GOAL := help\nhelp: ## usage\n\t@echo help\n"
                "guard:\n\tgit diff --quiet\nrelease: guard ## tag\n"
                "\tgit tag v1 && git push origin v1\nship:\n\t$(MAKE) release\n"
                "clean:\n\trm -rf dist\nlint:\n\truff check .\n"
            )

            def make(command):
                return decide(Request("Bash", root, root, OWNED, ALL_ACTIONS, command=command))

            self.check(
                [
                    ("make release", "destructive"),
                    ("make ship", "destructive"),
                    ("make -C . clean", "destructive"),
                    ("make lint", "unknown-command"),
                    ("make", "unknown-command"),
                ],
                make,
            )

    def test_outward_actions_escalate(self):
        self.check(
            [
                ("curl https://example.com", "outward"),
                ("gh pr create --fill", "outward"),
                ("gh api repos/x/y", "outward"),
                ("npm publish", "outward"),
                ("uv publish", "outward"),
                ("sh -c 'curl https://example.com'", "outward"),
                ("for f in a; do curl -d @$f x; done", "outward"),
            ],
            bash,
        )

    def test_destructive_verbs_escalate_whatever_the_grants(self):
        grants = (*ALL_ACTIONS, "delete", "version")
        self.check(
            [
                ("rm src/mine.py", "destructive"),
                ("rm -rf docs/notes", "destructive"),
                ("git push", "destructive"),
                ("git reset --hard", "destructive"),
                ("git checkout -- src/mine.py", "destructive"),
                ("git commit --amend -m x -- src/mine.py", "destructive"),
                ("git branch -D old", "destructive"),
                ("find docs -delete", "destructive"),
                ("cat src/mine.py; rm src/mine.py", "destructive"),
                ("ls\nrm -rf /", "destructive"),
                ("sh -c 'rm -rf beta.txt'", "destructive"),
                ("find . -exec rm {} +", "destructive"),
                ("ls | xargs rm", "destructive"),
                ("echo $(git reset --hard)", "destructive"),
                ("sudo ls", "destructive"),
                ("git pull", "destructive"),
            ],
            lambda command: bash(command, grants),
        )

    def test_read_only_probes_are_not_mistaken_for_destructive_or_captain_calls(self):
        probe = "from captain_barbossa.pane import Pane; print(Pane(1).composer(2))"
        heredoc = 'python3 - "$TMPDIR/x.json" <<EOF\nimport json,sys\nprint(json.load(open(sys.argv[1])))\nEOF'
        self.check(
            [
                (
                    "herdr pane move --help; herdr workspace create --help; herdr --version",
                    "granted",
                ),
                (
                    "herdr api schema pane.move | python3 -c 'import json,sys; json.load(sys.stdin)'",
                    "shell",
                ),
                (
                    "git branch --show-current; git status --short; sed -n 15,35p FILE; grep -n P F",
                    "granted",
                ),
                (
                    f"cd src/captain_barbossa; grep -n terminal_id *.py; {heredoc}",
                    "unsupported-syntax",
                ),
            ],
            bash,
        )
        self.check(
            [
                ("herdr pane close x; rm -rf y --help", "destructive"),
                ("herdr api schema x | python3 -c 'import os; os.remove(\"f\")'", "destructive"),
                ("echo 'import os; os.remove(1)' | python3", "destructive"),
                ('python3 -c "import captain_barbossa.cli as c; c.main()"', "captain-verb"),
                (
                    "python3 -c \"import runpy; runpy.run_module('captain_barbossa')\"",
                    "captain-verb",
                ),
                (
                    f"{heredoc.replace('python3 -', 'python3 -m captain_barbossa do push')}",
                    "captain-verb",
                ),
                ("git branch --show-current -D old", "destructive"),
                ("git push --force; git branch --show-current", "destructive"),
                (f'uv run --locked python -c "{probe}"', "captain-verb"),
                ('python3 -c "import captain_barbossa.protocol as p; p.bounce(1)"', "captain-verb"),
                ('python3 -c "from captain_barbossa.agents import crew"', "captain-verb"),
                ("./evil.sh --help", "unknown-command"),
                ("mytool --help; rm -rf x", "destructive"),
                ("mytool rm --help | python3 -c 'import os; os.remove(1)'", "destructive"),
                ("python3 evil.py --help", "shell"),
                ("uv run evil --help", "unknown-command"),
                ("herdr api schema x > out.json", "outside-grant"),
            ],
            bash,
        )

    def test_unclassified_shell_defers_to_the_native_permission_mode(self):
        self.check(
            [
                ("", "unparsed"),
                ("echo 'unclosed", "unparsed"),
                ("bash -c 'cat x'", "shell"),
                ("fish -c ls", "shell"),
                ("eval ls", "shell"),
                ("python script.py", "shell"),
                ("find . -exec cat {} +", "shell"),
                ("rg --pre cat foo", "shell"),
                ("ls | xargs cat", "shell"),
                ("echo $(whoami)", "unsupported-syntax"),
                ("echo `id`", "unsupported-syntax"),
                ('cat "$HOME/x"', "unsupported-syntax"),
                ("sleep 1 &", "unsupported-syntax"),
                ("(cd /tmp && ls)", "unsupported-syntax"),
                ("cat <<EOF", "unsupported-syntax"),
                ("FOO=1 make test", "wrapper"),
                ("git -C /tmp status", "wrapper"),
                ("make gate", "unknown-command"),
                ("sed -i s/a/b/ src/mine.py", "unknown-command"),
                ("sed 's/a/b/w out' src/mine.py", "unknown-command"),
                ("sed 's/a/b/e' src/mine.py", "unknown-command"),
                ("sed -n '/[/]/p;w x' src/mine.py", "unknown-command"),
                ("sed 1r/etc/passwd src/mine.py", "unknown-command"),
                ("git log --output=src/mine.py", "unknown-command"),
                ("git commit -a -m x", "unknown-command"),
            ],
            bash,
        )
        self.assertEqual(bash("make gate").verdict, Verdict.DEFER)

    def test_paths_outside_the_grants_escalate(self):
        self.check(
            [
                ("echo x > src/other.py", "outside-grant"),
                ("echo x > ~/x", "outside-grant"),
                ("echo x > docs/notes/{a,../../b}", "outside-grant"),
                ("echo x > docs/notes/.*", "outside-grant"),
                ("cd .. && echo x > repo/src/mine.py.bak", "outside-grant"),
                ("sed -i '' s/a/b/ src/other.py", "outside-grant"),
                ("sed -i.bak s/a/b/ src/mine.py", "outside-grant"),
                ("touch src/mine.py.bak", "outside-grant"),
                ("git add -A", "outside-grant"),
                ("git add .", "outside-grant"),
                ("git commit -m x", "outside-grant"),
                ("ruff format .", "outside-grant"),
                ("mv src/other.py src/mine.py", "outside-grant"),
                ("git add src/mine.py '>' src/other.py", "outside-grant"),
            ],
            bash,
        )

    def test_missing_action_grant_denies(self):
        self.check(
            [
                ("echo x > docs/notes/a", "action-not-granted"),
                ("make test", "action-not-granted"),
                ("git add src/mine.py", "action-not-granted"),
                ("touch docs/notes/a", "action-not-granted"),
                ("cat src/mine.py", "granted"),
            ],
            lambda command: bash(command, actions=()),
        )
        # Deny outranks escalate when one command of many is not granted.
        self.assertEqual(bash("rm x; make test", actions=()).code, "action-not-granted")

    def test_native_tools_are_judged_by_their_paths(self):
        def tool(name, paths, actions=ALL_ACTIONS):
            return decide(Request(name, PROJECT, PROJECT, OWNED, actions, paths=paths)).code

        self.assertEqual(tool("Read", ("/repo/src/other.py",)), "granted")
        self.assertEqual(tool("Grep", ()), "granted")
        self.assertEqual(
            tool("Read", ("/Users/x/.claude/projects/p/s/tool-results/a.txt",)), "granted"
        )
        self.assertEqual(tool("Edit", ("/repo/src/mine.py",)), "granted")
        self.assertEqual(tool("Write", ("/repo/docs/notes/new.md",)), "granted")
        self.assertEqual(tool("Edit", ("/repo/src/other.py",)), "outside-grant")
        self.assertEqual(tool("Edit", ("/repo/src/mine.py",), ()), "action-not-granted")
        self.assertEqual(tool("Edit", ()), "unparsed")
        self.assertEqual(tool("WebFetch", ()), "unknown-tool")
        self.assertEqual(Decision(tool("mcp__x__write", ())).verdict, Verdict.ESCALATE)

    def test_an_owned_path_outside_the_project_is_writable_but_never_its_git(self):
        owned = (*OWNED, "/elsewhere/site")

        def tool(paths):
            return decide(Request("Edit", PROJECT, PROJECT, owned, ALL_ACTIONS, paths=paths)).code

        def shell(command):
            return decide(
                Request("Bash", PROJECT, PROJECT, owned, ALL_ACTIONS, command=command)
            ).code

        self.assertEqual(tool(("/elsewhere/site/index.md",)), "granted")
        self.assertEqual(shell("echo x > /elsewhere/site/index.md"), "granted")
        self.assertEqual(tool(("/elsewhere/other.md",)), "outside-grant")
        self.assertEqual(tool(("/elsewhere/site/.git/config",)), "outside-grant")
        self.assertEqual(tool(("/elsewhere/site/.GIT/config",)), "outside-grant")
        self.assertEqual(tool(("/elsewhere/site/.captain/config",)), "outside-grant")
        self.assertEqual(tool(("/elsewhere/site/.CAPTAIN/config",)), "outside-grant")
        self.assertEqual(shell("echo x > /elsewhere/site/.git/HEAD"), "outside-grant")
        self.assertEqual(shell("echo x > /elsewhere/site/.Git/HEAD"), "outside-grant")
        self.assertEqual(shell("echo x > /elsewhere/site/.captain/settings.toml"), "outside-grant")
        # The gates that never depended on ownership stay where they were.
        self.assertEqual(shell("rm /elsewhere/site/index.md"), "destructive")

    def test_project_captain_settings_are_refused_like_assignment_ownership(self):
        for owned, path in (
            ((".captain",), f"{PROJECT}/.captain/settings.toml"),
            ((".",), f"{PROJECT}/.captain/graph.json"),
            (("src",), f"{PROJECT}/src/.Captain/x"),
        ):
            request = Request("Edit", PROJECT, PROJECT, owned, ALL_ACTIONS, paths=(path,))
            with self.subTest(path=path):
                self.assertEqual(decide(request).code, "outside-grant")
                with self.assertRaisesRegex(CaptainError, "protected-path"):
                    protocol.owned_path(Path(PROJECT), path)

    def test_crew_protocol_verbs_pass_but_captain_cannot_launder_other_work(self):
        captain = shlex.join([sys.executable, "-m", "captain_barbossa", "--session", "s1"])
        self.check(
            [
                (f"{captain} done Tia --report 'files; checks'", "granted"),
                (f"{captain} check Tia edit src/mine.py", "granted"),
                (f"{captain} ask Tia 'which file?' 2>&1", "granted"),
                (f"{captain} inbox Tia", "granted"),
                (f"{captain} memory show --scope repo", "granted"),
                (f"{captain} memory add Tia report 'summary'", "captain-verb"),
                (f"{captain} memory add a decided b --because c --scope repo", "captain-verb"),
                (f"{captain} do commit --message fix", "captain-do"),
                (f"{captain} do run test", "captain-do"),
                (f"{captain} crew --task x", "captain-verb"),
                ("python3 -m captain_barbossa done Tia", "captain-verb"),
                ("captain do push", "captain-do"),
                ("/abs/path/bin/captain do push", "captain-do"),
                ("python3 -m captain_barbossa do push", "captain-do"),
                ("cd /tmp && ../usr/bin/captain crew --task x", "captain-verb"),
                ("cd /repo/captain-barbossa && python3 -c 'import os'", "shell"),
            ],
            bash,
        )
        # `python -m` imports from its cwd first; a crew-writable module there runs as captain.
        owned = ("src",)
        request = Request("Bash", PROJECT, f"{PROJECT}/src", owned, command=f"{captain} done Tia")
        self.assertEqual(decide(request).code, "captain-verb")
        request = Request("Bash", PROJECT, PROJECT, owned, command=f"{captain} done Tia")
        self.assertEqual(decide(request).code, "granted")

    def test_protocol_verbs_pass_whatever_their_argument_text(self):
        captain = shlex.join([sys.executable, "-m", "captain_barbossa", "--session", "s1"])
        report = "ran do run test; rm and git push are\nnot run; `x` costs $5"
        self.check(
            [
                (f"{captain} done Tia --report {shlex.quote(report)}", "granted"),
                (f"{captain} ask Tia 'push now?\nor rm it?'", "granted"),
                (f"codex run; {captain} done Tia --report 'no push'", "unknown-command"),
                (f'{captain} done Tia --report "line\\\nrm"', "captain-verb"),
                (f'{captain} done Tia --report "$(rm -rf x)"', "captain-verb"),
            ],
            bash,
        )
        self.assertEqual(Decision("captain-do").verdict, Verdict.DENY)

    def test_a_report_assembled_from_a_heredoc_variable_passes_as_a_protocol_verb(self):
        captain = shlex.join([sys.executable, "-m", "captain_barbossa", "--session", "s1"])

        def report(body, rest=None, quote=""):
            rest = rest or f'{captain} done Jack --report "$REPORT"'
            return f"REPORT=$(cat <<{quote}EOF{quote}\n{body}\nEOF\n); {rest}"

        self.check(
            [
                (report("files: a.py; rm and git push were not run"), "granted"),
                (report("costs $5 and `x`", quote="'"), "granted"),
                (report("costs $5"), "captain-verb"),
                (report("x\nEOF\nrm -rf y\n", quote="'"), "captain-verb"),
                (report("ok", f'{captain} crew --task "$REPORT"'), "captain-verb"),
                (report("ok", f'{captain} do commit --message "$REPORT"'), "captain-do"),
                (report("rm -rf y", 'sh -c "$REPORT"', "'"), "destructive"),
                (report("ok", 'rm -rf "$REPORT"'), "destructive"),
                (report("ok", f"{captain} done Jack --report $REPORT"), "captain-verb"),
            ],
            bash,
        )

    def test_lookups_are_reads(self):
        self.check(
            [
                ('which captain 2>/dev/null; echo "---"; type captain 2>/dev/null', "granted"),
                ("command -v captain", "granted"),
                ("whereis git", "granted"),
                ("cat ~/.zshrc /etc/hosts", "granted"),
                ("echo 'rm -rf x' | sh", "destructive"),
            ],
            lambda command: bash(command, actions=()),
        )

    def test_argv_requests_unwrap_only_known_shells(self):
        def argv(*words):
            return decide(Request("Bash", PROJECT, PROJECT, OWNED, ALL_ACTIONS, argv=words)).code

        self.assertEqual(argv("bash", "-lc", "git status && make test"), "granted")
        self.assertEqual(argv("zsh", "-c", "rm -rf src"), "destructive")
        self.assertEqual(argv("fish", "-c", "ls"), "shell")
        self.assertEqual(argv("git", "status"), "granted")
        # An exec argv has no shell, so operators are plain arguments.
        self.assertEqual(argv("cat", ";", "src/mine.py"), "granted")

    def test_path_qualified_executables_do_not_inherit_utility_permissions(self):
        for command in ("./cat", "/repo/tools/cat", "uv run ./cat"):
            with self.subTest(command=command):
                self.assertEqual(bash(command, actions=()).verdict, Verdict.DEFER)
        request = Request("Bash", PROJECT, PROJECT, argv=("/repo/bash", "-c", "cat file"))
        self.assertEqual(decide(request).verdict, Verdict.DEFER)
        request = Request("Bash", PROJECT, PROJECT, argv=("/repo/bash", "-c", "rm file"))
        self.assertEqual(decide(request).code, "destructive")


class ApprovalHookTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack", {"name": "Jack", "agent": "jack", "incarnation_id": "first"}, self.current
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch("captain_barbossa.crew.Pane.agent_status", return_value="working"))

    def hook(self, tool="Bash", inputs=None, **extra):
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": "native-session",
            "tool_use_id": "toolu_1",
            "cwd": str(self.project),
            "tool_name": tool,
            "tool_input": inputs or {"command": "git status"},
            **extra,
        }
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps(event)])
        logged = json.loads(self.crew.events.read_text().splitlines()[-1])
        return json.loads(output.getvalue() or "{}"), logged

    def test_codex_session_start_observes_stamped_identity_without_claude_delivery(self):
        from captain_barbossa import native_history

        self.crew.record["provider"] = "codex"
        store.write_json(self.current.meta_path, self.current.meta)
        event = {"hook_event_name": "SessionStart", "session_id": "native-session"}
        with (
            patch.object(native_history, "observe") as observe,
            patch.object(events, "deliver_mail") as deliver,
            patch.object(events, "permission_decision") as permission,
            patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            events.append_event([str(self.crew.events)])
        observe.assert_called_once()
        self.assertEqual(observe.call_args.args[1]["incarnation_id"], "first")
        self.assertEqual(observe.call_args.args[1]["provider"], "codex")
        self.assertEqual(observe.call_args.args[1]["seq"], 1)
        deliver.assert_not_called()
        permission.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_unmanaged_hook_preserves_raw_event_without_inventing_provider(self):
        path = self.root / "unmanaged.jsonl"
        event = {"type": "agent-turn-complete", "timestamp": "original"}
        events.append_event([str(path), json.dumps(event)])
        self.assertEqual(json.loads(path.read_text()), event)

    def test_allow_once_uses_native_response_and_does_not_leave_an_approval_notice(self):
        output, event = self.hook("Edit", {"file_path": "src/a.py"})
        self.assertEqual(
            output["hookSpecificOutput"],
            {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}},
        )
        self.assertEqual(event["approval"], {"verdict": "allow-once", "code": "granted"})
        self.assertIsNone(event_status(event))
        self.assertEqual(self.crew.current_state().state, "working")

    def test_revoked_lifetime_refuses_native_tool_grants(self):
        from captain_barbossa import lifetime

        self.crew.record["lifetime_id"] = "a" * 32
        store.write_json(self.current.meta_path, self.current.meta)
        with patch.object(lifetime, "read", return_value={"revoked": True}):
            output, event = self.hook("Edit", {"file_path": "src/a.py"})
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(event["approval"]["code"], "action-not-granted")

    def test_denied_action_refuses_the_tool(self):
        output, event = self.hook(inputs={"command": "git commit src/a.py -m fix"})
        decision = output["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("action-not-granted", decision["message"])
        self.assertIsNone(event_status(event))

    def test_escalation_reaches_the_native_user_prompt_and_wait(self):
        for command in ("rm src/a.py", "curl https://example.com", "echo x > /etc/hosts"):
            with self.subTest(command=command):
                output, event = self.hook(inputs={"command": command})
                self.assertNotIn("hookSpecificOutput", output)
                self.assertIn("User approval required", output["systemMessage"])
                self.assertEqual(event["approval"]["verdict"], "escalate")
                self.assertEqual(event_status(event), "blocked")

    def test_malformed_missing_and_stale_requests_never_allow(self):
        for extra in ({"tool_input": []}, {"cwd": None}, {"tool_input": {"command": []}}):
            with self.subTest(extra=extra):
                output, event = self.hook(**extra)
                self.assertNotIn("hookSpecificOutput", output)
                self.assertEqual(event["approval"]["code"], "unparsed")
        store.write_json(self.current.directory / "protocol.json", {})
        output, _ = self.hook()
        self.assertNotIn("hookSpecificOutput", output)
        old = self.crew.events.with_name("jack-old.jsonl")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(old), '{"hook_event_name": "PermissionRequest"}'])
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(old.exists())

    def test_symlink_tool_path_outside_the_project_escalates(self):
        (self.project / "src").mkdir()
        (self.project / "src" / "outside").symlink_to(self.project.parent, target_is_directory=True)
        output, event = self.hook("Edit", {"file_path": "src/outside/file.py"})
        self.assertNotIn("hookSpecificOutput", output)
        self.assertEqual(event["approval"]["code"], "outside-grant")

    def test_shell_symlinks_cannot_escape_read_or_write_grants(self):
        (self.project / "src").mkdir()
        (self.project / "other").mkdir()
        (self.project / "src" / "link").symlink_to(self.project / "other")
        (self.project / "secret").symlink_to(self.root / "secret")
        output, event = self.pretool({"command": "echo x > src/link/file"})
        self.assertEqual(event["approval"]["code"], "outside-grant")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")
        # Reads pass wherever a link points; only writes are bound to the owned paths.
        _, event = self.pretool({"command": "cat ./secret"})
        self.assertEqual(event["approval"]["code"], "granted")

    def test_retargeting_an_owned_path_does_not_move_its_grant(self):
        (self.project / "other").mkdir()
        (self.project / "src").symlink_to(self.project / "other")
        _, event = self.pretool({"command": "echo x > src/file"})
        self.assertEqual(event["approval"]["code"], "outside-grant")

    def pretool(self, inputs, tool="Bash"):
        return self.hook(tool, inputs, hook_event_name="PreToolUse", tool_use_id="toolu_1")

    def test_pretooluse_enforces_grants_without_a_native_prompt(self):
        output, event = self.pretool({"file_path": "src/a.py"}, "Edit")
        self.assertEqual(
            output["hookSpecificOutput"],
            {"hookEventName": "PreToolUse", "permissionDecision": "allow"},
        )
        self.assertEqual(event["hook_event_name"], "PermissionDecision")
        output, _ = self.pretool({"command": "git commit src/a.py -m fix"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "action-not-granted", output["hookSpecificOutput"]["permissionDecisionReason"]
        )
        for command in ("rm -rf beta.txt", "curl https://example.com"):
            with self.subTest(command=command):
                output, event = self.pretool({"command": command})
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")
                self.assertEqual(event["hook_event_name"], "PreToolUse")
                self.assertEqual(self.crew.current_state().state, "blocked")

    def test_pretool_ask_reports_command_and_reason_before_permissionrequest(self):
        command = "rm " + "x" * 1700
        self.pretool({"command": command})
        response = protocol.wait(self.crew, 0)
        self.assertEqual(response["status"], "awaiting_approval")
        self.assertIn(command, response["summary"])
        self.assertIn(REASONS["destructive"][1], response["summary"])
        self.assertIn("[destructive]", response["summary"])
        protocol.poll(self.crew, response["delivery_id"])
        self.hook(inputs={"command": command})
        self.assertEqual(protocol.poll(self.crew)["status"], "working")
        self.pretool({"command": "rm another-file"})
        second = protocol.wait(self.crew, 0)
        self.assertEqual(second["status"], "awaiting_approval")
        self.assertIn("rm another-file", second["summary"])
        self.assertNotEqual(second["delivery_id"], response["delivery_id"])

    def test_unclassified_commands_leave_the_decision_to_the_native_mode(self):
        inputs = {"command": "make gate"}
        output, event = self.pretool(inputs)
        self.assertEqual(output, {})
        self.assertEqual(event["hook_event_name"], "PermissionDecision")
        self.assertEqual(event["approval"]["verdict"], "defer")
        # If the native mode prompts anyway, the prompt is pending for wait, with no verdict.
        output, event = self.hook(inputs=inputs)
        self.assertEqual(output, {})
        self.assertEqual(event["hook_event_name"], "PermissionRequest")
        self.assertEqual(event_status(event), "blocked")

    def test_pretooluse_leaves_tools_without_policy_to_the_native_flow(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps({
                "hook_event_name": "PreToolUse", "cwd": str(self.project),
                "tool_name": "WebFetch", "tool_input": {"url": "https://example.com"},
            })])  # fmt: skip
        self.assertEqual(output.getvalue(), "")
        recorded = [json.loads(line) for line in self.crew.events.read_text().splitlines()]
        self.assertEqual([event["hook_event_name"] for event in recorded], ["PreToolUse"])
        self.assertNotIn("approval", recorded[0])

    def test_a_pretooluse_ask_is_revalidated_without_duplicate_notice(self):
        inputs = {"command": "rm -rf beta.txt"}
        self.pretool(inputs)
        with patch.object(events, "decide", wraps=decide) as fresh:
            output, _ = self.hook(inputs=inputs)
        fresh.assert_called_once()
        self.assertEqual(output, {})
        logged = [json.loads(line) for line in self.crew.events.read_text().splitlines()]
        self.assertEqual(
            [e["hook_event_name"] for e in logged], ["PreToolUse", "PermissionRequest"]
        )
        self.assertEqual(logged[-1]["approval"]["pretool"], "toolu_1")
        self.assertNotIn("source", logged[-1]["approval"])
        self.assertEqual(event_status(logged[-1]), "blocked")
        # A second prompt for the same command is a new call, decided afresh.
        output, event = self.hook(inputs=inputs)
        self.assertIn("User approval required", output["systemMessage"])
        self.assertNotIn("pretool", event["approval"])

    def test_a_reprompted_allow_is_revalidated_logged_and_consumed(self):
        inputs = {"file_path": "src/a.py"}
        self.pretool(inputs, "Edit")
        with patch.object(events, "decide", wraps=decide) as fresh:
            output, event = self.hook("Edit", inputs)
        fresh.assert_called_once()
        self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        self.assertEqual(event["approval"]["pretool"], "toolu_1")
        self.assertEqual(len(self.crew.events.read_text().splitlines()), 2)
        _, event = self.hook("Edit", inputs)
        self.assertNotIn("pretool", event["approval"])

    def test_cached_allow_cannot_survive_revocation_or_grant_changes(self):
        inputs = {"file_path": "src/a.py"}
        for change in ("revoked", "done", "actions", "paths"):
            with self.subTest(change=change):
                self.pretool(inputs, "Edit")
                state_path = self.current.directory / "protocol.json"
                original = store.read_json(state_path)
                state = store.read_json(state_path)
                assignment = state["assignments"][state["active"]["jack"]]
                if change == "done":
                    assignment["state"] = "done"
                elif change in ("actions", "paths"):
                    assignment[change] = []
                store.write_json(state_path, state)
                with patch.object(events.lifetime, "revoked", return_value=change == "revoked"):
                    output, event = self.hook("Edit", inputs)
                self.assertNotEqual(event["approval"]["verdict"], "allow-once")
                store.write_json(state_path, original)


if __name__ == "__main__":
    unittest.main()
