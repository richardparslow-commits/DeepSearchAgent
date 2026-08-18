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


def _reverify_segfault(name: str, tests: list[str], target: str) -> str:
    """Recover the true verdict for a mutant mutmut reported as ``segfault``.

    ``segfault`` (SIGSEGV) means mutmut's worker process crashed while running
    the mutant's tests, which tells us nothing about whether those tests would
    have passed or failed. On macOS the sequential worker intermittently
    crashes regardless of the mutation, so a segfault masks an equivalent
    survivor and a cleanly killable mutant alike. Apply the mutation to the
    real source, run the test slice directly (outside the worker), restore the
    file, and return ``killed`` when the tests fail, ``survived`` when they
    still pass, or ``timeout`` when the direct run hangs -- a hang is a real
    defect and must surface as a timeout so the gate cannot absorb it.
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
            return "survived"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *tests],
                capture_output=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            # The direct run hangs, so the mutant is genuinely untriaged -- and
            # the hang itself is a test-suite defect worth failing the gate on.
            return "timeout"
        return "killed" if proc.returncode != 0 else "survived"
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
    #
    # Timeouts are recorded in the survivor header separately from genuine
    # survivors: a hang is never a triaged equivalent, so the baseline gate
    # fails on any timeout even when a module sits below its count baseline.
    survivors: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        name, status = (part.strip() for part in line.split(":", 1))
        if status == "survived":
            survivors.append((name, status))
        elif status == "timeout":
            survivors.append((name, status))
        elif status == "segfault":
            verdict = _reverify_segfault(name, tests, target)
            if verdict != "killed":
                survivors.append((name, verdict))
        elif status == "suspicious":
            if sys.platform == "darwin" and exit_codes.get(name) == -6:
                continue
            survivors.append((name, status))
    timeout_count = sum(1 for _, status in survivors if status == "timeout")
    suffix = f" ({timeout_count} timeout)" if timeout_count else ""
    with open(f"/tmp/mutmut_survivors_{module}.txt", "w") as f:
        f.write(f"### {module}: {len(survivors)} survivors{suffix}\n\n")
        for name, _status in survivors:
            show = subprocess.run(
                [sys.executable, "-m", "mutmut", "show", name],
                capture_output=True,
                text=True,
            )
            f.write(show.stdout or show.stderr)
            f.write("\n" + "=" * 60 + "\n\n")
    print(
        f"{module}: {len(survivors)} survivors"
        + (f" ({timeout_count} timeout)" if timeout_count else "")
        + f" -> /tmp/mutmut_survivors_{module}.txt"
    )
    # Echo the survivor names, their mutmut verdict, and how many tests are
    # associated with their function to stdout so a CI failure is actionable
    # without shelling into the runner: /tmp files do not survive the job, but
    # these lines land in the workflow log.
    associated: dict[str, int] = {}
    stats_path = Path("mutants") / "mutmut-stats.json"
    if stats_path.exists():
        stats_data = json.loads(stats_path.read_text())
        tests_by_fn = stats_data.get("tests_by_mangled_function_name", {})
        associated = {fn: len(tests) for fn, tests in tests_by_fn.items()}
    for name, status in survivors:
        func = name.rsplit("__mutmut_", 1)[0] if "__mutmut_" in name else name
        print(f"  - {name} [{status}, {associated.get(func, 0)} associated tests]")


if __name__ == "__main__":
    main()
