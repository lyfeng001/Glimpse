import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_syntax_highlighter():
    r = subprocess.run(["node", os.path.join(HERE, "hl_test.js")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("ok ")
