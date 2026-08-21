"""Apply a mutmut mutant to disk with class-aware method resolution.

``python -m mutmut apply <name>`` locates the function to replace with
``find_top_level_function_or_method(module, name)``, which returns the FIRST
method with that name anywhere in the module and ignores the class in the
``xǁClassNameǁmethod__mutmut_N`` key. A module with several same-named methods
(``providers.py`` defines five ``search`` methods) therefore gets the mutation
applied to the *wrong* method — e.g. ``SearchProvider.search`` instead of
``BvaLocalIndexProvider.search`` — which corrupts the re-verification verdict.

This helper re-implements mutmut's ``apply_mutant`` with the class scoped, so
the mutation lands on the exact method mutmut mutated. It is invoked by
``scripts/mutmut_pass.py`` when re-verifying a ``segfault`` mutant, replacing
the buggy ``mutmut apply`` subprocess.

Usage: python scripts/_apply_mutant.py <mutant_name>
"""

import sys

import libcst as cst

from mutmut.__main__ import (
    Config,
    find_mutant,
    orig_function_and_class_names_from_key,
    read_functions_from_index,
    read_mutant_function,
    read_mutants_module,
    read_orig_module,
)


def _find_method_in_class(
    module: cst.Module, class_name: str, method_name: str
) -> cst.FunctionDef | None:
    """Return the ``method_name`` method inside ``class_name``, or ``None``.

    Unlike mutmut's ``find_top_level_function_or_method``, this scopes the
    search to the named class so a module with several same-named methods
    resolves to the right one.
    """
    for child in module.body:
        if not isinstance(child, cst.ClassDef) or child.name.value != class_name:
            continue
        if not isinstance(child.body, cst.IndentedBlock):
            return None
        for method in child.body.body:
            if isinstance(method, cst.FunctionDef) and method.name.value == method_name:
                return method
    return None


def _find_top_level_function(
    module: cst.Module, function_name: str
) -> cst.FunctionDef | None:
    """Return the top-level ``function_name``, or ``None``."""
    return next(
        (
            child
            for child in module.body
            if isinstance(child, cst.FunctionDef)
            and child.name.value == function_name
        ),
        None,
    )


def apply_mutant_precisely(mutant_name: str) -> None:
    """Apply *mutant_name* to disk, scoping method lookups to its class.

    Mirrors mutmut's ``apply_mutant`` but resolves the target method within the
    class encoded in the mutant key instead of grabbing the first same-named
    method in the module.
    """
    path = find_mutant(mutant_name).path
    orig_function_name, class_name = orig_function_and_class_names_from_key(
        mutant_name
    )
    orig_function_name = orig_function_name.rpartition(".")[-1]

    orig_module = read_orig_module(path)
    if class_name is not None:
        original_function = _find_method_in_class(
            orig_module, class_name, orig_function_name
        )
    else:
        original_function = _find_top_level_function(
            orig_module, orig_function_name
        )
    if original_function is None:
        raise FileNotFoundError(f"Could not apply mutant {mutant_name}")

    functions_from_index = read_functions_from_index(mutant_name, path)
    if functions_from_index is not None:
        _, mutant_function = functions_from_index
    else:
        mutant_function = read_mutant_function(read_mutants_module(path), mutant_name)
    mutant_function = mutant_function.with_changes(name=cst.Name(orig_function_name))

    new_module = orig_module.deep_replace(original_function, mutant_function)
    with open(path, "w") as fh:
        fh.write(new_module.code)


def main() -> None:
    Config.ensure_loaded()
    apply_mutant_precisely(sys.argv[1])


if __name__ == "__main__":
    main()
