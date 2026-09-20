"""Cut the suite off from the developer's own HOME and captain memory roots.

Two things outside the checkout would otherwise decide what the tests see:

- config reads its middle settings layer from Path.home()/.captain/settings.toml, so a
  developer's own uncommented setting there (a custom [placement] shape, say) turns the
  gate red on a deliberate user choice.
- a live captain session exports CAPTAIN_STATE_ROOT and CAPTAIN_TEMP_ROOT, and
  memory._root reads those before CAPTAIN_MEMORY_ROOT. A test that sets only
  CAPTAIN_MEMORY_ROOT is then overruled and writes into the real memory root, leaving a
  project directory behind on every run.

Importing this module once isolates every test in the process: imports all happen before
any test runs, and nothing reads these variables at import time. Dropping the two
inherited roots fixes the tests that set CAPTAIN_MEMORY_ROOT themselves; the default
CAPTAIN_MEMORY_ROOT below catches the ones that set no root at all. A test that wants
its own roots still sets them, and they win as usual.

It lives here rather than in tests/__init__.py because the gate runs
`unittest discover -s tests`, which makes tests/ the top-level directory and never
imports the package. tests/test_captain.py imports it; tests/test_isolation.py fails if
that import is ever dropped.
"""

import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="captain-tests-")
HOME = os.path.join(ROOT, "home")
MEMORY_ROOT = os.path.join(ROOT, "memory")
# Everything a live captain session exports into its crew. The roots overrule
# CAPTAIN_MEMORY_ROOT; the other two would let a test read the live session.
INHERITED = ("CAPTAIN_STATE_ROOT", "CAPTAIN_TEMP_ROOT", "CAPTAIN_SESSION", "CAPTAIN_PROJECT")

os.makedirs(HOME)
for name in INHERITED:
    os.environ.pop(name, None)
# USERPROFILE is what Path.home() reads on Windows, HOME everywhere else.
os.environ.update(HOME=HOME, USERPROFILE=HOME, CAPTAIN_MEMORY_ROOT=MEMORY_ROOT)
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
