"""Run the dependency-free JavaScript regression cases as part of the Python suite."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend regression cases")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("frontend.test.cjs"))],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
