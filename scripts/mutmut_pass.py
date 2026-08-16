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
    survivors = [
        line.strip().split(":")[0]
        for line in result.stdout.splitlines()
        if "survived" in line
    ]
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
