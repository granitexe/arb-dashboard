"""Static guardrails: the package must never pull in trading/signing code.

Uses AST (not substring scan) so guardrail docstrings don't false-positive.
"""
import ast
import os

import basis_arb

PKG_DIR = os.path.dirname(basis_arb.__file__)
FORBIDDEN_MODULE_PREFIXES = ("hyperliquid.exchange", "eth_account", "hyperliquid.utils.signing")
# The only Hyperliquid symbols the whole package is allowed to import.
ALLOWED_HL_IMPORTS = {
    ("hyperliquid.info", "Info"),
    ("hyperliquid.utils.constants", "MAINNET_API_URL"),
}


def _py_files():
    for root, _dirs, files in os.walk(PKG_DIR):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _imports(path):
    """Yield (module, name) pairs for every import in a file."""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                yield mod, alias.name


def test_no_forbidden_imports_anywhere():
    """Guardrail: no module in the package (except execution/) may import signing/trading libs.

    The execution/ sub-package is the INTENTIONALLY isolated layer that holds all
    Exchange/signing imports. All other modules must remain read-only.
    """
    offenders = []
    for path in _py_files():
        # execution/ is the intentional isolation boundary for trading/signing code.
        # All other modules must stay clean.
        if "/execution/" in path:
            continue
        for mod, name in _imports(path):
            if any(mod == p or mod.startswith(p + ".") or mod == p for p in FORBIDDEN_MODULE_PREFIXES):
                offenders.append((path, mod, name))
            if mod == "hyperliquid" and name == "Exchange":
                offenders.append((path, mod, name))
    assert offenders == [], f"forbidden trading/signing imports found: {offenders}"


def test_only_whitelisted_hyperliquid_imports():
    """Only execution/ and sources/ may import hyperliquid libs, and only via allowlist."""
    found = set()
    for path in _py_files():
        # execution/ may import Exchange (that's its job). sources/ may import Info.
        if "/execution/" in path:
            continue
        for mod, name in _imports(path):
            if mod.startswith("hyperliquid") and name is not None:
                found.add((mod, name))
    assert found.issubset(ALLOWED_HL_IMPORTS), f"unexpected hyperliquid imports: {found - ALLOWED_HL_IMPORTS}"


def test_hyperliquid_client_uses_info_only():
    src = open(os.path.join(PKG_DIR, "sources", "hyperliquid_info.py"), encoding="utf-8").read()
    assert "from hyperliquid.info import Info" in src
    assert "skip_ws=True" in src
    # No Exchange/signing usage.
    assert "Exchange(" not in src
    assert "eth_account" not in [m for m, _ in _imports(os.path.join(PKG_DIR, "sources", "hyperliquid_info.py"))]


def test_no_private_key_identifiers():
    """Guardrail: no module in the package (except execution/) may reference credential names.

    The execution/ sub-package is the only place that handles secret keys,
    so it is excluded from this check.
    """
    bad = ("secret_key", "private_key", "mnemonic")
    for path in _py_files():
        # execution/ is the only place allowed to handle secret keys
        if "/execution/" in path:
            continue
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        kwargs = {kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords if kw.arg}
        ident = names | attrs | kwargs
        assert not (ident & set(bad)), f"{path} references credential identifier {ident & set(bad)}"
