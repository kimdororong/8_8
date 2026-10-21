import json
import unittest

import server


class ParseReviewTest(unittest.TestCase):
    def test_verdict_is_recomputed_from_severity(self):
        text = "결과:\n" + json.dumps({
            "verdict": "approved", "summary": "s", "strengths": ["a"],
            "issues": [{"severity": "blocker", "problem": "p"}]})
        self.assertEqual(server.parse_review(text)["verdict"], "changes_requested")

    def test_major_is_conditional_and_none_is_approved(self):
        major = json.dumps({"issues": [{"severity": "major"}]})
        self.assertEqual(server.parse_review(major)["verdict"], "conditional")
        self.assertEqual(server.parse_review("{}")["verdict"], "approved")

    def test_unknown_severity_becomes_minor(self):
        r = server.parse_review(json.dumps({"issues": [{"severity": "weird"}]}))
        self.assertEqual(r["issues"][0]["severity"], "minor")

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            server.parse_review("no json here")

    def test_data_url(self):
        self.assertEqual(server.split_data_url("data:image/png;base64,AAAA"), ("image/png", "AAAA"))
        with self.assertRaises(ValueError):
            server.split_data_url("data:text/html;base64,AAAA")


if __name__ == "__main__":
    unittest.main()
