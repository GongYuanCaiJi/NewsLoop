from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CALLERS = (
    ROOT / "backend" / "app.py",
    ROOT / "auto_grader_daemon.py",
    ROOT / "streamlit" / "hybrid_dashboard.py",
)


class SchemaCallerArchitectureTests(unittest.TestCase):
    def test_callers_cannot_reimplement_schema_compatibility(self):
        forbidden_names = {
            "DEFAULT_COLUMN_MAPPING",
            "FIELD_SPECS",
            "normalize_column_mapping",
            "check_notion_schema",
            "auto_fill_missing_notion_columns",
            "_select_creatable_type",
            "_build_property_create_schema",
        }

        for path in CALLERS:
            with self.subTest(path=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                used_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                defined_functions = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertTrue(
                    forbidden_names.isdisjoint(used_names | defined_functions),
                    f"schema compatibility must stay behind SchemaManager: {forbidden_names & (used_names | defined_functions)}",
                )

                imported_manager = any(
                    isinstance(node, ast.ImportFrom)
                    and str(node.module or "").endswith("schema_manager")
                    and any(alias.name == "SchemaManager" for alias in node.names)
                    for node in ast.walk(tree)
                )
                self.assertTrue(imported_manager, "caller must cross the SchemaManager interface")

    def test_capture_intake_uses_schema_check_result_instead_of_formatting_errors(self):
        source = (ROOT / "backend" / "capture_intake.py").read_text(encoding="utf-8")

        self.assertNotIn('detail=f"SchemaError:', source)
        self.assertIn("schema_check = self._schema.check()", source)

    def test_dashboard_manager_factory_loads_shared_override_cache(self):
        source = (ROOT / "streamlit" / "hybrid_dashboard.py").read_text(encoding="utf-8")

        self.assertEqual(source.count("SchemaManager("), 1)
        self.assertIn("manager.load_cache()", source)

    def test_runtime_config_swap_does_not_publish_partial_state(self):
        from backend import app

        original = {
            name: getattr(app, name)
            for name in (
                "LOCAL_CFG_PATH",
                "SCHEMA_TTL_SECONDS",
                "NOTION_TOKEN",
                "DB_ID",
                "NOTION_HEADERS",
                "schema_manager",
            )
        }
        old_manager = object()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "dashboard_config.json"
                config_path.write_text(
                    '{"NOTION_TOKEN": "new-token", "DB_ID": "new-db"}',
                    encoding="utf-8",
                )
                app.LOCAL_CFG_PATH = str(config_path)
                app.SCHEMA_TTL_SECONDS = "not-an-int"
                app.NOTION_TOKEN = "old-token"
                app.DB_ID = "old-db"
                app.NOTION_HEADERS = {"Authorization": "Bearer old-token"}
                app.schema_manager = old_manager

                with self.assertRaises(ValueError):
                    app._get_runtime_state()

                self.assertEqual(app.NOTION_TOKEN, "old-token")
                self.assertEqual(app.DB_ID, "old-db")
                self.assertEqual(app.NOTION_HEADERS, {"Authorization": "Bearer old-token"})
                self.assertIs(app.schema_manager, old_manager)
        finally:
            for name, value in original.items():
                setattr(app, name, value)


if __name__ == "__main__":
    unittest.main()
