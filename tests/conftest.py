import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from bruce_lora.simdevice import PyDevice  # noqa: E402
from cppdevice import HARNESS, CppDevice  # noqa: E402


def _harness_ok() -> bool:
    if HARNESS.exists():
        return True
    build = HARNESS.parent / "build.sh"
    if shutil.which("clang++") and build.exists():
        return subprocess.run([str(build)], capture_output=True).returncode == 0
    return False


@pytest.fixture(params=["py", "cpp"])
def make_device(request):
    """Factory for the Core2 side: the Python model and the firmware's real C++ engine."""
    made = []

    def factory():
        if request.param == "cpp":
            if not _harness_ok():
                pytest.skip("C++ harness unavailable (needs clang++ and the firmware repo)")
            d = CppDevice()
            made.append(d)
            return d
        return PyDevice()

    yield factory
    for d in made:
        d.close()
