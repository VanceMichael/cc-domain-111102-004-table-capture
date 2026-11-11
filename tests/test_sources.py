import unittest
import json
from app.wire import Replay

from app.markup import tokens
class DomainTests(unittest.TestCase):
    def test_position(self):
        self.assertEqual(tokens("<table>\n<tr>")[1].line,2)
    def test_entity(self):
        self.assertEqual(tokens("<td>A&amp;B</td>")[1].text,"A&B")
    def test_attributes(self):
        self.assertIn(("rowspan","2"),tokens('<th rowspan="2">x</th>')[0].attrs)


class WireTests(unittest.TestCase):
    def test_unknown_route(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"routes.json";p.write_text("{}")
            self.assertEqual(Replay(p).get("https://missing.invalid").status,404)
