"""Локальная проверка: AST всех модулей + запуск тестов без pytest."""
import ast
import os
import sys
import types
from pathlib import Path

ROOT = Path(r"C:\Users\User\Desktop\DeepSeek")
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: False))
try:
    import pytest  # noqa
except ImportError:
    stub = types.ModuleType("pytest")

    class _R:
        def __init__(s, e):
            s.e = e

        def __enter__(s):
            return s

        def __exit__(s, et, ev, tb):
            return et is not None and issubclass(et, s.e)

    stub.fixture = lambda *a, **k: (lambda f: f)
    stub.raises = _R
    sys.modules["pytest"] = stub

ok, n = True, 0
for dp, dn, fn in os.walk(ROOT):
    dn[:] = [d for d in dn if d not in ("__pycache__", ".venv", ".git", "tests")]
    for f in fn:
        if f.endswith(".py"):
            n += 1
            p = Path(dp) / f
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                ok = False
                print(f"SYNTAX ERROR {p}: {e}")
print(f"AST OK: {n} files" if ok else "AST FAILED")

sys.path.insert(0, str(ROOT / "tests"))
import test_formatting, test_parser  # noqa: E402

fails, total = 0, 0
for mod in (test_formatting, test_parser):
    for name in sorted(dir(mod)):
        if name.startswith("test_"):
            total += 1
            try:
                getattr(mod, name)()
            except Exception as e:
                fails += 1
                print(f"FAIL {mod.__name__}.{name}: {type(e).__name__}: {e}")
print(f"TESTS: {total - fails}/{total} passed")
sys.exit(0 if (ok and fails == 0) else 1)