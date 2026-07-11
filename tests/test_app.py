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
        self.assertEqual(app.get_dueum_variants("련"), ["련", "연"])
        self.assertEqual(app.get_dueum_variants("량"), ["량", "양"])
        self.assertEqual(app.get_dueum_variants("락"), ["락", "낙"])
        self.assertEqual(app.get_dueum_variants("각"), ["각"])
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

    def test_search_rejects_combined_dictionary(self):
        response = app.app.test_client().get("/api/search?query=기&dictionary=both&mode=all")
        self.assertEqual(response.status_code, 400)
        self.assertIn("표준국어대사전 또는 우리말샘", response.json["error"])

    def test_search_uses_fast_continuation_checks(self):
        candidate = app.normalize_item(SAMPLE["channel"]["item"][0], "stdict")
        with patch.object(app, "paged_search", return_value=([candidate], 1, [])), \
             patch.object(app, "continuation_count", return_value=(1, [])) as count:
            response = app.app.test_client().get("/api/search?query=기&dictionary=stdict&mode=all")
        self.assertEqual(response.status_code, 200)
        count.assert_not_called()

    def test_one_shot_sort_uses_broader_candidate_pool(self):
        safe = app.normalize_item({"word": "가나", "sense": {"pos": "명사"}}, "stdict")
        shot = app.normalize_item({"word": "가슘", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable == "슘" else (3, [])
        with patch.object(app, "merged_search", return_value=([safe, shot], 2, [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=가&dictionary=stdict&mode=all&sort=one-shot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["가슘", "가나"])

    def test_one_shot_mode_uses_broader_fast_search(self):
        shot = app.normalize_item({"word": "가슘", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search") as paged, \
             patch.object(app, "merged_search", return_value=([shot], 100, [])) as merged, \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=가&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["words"][0]["word"], "가슘")
        paged.assert_not_called()
        merged.assert_called_once()

    def test_one_shot_mode_uses_direct_rare_final_candidates(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "merged_search", return_value=([], 2911, [])), \
             patch.object(app, "rare_final_candidates", return_value=([shot], [])) as rare:
            response = app.app.test_client().get("/api/search?query=리&dictionary=opendict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["리튬"])
        rare.assert_called_once()

    def test_one_shot_total_includes_direct_rare_candidates(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "merged_search", return_value=([], 0, [])), \
             patch.object(app, "rare_final_candidates", return_value=([shot], [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 1)
        self.assertEqual(response.json["one_shot_count"], 1)

    def test_rare_final_candidates_filters_to_matching_rare_endings(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "opendict")
        linoleum = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        safe = app.normalize_item({"word": "리튬이온", "sense": {"pos": "명사"}}, "opendict")
        def fake_fetch(_d, query, _s, _c, _f):
            if query == "리튬":
                return [shot, safe], 2
            if query == "리놀륨":
                return [linoleum], 1
            return [], 0
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "리", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["리튬", "리놀륨"])

    def test_fast_analysis_checks_dueum_variant_for_rare_final(self):
        candidate = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        follow = app.normalize_item({"word": "윰라대왕", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 0), ([follow], 2)]) as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["is_one_shot"])
        self.assertEqual(analysed[0]["next_word_count"], 2)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["륨", "윰"])

    def test_fast_analysis_does_not_apply_dueum_when_disabled(self):
        candidate = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary") as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), False, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        fetch.assert_not_called()

    def test_merged_search_continues_after_filtered_empty_batch(self):
        later = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 2911), ([later], 2911)]) as fetch:
            words, total, warnings = app.merged_search(["stdict"], "리", app.Filters(), limit=1)
        self.assertEqual(total, 2911)
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["리튬"])
        self.assertEqual([call.args[2] for call in fetch.call_args_list], [1, 101])

    def test_merged_search_stops_on_api_start_limit(self):
        with patch.object(app, "fetch_dictionary", side_effect=[([], 2911), app.ApiError("Invalid start value")]) as fetch:
            words, total, warnings = app.merged_search(["opendict"], "리", app.Filters(), limit=1)
        self.assertEqual(words, [])
        self.assertEqual(total, 2911)
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_count, 2)

    def test_continuation_is_not_one_shot_when_filtered_match_exists(self):
        match = app.normalize_item({"word": "가가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", return_value=([match], 4043)) as fetch:
            count, warnings = app.continuation_count(["stdict"], "가", app.Filters(), False)
        self.assertEqual(count, 4043)
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_args.args[3], app.API_PAGE_SIZE)

    def test_continuation_checks_dueum_variant(self):
        match = app.normalize_item({"word": "연가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 0), ([match], 12)]) as fetch:
            count, warnings = app.continuation_count(["stdict"], "련", app.Filters(), True)
        self.assertEqual(count, 12)
        self.assertEqual(warnings, [])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["련", "연"])

    def test_fast_continuation_stops_after_first_match(self):
        match = app.normalize_item({"word": "리가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", return_value=([match], 100)) as fetch:
            count, warnings = app.continuation_count(["stdict", "opendict"], "리", app.Filters(), True, exact=False)
        self.assertEqual(count, 100)
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[3], app.FAST_CONTINUATION_PAGE_SIZE)

    def test_one_shot_page_prioritizes_rare_finals(self):
        common = [app.normalize_item({"word": f"리가{i}", "sense": {"pos": "명사"}}, "stdict") for i in range(40)]
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable == "튬" else (10, [])
        with patch.object(app, "continuation_count", side_effect=count_for_syllable):
            _analysed, visible, _has_more, warnings = app.one_shot_page(["stdict"], common + [lithium], app.Filters(), True, 1)
        self.assertEqual(warnings, [])
        self.assertEqual(visible[0]["word"], "리튬")

    def test_fast_analysis_marks_rare_final_without_api_calls(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        common = app.normalize_item({"word": "리본", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "continuation_count") as count:
            analysed, warnings = app.analyse_words(["stdict"], [lithium, common], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        self.assertFalse(analysed[1]["is_one_shot"])
        count.assert_not_called()


if __name__ == "__main__":
    unittest.main()
