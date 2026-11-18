import subprocess
import sys

code = subprocess.run(
    [sys.executable, "-c", "import httpx; print(httpx.__file__)"],
    capture_output=True,
    text=True,
)
print(code.stdout, code.stderr)
