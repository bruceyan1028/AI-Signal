import unittest

from src import data_scope


class DataScopeTest(unittest.TestCase):
    def test_public_scope_has_no_owner(self):
        self.assertEqual(data_scope.normalize_scope(), ("public", None))

    def test_personal_scope_requires_an_owner(self):
        self.assertEqual(
            data_scope.normalize_scope("personal", "user-123"),
            ("personal", "user-123"),
        )
        with self.assertRaises(data_scope.ScopeError):
            data_scope.normalize_scope("personal")

    def test_public_scope_rejects_an_owner(self):
        with self.assertRaises(data_scope.ScopeError):
            data_scope.normalize_scope("public", "user-123")

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(data_scope.ScopeError):
            data_scope.normalize_scope("team", "user-123")


if __name__ == "__main__":
    unittest.main()
