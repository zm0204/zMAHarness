from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mah.adapters import registry
from mah.catalog import (
    REPO_CATALOG_PATH,
    CatalogError,
    effective_efforts,
    load_catalog,
    load_repo_catalog,
)


class RepoCatalogValidationTests(unittest.TestCase):
    def test_repo_catalog_loads_and_passes_all_validations(self):
        catalog = load_repo_catalog()
        self.assertEqual(catalog.schema_version, 1)
        self.assertEqual(catalog.updated, "2026-11-19")
        self.assertFalse(catalog.local_overrides)
        self.assertEqual(catalog.routing.tight_percent, 90)
        self.assertEqual(catalog.routing.detect_ttl_hours, 12)
        self.assertEqual(catalog.routing.quota_ttl_minutes, 10)
        self.assertEqual(catalog.routing.agy_unknown_cooldown_minutes, 60)
        self.assertEqual(catalog.routing.agy_cooldown_horizon_days, 8)
        models_by_key = {m.key: m for m in catalog.models}
        for cell in catalog.cells:
            for pick in cell.picks:
                model = models_by_key[pick.model]
                self.assertEqual(model.status, "recommended")
                self.assertIn(pick.effort, model.efforts)
                for alt in pick.option3:
                    alt_model = models_by_key[alt.model]
                    self.assertEqual(alt_model.status, "recommended")
                    self.assertIn(alt.effort, alt_model.efforts)
        for key in ("save", "upgrade"):
            for pick in catalog.option3[key]:
                model = models_by_key[pick.model]
                self.assertEqual(model.status, "recommended")
                self.assertIn(pick.effort, model.efforts)

    def test_repo_catalog_refused_prefixes_match_registry_and_non_refused_are_allowed(self):
        catalog = load_repo_catalog()
        catalog_refused = {}
        for model in catalog.models:
            if model.status == "refused":
                for route in model.routes:
                    self.assertIsNotNone(route.prefix)
                    self.assertIsNone(route.id_template)
                    catalog_refused.setdefault(route.adapter, []).append(route.prefix)
            else:
                for route in model.routes:
                    self.assertIsNotNone(route.id_template)
                    self.assertIsNone(route.prefix)
                    for effort in model.efforts:
                        expanded = route.expand_id(effort)
                        self.assertIsNone(
                            registry.refused_model_reason(route.adapter, expanded),
                            (model.key, route.adapter, expanded),
                        )
        registry_refused = {adapter: list(prefixes) for adapter, (prefixes, _reason) in registry.REFUSED_MODELS.items()}
        self.assertEqual(catalog_refused, registry_refused)

    def test_repo_catalog_option3_save_follows_appendix_c_d7(self):
        catalog = load_repo_catalog()
        save_picks = [(pick.model, pick.effort) for pick in catalog.option3["save"]]
        self.assertEqual(
            save_picks,
            [("gemini-3.8-flash", "low"), ("claude-sonnet", "low"), ("gpt-6-luna", "low")],
        )
        self.assertEqual(catalog.model_by_key("gpt-6-luna").status, "recommended")

    def test_find_cell_exact_and_fallback_order(self):
        catalog = load_repo_catalog()
        self.assertEqual(catalog.find_cell("review", "standard").level, "standard")
        # second_opinion only defines deep -> light/standard fall back upward to deep.
        self.assertEqual(catalog.find_cell("second_opinion", "standard").level, "deep")
        self.assertEqual(catalog.find_cell("second_opinion", "light").level, "deep")
        # research defines light and standard -> deep falls back downward to standard.
        self.assertEqual(catalog.find_cell("research", "deep").level, "standard")
        self.assertIsNone(catalog.find_cell("unknown", "light"))


class CatalogSchemaAndLocalOverrideTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-catalog-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_unsupported_schema_version_returns_none_with_warning(self):
        bad_repo = self.write("bad_repo.toml", 'schema_version = 99\nupdated = "2026-11-19"\n')
        catalog, warnings = load_catalog(repo_path=bad_repo)
        self.assertIsNone(catalog)
        self.assertEqual(len(warnings), 1)
        self.assertIn("99", warnings[0])

    def test_validation_rejects_malformed_repo_catalogs(self):
        base = """
schema_version = 1
updated = "2026-11-19"
"""
        cases = {
            "duplicate_key": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "m1-{effort}", quota = "agy:gemini" }]
