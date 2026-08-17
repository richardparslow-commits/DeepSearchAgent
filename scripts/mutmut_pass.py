"""Run a scoped mutmut pass over one module and dump surviving-mutant diffs.

Usage: python scripts/mutmut_pass.py <module> <test_file> [extra_test_file ...]

Rewrites [tool.mutmut] in pyproject.toml, generates coverage for the module,
runs `mutmut run --max-children 1` (sequential children avoid a macOS
fork/CoreFoundation crash in mutmut), and writes the diff of every surviving
mutant to /tmp/mutmut_survivors_<module>.txt for triage.

The `mutants/` scratch dir must keep a full copy of the package (mutmut only
copies the module under mutation, so siblings are needed for imports) while
replacing only the current module's copy and dropping the cached
mutant->test associations, so tests added since the last pass are picked up.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _reverify_segfault(name: str, tests: list[str], target: str) -> bool:
    """Recover the true verdict for a mutant mutmut reported as ``segfault``.

    ``segfault`` (SIGSEGV) means mutmut's worker process crashed while running
    the mutant's tests, which tells us nothing about whether those tests would
    have passed or failed. On macOS the sequential worker intermittently
    crashes regardless of the mutation, so a segfault masks an equivalent
    survivor and a cleanly killable mutant alike. Apply the mutation to the
    real source, run the test slice directly (outside the worker), restore the
    file, and return True only when the tests still pass.
    """
    original = Path(target).read_bytes()
    try:
        applied = subprocess.run(
            [sys.executable, "-m", "mutmut", "apply", name],
            capture_output=True,
        )
        if applied.returncode != 0:
            # The mutation could not be reproduced for re-verification. Keep it
            # flagged as untriaged rather than silently dropping an unknown
            # outcome.
            return True
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *tests],
                capture_output=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            # The direct run hangs, so the mutant is genuinely untriaged.
            return True
        return proc.returncode == 0
    finally:
        Path(target).write_bytes(original)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    module = sys.argv[1]
    tests = [
        t if t.startswith("tests/") else f"tests/{t}" for t in sys.argv[2:]
    ]
    target = f"va_legal_agent/{module}"
    dotted = target.replace("/", ".").removesuffix(".py")

    with open("pyproject.toml") as f:
        text = f.read()
    text = re.sub(
        r"source_paths = \[[^\]]*\]",
        f'source_paths = ["{target}"]',
        text,
    )
    selection = ", ".join(f'"{t}"' for t in tests)
    text = re.sub(
        r"pytest_add_cli_args_test_selection = \[[^\]]*\]",
        f"pytest_add_cli_args_test_selection = [{selection}]",
        text,
    )
    with open("pyproject.toml", "w") as f:
        f.write(text)

    # Keep a full package copy in the scratch dir so sibling imports resolve.
    scratch_pkg = Path("mutants") / "va_legal_agent"
    scratch_pkg.mkdir(parents=True, exist_ok=True)
    for source in Path("va_legal_agent").glob("*.py"):
        shutil.copy2(source, scratch_pkg / source.name)

    # The scratch dir lives INSIDE the project, so dotenv's find_dotenv walks
    # up from mutants/va_legal_agent to the real .env, and mutmut's copy step
    # skips files already present — so a sandbox created before conftest.py
    # existed never gets it. Without the hermetic-env fixture, the sandbox
    # inherits the user's real settings (RPM budgets, provider list) and the
    # baseline test run sleeps/throttles for minutes and fails. Copy it
    # explicitly every pass.
    conftest = Path("conftest.py")
    if conftest.exists():
        shutil.copy2(conftest, Path("mutants") / "conftest.py")

    # Drop the current module's copy and the cached test associations so this
    # pass re-collects stats against the tests as they exist right now.
    stem = Path(module).stem
    for suffix in (".py", ".py.meta", ".py.spans"):
        (scratch_pkg / f"{stem}{suffix}").unlink(missing_ok=True)
    (Path("mutants") / "mutmut-stats.json").unlink(missing_ok=True)

    subprocess.run(["rm", "-f", ".coverage"])
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--cov=" + dotted, *tests],
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "1"],
        capture_output=True,
    )

    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"], capture_output=True, text=True
    )
    exit_codes: dict[str, int] = {}
    meta_path = scratch_pkg / f"{stem}.py.meta"
    if meta_path.exists():
        exit_codes = json.loads(meta_path.read_text()).get("exit_code_by_key", {})

    # Classify each mutant by status. ``survived`` and ``timeout`` are plainly
    # untriaged (a hung mutant is just as untriaged as a passing one), and
    # ``suspicious`` (any abnormal exit) is untriaged too, with one exclusion:
    # on macOS mutmut's worker fork intermittently aborts with a CoreFoundation
    # SIGABRT (exit -6) that has nothing to do with the mutant, so skip that
    # exact signature locally. The nightly CI gate runs on Linux, where no such
    # crash exists and every suspicious mutant still fails the gate. A
    # ``segfault`` (SIGSEGV) is re-verified by running the mutant's tests
    # directly, because the macOS worker crash masks the true killed/survived
    # verdict (see :func:`_reverify_segfault`).
    survivors: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        name, status = (part.strip() for part in line.split(":", 1))
        if status == "survived":
            survivors.append(name)
        elif status == "timeout":
            survivors.append(name)
        elif status == "segfault":
            if _reverify_segfault(name, tests, target):
                survivors.append(name)
        elif status == "suspicious":
            if sys.platform == "darwin" and exit_codes.get(name) == -6:
                continue
            survivors.append(name)
    with open(f"/tmp/mutmut_survivors_{module}.txt", "w") as f:
        f.write(f"### {module}: {len(survivors)} survivors\n\n")
        for name in survivors:
            show = subprocess.run(
                [sys.executable, "-m", "mutmut", "show", name],
                capture_output=True,
                text=True,
            )
            f.write(show.stdout or show.stderr)
            f.write("\n" + "=" * 60 + "\n\n")
    print(f"{module}: {len(survivors)} survivors -> /tmp/mutmut_survivors_{module}.txt")


if __name__ == "__main__":
    main()
