import unittest
from pathlib import Path


class MySQLPersonalSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path("src/mysql_backend.py").read_text(encoding="utf-8")

    def test_records_are_scoped_and_legacy_api_defaults_to_public(self):
        self.assertIn("scope_type ENUM('public','personal')", self.source)
        self.assertIn("owner_user_id VARCHAR(128) NOT NULL DEFAULT ''", self.source)
        self.assertIn("UNIQUE KEY uq_table_record_scope", self.source)
        self.assertIn("def create_scoped_record(", self.source)
        self.assertIn("scope_type: str = data_scope.PUBLIC_SCOPE", self.source)
        self.assertIn("def read_scoped_records(", self.source)
        self.assertIn("AND scope_type='public' AND owner_user_id=''", self.source)

    def test_personal_product_tables_are_present(self):
        for table in ("users", "user_preferences", "subscriptions", "user_signal_state", "media_assets"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", self.source)
        self.assertIn("def upsert_user_signal_state(", self.source)
        self.assertIn("def create_subscription(", self.source)
        self.assertIn("def register_media_asset(", self.source)

    def test_media_is_referenced_not_stored_as_a_blob(self):
        self.assertIn("storage_url TEXT NOT NULL", self.source)
        self.assertNotIn("media_blob", self.source)


if __name__ == "__main__":
    unittest.main()
