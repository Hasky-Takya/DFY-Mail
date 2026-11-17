import json
import unittest

from graph_schema import DEFAULTS, graph_template, normalize_settings


class GraphSchemaTests(unittest.TestCase):
    def test_defaults_and_independence(self):
        self.assertEqual(normalize_settings(), DEFAULTS)
        self.assertFalse(normalize_settings()["enabled"])
        config = normalize_settings()
        config["batch_size"] = 99
        self.assertEqual(normalize_settings()["batch_size"], 8)

    def test_only_section_null_means_default(self):
        self.assertEqual(normalize_settings(None), DEFAULTS)
        for key in DEFAULTS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_settings({key: None})
        self.assertEqual(normalize_settings({"enabled": True})["enabled"], True)

    def test_sections_unknown_keys_and_types(self):
        for value in ([], False, 1, "{}", {"secret": 1}, {"enabled": 1}, {"enabled": "false"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_settings(value)
        for key in DEFAULTS.keys() - {"enabled", "assistant_name", "assistant_aliases", "user_name", "user_aliases"}:
            for value in (True, False, 0, -1, 1.0, "2", [], 10**9):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    normalize_settings({key: value})

    def test_identity_settings_strict_and_independent(self):
        aliases = ["Other"]
        config = normalize_settings({"assistant_name": "Name", "assistant_aliases": aliases})
        aliases.append("mutation")
        self.assertEqual(config["assistant_aliases"], ["Other"])
        config["user_aliases"].append("mutation")
        self.assertEqual(normalize_settings()["user_aliases"], ["用户"])
        for value in (None, "Alias", [""], [1], ["a", " A "]):
            with self.assertRaises(ValueError):
                normalize_settings({"assistant_aliases": value})
        for value in (None, [], "", "bad\nname"):
            with self.assertRaises(ValueError):
                normalize_settings({"user_name": value})

    def test_template_has_exact_candidate_root_and_controlled_predicates(self):
        text = graph_template({"max_updates": 4, "max_text_chars": 32})
        candidate = json.loads(text.split("Candidate example/schema:\n", 1)[1])
        self.assertEqual(set(candidate), {"facts", "entities", "relations", "states", "event_relations"})
        self.assertIn("At most 4 items TOTAL", text)
        self.assertIn("friend_of", text)
        self.assertIn("caused_by", text)
        self.assertIn("located_at", text)
        self.assertIsNone(candidate["entities"][0]["target_id"])


if __name__ == "__main__":
    unittest.main()
