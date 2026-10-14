import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOTPATH = ROOT / "src" / "dataprice" / "hotpath"
BANNED = (
    "psycopg",
    "clickhouse_connect",
    "arcticdb",
    "dataprice.ledger",
    "dataprice.market",
    "dataprice.factors",
    "dataprice.session",
    "dataprice.bootstrap",
    "dataprice.cli",
    "dataprice.smoke",
    "dataprice.health",
)


def test_hotpath_does_not_import_databases():
    offenders: list[str] = []
    for path in HOTPATH.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith(BANNED):
                    offenders.append(f"{path.name}: {module}")
    assert offenders == []
