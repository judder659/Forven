"""The backend caps numeric library thread pools (2026-10-30 scanner slowdown)."""

import os
import subprocess
import sys


def test_importing_forven_caps_blas_threads_unless_set():
    code = "import os, forven; print(os.environ['OPENBLAS_NUM_THREADS'], os.environ['OMP_NUM_THREADS'])"
    env = {k: v for k, v in os.environ.items() if not k.endswith("_NUM_THREADS")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["1", "1"]

    env["OPENBLAS_NUM_THREADS"] = "4"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["4", "1"]  # an operator's own value wins
