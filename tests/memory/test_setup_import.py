import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is ineffective as root")
def test_import_mem0_with_uncreatable_config_dir(tmp_path):
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o555)
    env = {**os.environ, "MEM0_DIR": str(parent / "child"), "MEM0_TELEMETRY": "false"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import mem0"], env=env, capture_output=True, text=True, timeout=120
        )
    finally:
        parent.chmod(0o755)
    assert proc.returncode == 0, proc.stderr
