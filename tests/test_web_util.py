import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = sorted((Path(__file__).parent / "js").glob("*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", JS_TESTS, ids=[p.stem for p in JS_TESTS])
def test_web_js_unit_tests(path):
    result = subprocess.run(["node", "--test", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
