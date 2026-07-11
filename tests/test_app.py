import json
import unittest
from unittest.mock import patch

import app


SAMPLE = {
    "channel": {
        "total": 1,
        "item": [{
            "word": "기쁨",
            "target_code": "123",
            "sense": {"pos": "명사", "definition": "흐뭇하고 흡족한 마음."},
        }],
    }
}


class HelperTests(unittest.TestCase):
    def test_dueum_and_last_syllable(self):
        self.assertEqual(app.get_dueum_variants("녀"), ["녀", "여"])
        self.assertEqual(app.last_hangul_syllable("기쁨(1)-"), "쁨")

    def test_validation(self):
        self.assertEqual(app.validate_query(" 기 "), "기")
        with self.assertRaises(ValueError):
            app.validate_query("abc")

    def test_json_parser(self):
        words, total = app.parse_json(SAMPLE, "stdict")
        self.assertEqual(total, 1)
        self.assertEqual(words[0]["word"], "기쁨")
        self.assertEqual(
            words[0]["detail_url"],
            "https://stdict.korean.go.kr/search/searchResult.do?searchKeyword=%EA%B8%B0%EC%81%A8&pageSize=10",
        )

    def test_health(self):
        response = app.app.test_client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_search_response(self):
        candidate = app.normalize_item(SAMPLE["channel"]["item"][0], "stdict")
        with patch.object(app, "paged_search", return_value=([candidate], 1, [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=기&dictionary=stdict&mode=all")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["words"][0]["is_one_shot"])

    def test_continuation_is_not_one_shot_when_filtered_match_exists(self):
        match = app.normalize_item({"word": "가가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", return_value=([match], 4043)) as fetch:
            count, warnings = app.continuation_count(["stdict"], "가", app.Filters(), False)
        self.assertEqual(count, 4043)
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_args.args[3], app.API_PAGE_SIZE)


if __name__ == "__main__":
    unittest.main()