[[models]]
key = "m1"
display = "M1 dup"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "m1b-{effort}", quota = "agy:gemini" }]
""",
            "duplicate_alias_case_insensitive": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
aliases = ["Flash"]
routes = [{ adapter = "agy-print", id = "m1-{effort}", quota = "agy:gemini" }]
[[models]]
key = "m2"
display = "M2"
family = "gemini"
status = "selectable"
billing = "subscription"
efforts = ["low"]
aliases = ["flash"]
routes = [{ adapter = "agy-print", id = "m2-{effort}", quota = "agy:gemini" }]
""",
            "unregistered_adapter": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "no-such-adapter", id = "m1", quota = "x" }]
""",
            "invalid_brace_placeholder": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "m1-{other}", quota = "agy:gemini" }]
""",
            "refused_with_id": base + """
[[models]]
key = "bad"
display = "Bad"
family = "gemini"
status = "refused"
billing = "subscription"
routes = [{ adapter = "agy-print", id = "bad-low", quota = "agy:gemini" }]
""",
            "non_refused_with_prefix": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "selectable"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", prefix = "m1-", quota = "agy:gemini" }]
""",
            "registry_refused_listed_as_selectable": base + """
[[models]]
key = "orch"
display = "Orch"
family = "gemini"
status = "selectable"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "gemini-pro-orchestrator-{effort}", quota = "agy:gemini" }]
""",
            "cell_references_selectable_model": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "selectable"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "m1-{effort}", quota = "agy:gemini" }]
[[cells]]
task = "review"
level = "light"
picks = [{ model = "m1", effort = "low" }]
""",
            "cell_references_unsupported_effort": base + """
[[models]]
key = "m1"
display = "M1"
family = "gemini"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["low"]
routes = [{ adapter = "agy-print", id = "m1-{effort}", quota = "agy:gemini" }]
[[cells]]
task = "review"
level = "light"
picks = [{ model = "m1", effort = "high" }]
""",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                path = self.write(f"{name}.toml", text)
                with self.assertRaises(CatalogError):
                    load_repo_catalog(path)

    def test_local_override_adds_replaces_and_merges_cells_routing_option3(self):
        local = self.write("models.local.toml", """
schema_version = 1

[routing]
tight_percent = 85
detect_ttl_hours = 6

[[models]]
key = "gpt-oss-120b"
display = "GPT-OSS 120B"
family = "gpt-oss"
status = "recommended"
cost = "economy"
billing = "subscription"
efforts = ["medium"]
routes = [{ adapter = "agy-print", id = "gpt-oss-120b-{effort}", quota = "agy:gpt-oss" }]

[[models]]
key = "gemini-4-argon"
display = "Gemini 4 Argon Custom"
family = "gemini"
status = "recommended"
cost = "premium"
billing = "subscription"
efforts = ["medium", "high"]
aliases = ["argon"]
alias_effort = "medium"
routes = [{ adapter = "agy-print", id = "gemini-4-argon-{effort}", quota = "agy:gemini" }]

[[cells]]
task = "research"
level = "light"
picks = [
  { model = "gemini-3.8-flash", effort = "low" },
  { model = "gpt-oss-120b", effort = "medium" },
]

[option3]
upgrade = [{ model = "gemini-4-argon", effort = "high" }]
""")
        catalog, warnings = load_catalog(local_path=local, repo_path=REPO_CATALOG_PATH)
        self.assertIsNotNone(catalog)
        self.assertTrue(catalog.local_overrides)
        self.assertEqual(warnings, [])
        self.assertEqual(catalog.routing.tight_percent, 85)
        self.assertEqual(catalog.routing.detect_ttl_hours, 6)
        self.assertEqual(catalog.routing.quota_ttl_minutes, 10)  # preserved from repo
        self.assertIsNotNone(catalog.model_by_key("gpt-oss-120b"))
        argon = catalog.model_by_key("gemini-4-argon")
        self.assertEqual(argon.display, "Gemini 4 Argon Custom")
        self.assertEqual(argon.aliases, ("argon",))  # whole-entry replace drops 4argon
        self.assertEqual(argon.efforts, ("medium", "high"))
        cell = catalog.find_cell("research", "light")
        self.assertEqual([(p.model, p.effort) for p in cell.picks],
                         [("gemini-3.8-flash", "low"), ("gpt-oss-120b", "medium")])
        self.assertEqual([(p.model, p.effort) for p in catalog.option3["upgrade"]],
                         [("gemini-4-argon", "high")])
        # Unmodified option3.save kept intact.
        self.assertEqual(len(catalog.option3["save"]), 3)

    def test_local_override_cannot_replace_repo_refused_model_but_can_refuse_existing(self):
        local = self.write("models.local.toml", """
schema_version = 1

[[models]]
key = "gemini-pro-orchestrator"
display = "Trying to unrefuse"
family = "gemini"
status = "selectable"
billing = "subscription"
efforts = ["high"]
routes = [{ adapter = "agy-print", id = "gemini-4-neon-{effort}", quota = "agy:gemini" }]

[[models]]
key = "gemini-4-neon"
display = "Refused locally"
family = "gemini"
status = "refused"
billing = "subscription"
routes = [{ adapter = "agy-print", prefix = "gemini-4-neon-", quota = "agy:gemini" }]
""")
        catalog, warnings = load_catalog(local_path=local, repo_path=REPO_CATALOG_PATH)
        self.assertIsNotNone(catalog)
        self.assertTrue(catalog.local_overrides)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gemini-pro-orchestrator", warnings[0])
        self.assertEqual(catalog.model_by_key("gemini-pro-orchestrator").status, "refused")
        self.assertEqual(catalog.model_by_key("gemini-4-neon").status, "refused")
        # Repo cells that referenced gemini-4-neon have that pick cleanly pruned.
        review_std = catalog.find_cell("review", "standard")
        self.assertNotIn("gemini-4-neon", [p.model for p in review_std.picks])

    def test_local_override_syntax_or_schema_error_is_ignored_with_warning(self):
        for name, text in (
            ("syntax.toml", "schema_version = 1\n[routing\n"),
            ("bad_version.toml", "schema_version = 2\n"),
            ("bad_routing.toml", "schema_version = 1\n[routing]\ntight_percent = 150\n"),
            ("alias_collision.toml", """
schema_version = 1
[[models]]
key = "custom-model"
display = "Custom"
family = "custom"
status = "selectable"
billing = "local"
efforts = ["low"]
aliases = ["FLASH"]
routes = [{ adapter = "agy-print", id = "custom-{effort}", quota = "agy:custom" }]
"""),
        ):
            with self.subTest(name=name):
                local = self.write(name, text)
                catalog, warnings = load_catalog(local_path=local, repo_path=REPO_CATALOG_PATH)
                self.assertIsNotNone(catalog)
                self.assertFalse(catalog.local_overrides)
                self.assertEqual(len(warnings), 1)
                self.assertEqual(catalog.routing.tight_percent, 90)

    def test_local_override_pick_referencing_missing_model_skips_only_that_pick(self):
        local = self.write("models.local.toml", """
schema_version = 1

[[cells]]
task = "research"
level = "light"
picks = [
  { model = "does-not-exist", effort = "low" },
  { model = "gemini-3.8-flash", effort = "low" },
]
""")
        catalog, warnings = load_catalog(local_path=local, repo_path=REPO_CATALOG_PATH)
        self.assertIsNotNone(catalog)
        self.assertTrue(catalog.local_overrides)
        self.assertEqual(len(warnings), 1)
        self.assertIn("does-not-exist", warnings[0])
        cell = catalog.find_cell("research", "light")
        self.assertEqual([(p.model, p.effort) for p in cell.picks], [("gemini-3.8-flash", "low")])

    def test_effective_efforts_intersects_model_adapter_detected_and_max_effort(self):
        catalog = load_repo_catalog()
        terra = catalog.model_by_key("gpt-6.1-terra")
        route = terra.routes[0]
        self.assertEqual(
            effective_efforts(terra, route,
                              detected_efforts=("low", "medium", "high", "xhigh", "max", "ultra")),
            ("low", "medium", "high", "xhigh"),
        )
        self.assertEqual(
            effective_efforts(terra, route,
                              detected_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
                              max_effort="high"),
            ("low", "medium", "high"),
        )

    def test_openai_api_route_is_refused_in_4a_1(self):
        path = self.write("openai_api_route.toml", """
schema_version = 1
updated = "2026-11-01"

[[models]]
key = "local-qwen"
display = "Local Qwen"
family = "qwen"
status = "selectable"
billing = "local"
efforts = ["medium"]
routes = [{ adapter = "openai-api", id = "lmstudio/qwen", quota = "local:lmstudio" }]
""")
        with self.assertRaisesRegex(CatalogError, "openai-api 的目錄路由在 4a-2 才支援"):
            load_repo_catalog(path)
        local_path = self.write("openai_api_local.toml", """
schema_version = 1

[[models]]
key = "local-qwen"
display = "Local Qwen"
family = "qwen"
status = "selectable"
billing = "local"
efforts = ["medium"]
routes = [{ adapter = "openai-api", id = "lmstudio/qwen", quota = "local:lmstudio" }]
""")
        catalog, warnings = load_catalog(local_path=local_path, repo_path=REPO_CATALOG_PATH)
        self.assertIsNotNone(catalog)
        self.assertFalse(catalog.local_overrides)
        self.assertEqual(len(warnings), 1)
        self.assertIn("openai-api 的目錄路由在 4a-2 才支援", warnings[0])


if __name__ == "__main__":
    unittest.main()
