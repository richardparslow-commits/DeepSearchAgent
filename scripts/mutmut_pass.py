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
    # Count "suspicious" mutants (timeout / abnormal exit) as survivors too:
    # they are just as untriaged as a plain survivor - e.g. a mutant that slips
    # past a guard into a real network call can show up as suspicious on one
    # platform and survived on another (see the llm.py `or` -> `and` mutant).
    # One exclusion: on macOS, mutmut's worker fork intermittently aborts with
    # a CoreFoundation SIGABRT (exit -6, reported as "suspicious") that has
    # nothing to do with the mutant; skip that exact signature so the local
    # gate is not flaky. The nightly CI gate runs on Linux, where no such
    # crash exists, so every suspicious mutant there still fails the gate.
    exit_codes: dict[str, int] = {}
    meta_path = scratch_pkg / f"{stem}.py.meta"
    if meta_path.exists():
        exit_codes = json.loads(meta_path.read_text()).get("exit_code_by_key", {})
    survivors = []
    for line in result.stdout.splitlines():
        if "survived" in line:
            survivors.append(line.strip().split(":")[0])
        elif "suspicious" in line:
            name = line.strip().split(":")[0]
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
