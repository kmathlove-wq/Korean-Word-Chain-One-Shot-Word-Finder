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
        self.assertEqual(app.get_dueum_variants("릎"), ["릎"])
        self.assertEqual(app.get_dueum_variants("각"), ["각"])
        self.assertEqual(app.last_hangul_syllable("기쁨(1)-"), "쁨")

    def test_validation(self):
        self.assertEqual(app.validate_query(" 기 "), "기")
        with self.assertRaises(ValueError):
            app.validate_query("abc")

    def test_unknown_part_of_speech_can_pass_noun_filter(self):
        word = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "품사 미상"}}, "opendict")
        filters = app.Filters(noun_only=True, include_technical=True)
        self.assertTrue(app.allowed(word, filters))

    def test_no_part_of_speech_can_pass_noun_filter(self):
        word = app.normalize_item({"word": "인산^나트륨", "pos": "품사 없음", "definition": "인산 나트륨."}, "stdict")
        filters = app.Filters(noun_only=True, include_technical=True)
        self.assertTrue(app.allowed(word, filters))

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
        self.assertIn("no-store", response.headers["Cache-Control"])

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
        count.assert_called_once()

    def test_response_merges_duplicate_headwords_even_when_metadata_differs(self):
        duplicate_a = app.normalize_item({"word": "인듐", "sense": {"pos": "명사", "definition": "은백색의 무른 금속 원소."}}, "stdict")
        duplicate_b = app.normalize_item({"word": "인듐", "sense": {"pos": "품사 없음", "definition": "은백색의 무른 금속 원소. "}}, "stdict")
        with patch.object(app, "rare_final_candidates", return_value=([duplicate_a, duplicate_b], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "starting_total", return_value=(2, [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=인&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["인듐"])

    def test_one_shot_mode_combines_prefix_and_other_rare_final_families(self):
        knee = app.normalize_item({"word": "무릎", "sense": {"pos": "명사", "definition": "넓적다리와 정강이 사이."}}, "stdict")
        sodium = app.normalize_item({"word": "무수탄산나트륨", "sense": {"pos": "품사 없음", "definition": "탄산 나트륨 무수물."}}, "stdict")

        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable in {"릎", "륨"} else (10, [])

        with patch.object(app, "rare_final_candidates", return_value=([knee], [])) as rare, \
             patch.object(app, "prefix_expansion_candidates", return_value=([sodium], [])), \
             patch.object(app, "starting_total", return_value=(3449, [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=무&dictionary=stdict&mode=one-shot&sort=alphabet&dueum=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["무릎", "무수탄산나트륨"])
        rare.assert_called_once()

    def test_next_sort_uses_fast_continuation_counts_for_all_syllables(self):
        many = app.normalize_item({"word": "장가", "sense": {"pos": "명사"}}, "stdict")
        few = app.normalize_item({"word": "장튬", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable == "튬" else (30, [])
        with patch.object(app, "paged_search", return_value=([many, few], 2, [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable) as count:
            response = app.app.test_client().get("/api/search?query=장&dictionary=stdict&mode=all&sort=next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["장튬", "장가"])
        self.assertTrue(count.call_count >= 2)
        self.assertTrue(all(not call.args[4] for call in count.call_args_list))

    def test_one_shot_sort_uses_broader_candidate_pool(self):
        safe = app.normalize_item({"word": "가나", "sense": {"pos": "명사"}}, "stdict")
        shot = app.normalize_item({"word": "가슘", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable == "슘" else (3, [])
        with patch.object(app, "paged_search", return_value=([safe, shot], 2, [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=가&dictionary=stdict&mode=all&sort=one-shot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["가슘", "가나"])

    def test_one_shot_sort_expands_hidden_rare_prefixes(self):
        seed = app.normalize_item({"word": "인듐", "sense": {"pos": "명사"}}, "stdict")
        sodium = app.normalize_item({"word": "인산나트륨", "pos": "품사 없음", "definition": "인산 나트륨."}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True):
            return (0, []) if syllable in {"듐", "륨"} else (5, [])
        with patch.object(app, "paged_search", return_value=([], 2340, [])), \
             patch.object(app, "rare_final_candidates", return_value=([seed], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([sodium], [])) as expand, \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=인&dictionary=stdict&mode=all&sort=one-shot&dueum=false")
        self.assertEqual(response.status_code, 200)
        self.assertIn("인산나트륨", [word["word"] for word in response.json["words"]])
        expand.assert_called_once()

    def test_one_shot_mode_uses_broader_fast_search(self):
        shot = app.normalize_item({"word": "가슘", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search") as paged, \
             patch.object(app, "merged_search") as merged, \
             patch.object(app, "one_shot_scan_candidates", return_value=([shot], 100, False, [])) as scan, \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=가&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["words"][0]["word"], "가슘")
        paged.assert_not_called()
        merged.assert_not_called()
        scan.assert_called_once()

    def test_one_shot_mode_uses_direct_rare_final_candidates(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "one_shot_scan_candidates", return_value=([], 2911, True, [])), \
             patch.object(app, "rare_final_candidates", return_value=([shot], [])) as rare, \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=opendict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["리튬"])
        rare.assert_called_once()

    def test_one_shot_total_includes_direct_rare_candidates(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "one_shot_scan_candidates", return_value=([], 0, False, [])), \
             patch.object(app, "rare_final_candidates", return_value=([shot], [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 1)
        self.assertEqual(response.json["one_shot_count"], 1)

    def test_one_shot_mode_can_continue_after_empty_scan_window(self):
        with patch.object(app, "one_shot_scan_candidates", return_value=([], 12651, True, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])):
            response = app.app.test_client().get("/api/search?query=수&dictionary=opendict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["words"], [])
        self.assertTrue(response.json["has_more"])

    def test_one_shot_mode_scans_requested_page_window(self):
        shot = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "품사 미상"}}, "opendict")
        with patch.object(app, "one_shot_scan_candidates", return_value=([shot], 12651, True, [])) as scan, \
             patch.object(app, "rare_final_candidates") as rare, \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=수&dictionary=opendict&mode=one-shot&sort=alphabet&page=2&noun_only=true&include_technical=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["words"][0]["word"], "수산화나트륨")
        self.assertTrue(response.json["has_more"])
        self.assertEqual(scan.call_args.args[3], 2)
        rare.assert_not_called()

    def test_one_shot_mode_expands_rare_candidate_prefixes(self):
        seed = app.normalize_item({"word": "수산화카드뮴", "sense": {"pos": "품사 미상"}}, "opendict")
        sodium = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "품사 미상"}}, "opendict")
        with patch.object(app, "one_shot_scan_candidates", return_value=([seed], 12651, True, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([sodium], [])) as expand, \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=수&dictionary=opendict&mode=one-shot&sort=alphabet&noun_only=true&include_technical=true&dueum=false")
        self.assertEqual(response.status_code, 200)
        self.assertIn("수산화나트륨", [word["word"] for word in response.json["words"]])
        expand.assert_called_once()

    def test_prefix_expansion_finds_hidden_same_family_rare_word(self):
        seed = app.normalize_item({"word": "수산화카드뮴", "sense": {"pos": "품사 미상"}}, "opendict")
        sodium = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "품사 미상"}}, "opendict")
        def fake_fetch(_dictionary, prefix, _start, _count, _filters, method="start", **_kwargs):
            if prefix == "수산화" and method == "start":
                return [sodium], 1
            return [], 0
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.prefix_expansion_candidates(["opendict"], "수", [seed], app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["수산화나트륨"])

    def test_rare_final_candidates_filters_to_matching_rare_endings(self):
        shot = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "opendict")
        linoleum = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        safe = app.normalize_item({"word": "리튬이온", "sense": {"pos": "명사"}}, "opendict")
        def fake_fetch(_d, query, _s, _c, _f, method="start"):
            if query == "튬" and method == "end":
                return [shot, safe], 2
            if query == "륨" and method == "end":
                return [linoleum], 1
            return [], 0
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "리", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(word["word"] for word in words), ["리놀륨", "리튬"])

    def test_rare_final_candidates_finds_middle_syllable_words(self):
        magnesium = app.normalize_item({"word": "수산마그네슘", "sense": {"pos": "명사"}}, "opendict")
        other = app.normalize_item({"word": "마그네슘", "sense": {"pos": "명사"}}, "opendict")
        def fake_fetch(_d, query, _s, _c, _f, method="start"):
            return ([magnesium, other], 2) if query == "슘" and method == "end" else ([], 0)
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "수", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["수산마그네슘"])

    def test_rare_final_candidates_scans_deeper_end_pages(self):
        sodium = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "명사"}}, "opendict")
        def fake_fetch(_d, query, start, _c, _f, method="start"):
            return ([sodium], 450) if query == "륨" and method == "end" and start == 2 else ([], 450)
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "수", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["수산화나트륨"])

    def test_fast_analysis_checks_dueum_variant_for_rare_final(self):
        candidate = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        follow = app.normalize_item({"word": "윰라대왕", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 0), ([follow], 2)]) as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["is_one_shot"])
        self.assertEqual(analysed[0]["next_word_count"], 2)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["륨", "윰"])

    def test_dueum_does_not_change_knee_final_to_swamp(self):
        candidate = app.normalize_item({"word": "무릎", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", return_value=([], 0)) as fetch:
            analysed, warnings = app.analyse_words(["stdict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["릎"])

    def test_fast_analysis_does_not_apply_dueum_when_disabled(self):
        candidate = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary", return_value=([], 0)) as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), False, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["륨"])

    def test_fast_analysis_rejects_rare_final_when_follow_word_exists(self):
        candidate = app.normalize_item({"word": "수산마그네슘", "sense": {"pos": "명사"}}, "opendict")
        follow = app.normalize_item({"word": "슘페터", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary", return_value=([follow], 1)) as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["is_one_shot"])
        self.assertEqual(analysed[0]["next_word_count"], 1)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["슘"])

    def test_merged_search_continues_after_filtered_empty_batch(self):
        later = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 2911), ([later], 2911)]) as fetch:
            words, total, warnings = app.merged_search(["stdict"], "리", app.Filters(), limit=1)
        self.assertEqual(total, 2911)
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["리튬"])
        self.assertEqual([call.args[2] for call in fetch.call_args_list], [1, 2])

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

    def test_fast_analysis_verifies_rare_final_before_marking_one_shot(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        common = app.normalize_item({"word": "리본", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "continuation_count", return_value=(0, [])) as count:
            analysed, warnings = app.analyse_words(["stdict"], [lithium, common], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        self.assertFalse(analysed[1]["is_one_shot"])
        count.assert_called_once()


if __name__ == "__main__":
    unittest.main()
