"""The page on the agents' RPC interface matches the agents (scripts/agent_interface.py)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_interface.py"


@pytest.mark.skipif(not Path(sys.executable).with_name("ast-grep").exists()
                    and shutil.which("ast-grep") is None, reason="needs ast-grep (ast-grep-cli)")
def test_agent_interface_page_is_up_to_date():
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
