"""核对交换资料的客户编号、时序和关联条件。"""
import json
from pathlib import Path
import unittest


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def rows(name):
    content = (SAMPLES / f"{name}.ndjson").read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines()]


class ExchangeSampleTest(unittest.TestCase):
    def test_records_are_json_objects(self):
        files = sorted(SAMPLES.glob("*.ndjson"))
        self.assertEqual(len(files), 5)
        for path in files:
            with self.subTest(path=path.name):
                records = rows(path.stem)
                self.assertTrue(records)
                self.assertTrue(all(isinstance(row, dict) for row in records))

    def test_related_records_preserve_source_evidence(self):
        a, b, c = rows("shifts")
        self.assertEqual(b["rider"], c["rider"])
        self.assertNotEqual(b["station"], c["station"])
        attendance = rows("attendance")
        self.assertLess(attendance[2]["earliest"], a["until"])
        self.assertGreater(attendance[2]["latest"], a["until"])
        moves = rows("handoffs")
        self.assertEqual(moves[1]["move"], moves[3]["move"])
        self.assertEqual(set(moves[1]["proof"] + moves[3]["proof"]), {"r-a", "r-b"})
        self.assertNotEqual(moves[4]["from"], moves[5]["from"])
