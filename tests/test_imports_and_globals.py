import os
import sys
import importlib
import inspect
import dis
import builtins
import pytest

def test_all_modules_and_bytecode_globals():
    builtins_set = set(dir(builtins))
    errors = []

    for root, dirs, files in os.walk("src"):
        for file in files:
            if file.endswith(".py") and not file.startswith("__pycache__"):
                mod_path = os.path.relpath(os.path.join(root, file), ".").replace("/", ".").replace("\\", ".").replace(".py", "")
                try:
                    mod = importlib.import_module(mod_path)
                except Exception as e:
                    errors.append(f"IMPORT ERROR {mod_path}: {e}")
                    continue

                for name, obj in inspect.getmembers(mod):
                    if inspect.isfunction(obj) or inspect.iscoroutinefunction(obj):
                        fn_mod = getattr(obj, "__module__", "")
                        if fn_mod == mod_path:
                            fn_globals = getattr(obj, "__globals__", vars(mod))
                            for instr in dis.get_instructions(obj):
                                if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
                                    var_name = instr.argval
                                    if var_name not in fn_globals and var_name not in builtins_set:
                                        errors.append(f"MISSING GLOBAL in {mod_path}:{obj.__qualname__} -> {var_name}")
                    elif inspect.isclass(obj) and getattr(obj, "__module__", "") == mod_path:
                        for m_name, m_obj in obj.__dict__.items():
                            if inspect.isfunction(m_obj) or inspect.iscoroutinefunction(m_obj):
                                fn_globals = getattr(m_obj, "__globals__", vars(mod))
                                for instr in dis.get_instructions(m_obj):
                                    if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
                                        var_name = instr.argval
                                        if var_name not in fn_globals and var_name not in builtins_set:
                                            errors.append(f"MISSING GLOBAL in {mod_path}:{obj.__name__}.{m_name} -> {var_name}")

    assert not errors, f"Unresolved global variables detected:\n" + "\n".join(errors)
