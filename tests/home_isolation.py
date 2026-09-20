"""Point HOME at an empty temp directory for the whole suite.

config reads its middle settings layer from Path.home()/.captain/settings.toml, so a
developer's own uncommented setting there (a custom [placement] shape, say) would decide
what the tests see and turn the gate red on a deliberate user choice. Importing this
module once isolates every test in the process, since imports all happen before any test
runs and nothing reads HOME at import time.

It lives here rather than in tests/__init__.py because the gate runs
`unittest discover -s tests`, which makes tests/ the top-level directory and never
imports the package. tests/test_captain.py imports it; HomeIsolationTests there fails if
that import is ever dropped.
"""

import atexit
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="captain-tests-home-")
# USERPROFILE is what Path.home() reads on Windows, HOME everywhere else.
os.environ.update(HOME=HOME, USERPROFILE=HOME)
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
