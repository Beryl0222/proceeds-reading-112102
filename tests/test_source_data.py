"""检查交换资料的标识、文本和跨表引用。"""

import json
from pathlib import Path
import unittest


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class SourceDataTest(unittest.TestCase):
    def setUp(self):
        self.tables = {}
        for path in sorted(DATA_DIR.glob("*.json")):
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rows, list)
            self.assertTrue(rows, path.name)
            by_id = {}
            for row in rows:
                identifier = row["id"]
                self.assertIsInstance(identifier, str, path.name)
                self.assertNotIn(identifier, by_id, path.name)
                if "text" in row:
                    self.assertIsInstance(row["text"], str, path.name)
                by_id[identifier] = row
            self.tables[path.stem] = by_id

    def test_exchange_tables_present(self):
        self.assertEqual(set(self.tables), {'claims', 'ledger', 'proofs', 'terms'})

    def test_related_source_records(self):
        data = self.tables
        claims = data["claims"]
        ledger = data["ledger"]
        for entry in ledger.values():
            self.assertIn(entry["business_ref"], claims)
        self.assertEqual(claims["C3"]["invoice_id"], claims["C4"]["invoice_id"])
        returned = ledger["L4"]
        original = ledger[returned["reverses"]]
        self.assertEqual(returned["business_ref"], original["business_ref"])
        self.assertLess(returned["amount"], original["amount"])
        self.assertLess(original["date"], data["terms"]["T4"]["from"])
        self.assertLess(data["terms"]["T4"]["from"], data["proofs"]["P3"]["received"])
