import json
import time
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
    def setUp(self):
        # 테스트 간 캐시 오염(gather_one_shot_page 진행 상황, fetch_dictionary)을 막는다.
        app.cache._items.clear()

    def test_dueum_and_last_syllable(self):
        self.assertEqual(app.get_dueum_variants("녀"), ["녀", "여"])
        self.assertEqual(app.get_dueum_variants("련"), ["련", "연"])
        self.assertEqual(app.get_dueum_variants("량"), ["량", "양"])
        self.assertEqual(app.get_dueum_variants("락"), ["락", "낙"])
        self.assertEqual(app.get_dueum_variants("랑"), ["랑", "낭"])
        self.assertEqual(app.get_dueum_variants("렌"), ["렌", "넨"])
        self.assertEqual(app.get_dueum_variants("린"), ["린", "인"])
        self.assertEqual(app.get_dueum_variants("륨"), ["륨", "윰"])
        self.assertEqual(app.get_dueum_variants("릎"), ["릎", "늪"])
        self.assertEqual(app.get_dueum_variants("른"), ["른", "는"])
        self.assertEqual(app.convert_dueum_word("른개"), "는개")
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

    def test_word_chain_filters_reject_single_noun_and_verb(self):
        filters = app.Filters(noun_only=True, include_technical=True, include_single=False)
        single_noun = app.normalize_item({"word": "슛", "sense": {"pos": "명사"}}, "stdict")
        verb = app.normalize_item({"word": "슛하다", "sense": {"pos": "동사"}}, "stdict")
        self.assertFalse(app.allowed(single_noun, filters))
        self.assertFalse(app.allowed(verb, filters))

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

    def test_dueum_guide_shows_converted_word(self):
        response = app.app.test_client().get("/두음법칙 보기?word=른개")
        self.assertEqual(response.status_code, 200)
        self.assertIn("른개 → 는개", response.get_data(as_text=True))

    def test_old_dueum_url_redirects_to_korean_address(self):
        response = app.app.test_client().get("/dueum")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.headers["Location"].endswith("/%EB%91%90%EC%9D%8C%EB%B2%95%EC%B9%99%20%EB%B3%B4%EA%B8%B0"))

    def test_starting_search_merges_dueum_variant_results(self):
        converted = app.normalize_item({"word": "는개", "sense": {"pos": "명사"}}, "stdict")

        def fake_paged(_dictionaries, search_query, _filters, _page):
            return ([converted], 92, []) if search_query == "는" else ([], 0, [])

        with patch.object(app, "paged_search", side_effect=fake_paged) as paged, \
             patch.object(app, "continuation_count", return_value=(5, [])):
            response = app.app.test_client().get("/api/search?query=른&dictionary=stdict&mode=words&sort=alphabet&dueum=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["는개"])
        self.assertEqual(response.json["words"][0]["next_word_count"], 5)
        self.assertEqual([call.args[1] for call in paged.call_args_list], ["른", "는"])

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

    def test_next_sort_uses_fast_continuation_counts_for_all_syllables(self):
        many = app.normalize_item({"word": "장가", "sense": {"pos": "명사"}}, "stdict")
        few = app.normalize_item({"word": "장튬", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True, _slow=False):
            return (0, []) if syllable == "튬" else (30, [])
        with patch.object(app, "paged_search", return_value=([many, few], 2, [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable) as count:
            response = app.app.test_client().get("/api/search?query=장&dictionary=stdict&mode=all&sort=next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["장튬", "장가"])
        self.assertTrue(count.call_count >= 2)
        self.assertTrue(all(not call.args[4] for call in count.call_args_list))

    def test_next_sort_has_more_uses_dictionary_total(self):
        candidates = [
            app.normalize_item({"word": f"리가{index}", "sense": {"pos": "명사"}}, "stdict")
            for index in range(24)
        ]
        with patch.object(app, "paged_search", return_value=(candidates, 495, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", return_value=(10, [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=all&sort=next&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["words"]), 24)
        self.assertTrue(response.json["has_more"])

    def test_next_sort_second_page_returns_page_candidates_without_offsetting_twice(self):
        candidates = [
            app.normalize_item({"word": f"리나{index}", "sense": {"pos": "명사"}}, "stdict")
            for index in range(24)
        ]
        with patch.object(app, "paged_search", return_value=(candidates, 495, [])), \
             patch.object(app, "rare_final_candidates") as rare, \
             patch.object(app, "prefix_expansion_candidates") as expand, \
             patch.object(app, "continuation_count", return_value=(8, [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=all&sort=next&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["words"]), 24)
        self.assertTrue(response.json["has_more"])
        rare.assert_not_called()
        expand.assert_not_called()

    def test_one_shot_sort_uses_broader_candidate_pool(self):
        safe = app.normalize_item({"word": "가나", "sense": {"pos": "명사"}}, "stdict")
        shot = app.normalize_item({"word": "가슘", "sense": {"pos": "명사"}}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True, _slow=False):
            return (0, []) if syllable == "슘" else (3, [])
        with patch.object(app, "paged_search", return_value=([safe, shot], 2, [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=가&dictionary=stdict&mode=all&sort=one-shot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["가슘", "가나"])
        self.assertEqual(response.json["words"][1]["next_word_count"], 3)

    def test_one_shot_sort_checks_common_endings_instead_of_using_placeholder_one(self):
        common = app.normalize_item({"word": "표가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search", return_value=([common], 1, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", return_value=(4043, [])) as count:
            response = app.app.test_client().get("/api/search?query=표&dictionary=stdict&mode=all&sort=one-shot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["words"][0]["next_word_count"], 4043)
        self.assertFalse(response.json["words"][0]["is_one_shot"])
        count.assert_called_once()

    def test_unchecked_common_ending_is_not_reported_as_one(self):
        common = app.normalize_item({"word": "표가", "sense": {"pos": "명사"}}, "stdict")
        analysed, warnings = app.analyse_words(
            ["stdict"], [common], app.Filters(), True, exact_counts=False, fast_all_counts=False,
        )
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["count_available"])
        self.assertNotEqual(analysed[0]["next_word_count"], 1)

    def test_unexpected_analysis_failure_keeps_json_response_contract(self):
        candidate = app.normalize_item({"word": "표가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search", return_value=([candidate], 1, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "analyse_words", side_effect=RuntimeError("internal")):
            response = app.app.test_client().get("/api/search?query=표&dictionary=stdict&mode=all&sort=one-shot")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertIn("일시적인 오류", response.json["error"])

    def test_one_shot_sort_expands_hidden_rare_prefixes(self):
        seed = app.normalize_item({"word": "인듐", "sense": {"pos": "명사"}}, "stdict")
        sodium = app.normalize_item({"word": "인산나트륨", "pos": "품사 없음", "definition": "인산 나트륨."}, "stdict")
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True, _slow=False):
            return (0, []) if syllable in {"듐", "륨"} else (5, [])
        with patch.object(app, "paged_search", return_value=([], 2340, [])), \
             patch.object(app, "rare_final_candidates", return_value=([seed], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([sodium], [])) as expand, \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get("/api/search?query=인&dictionary=stdict&mode=all&sort=one-shot&dueum=false")
        self.assertEqual(response.status_code, 200)
        self.assertIn("인산나트륨", [word["word"] for word in response.json["words"]])
        expand.assert_called_once()

    def test_prefix_expansion_skips_generic_probes_without_rare_seed(self):
        common = app.normalize_item({"word": "표가", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary") as fetch:
            words, warnings = app.prefix_expansion_candidates(["opendict"], "표", [common], app.Filters())
        self.assertEqual(words, [])
        self.assertEqual(warnings, [])
        fetch.assert_not_called()

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
        def fake_fetch(_d, query, _s, _c, _f, method="start", **_kwargs):
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
        def fake_fetch(_d, query, _s, _c, _f, method="start", **_kwargs):
            return ([magnesium, other], 2) if query == "슘" and method == "end" else ([], 0)
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "수", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["수산마그네슘"])

    def test_rare_final_candidates_finds_rebound_shot(self):
        rebound = app.normalize_item({"word": "리바운드^슛", "sense": {"pos": "품사 없음"}}, "stdict")
        def fake_fetch(_d, query, _s, _c, _f, method="start", **_kwargs):
            return ([rebound], 1) if query == "슛" and method == "end" else ([], 0)
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["stdict"], "리", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["리바운드슛"])

    def test_rare_final_candidates_scans_deeper_end_pages(self):
        sodium = app.normalize_item({"word": "수산화나트륨", "sense": {"pos": "명사"}}, "opendict")
        def fake_fetch(_d, query, start, _c, _f, method="start", **_kwargs):
            return ([sodium], 450) if query == "륨" and method == "end" and start == 2 else ([], 450)
        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, warnings = app.rare_final_candidates(["opendict"], "수", app.Filters())
        self.assertEqual(warnings, [])
        self.assertEqual([word["word"] for word in words], ["수산화나트륨"])

    # --- 조각 B 회귀 테스트: 모든 넓은 탐색에 시간 제한 적용 ---
    def test_rare_final_candidates_skips_deep_jobs_past_deadline(self):
        # 얕은 탐색에서 못 찾았고 마감도 이미 지났으면, 오래 걸리는 깊은
        # 탐색은 아예 시작하지 않는다(2026-09-15, 10초 이내 응답 목표).
        with patch.object(app, "fetch_dictionary", return_value=([], 0)) as fetch:
            words, warnings = app.rare_final_candidates(
                ["stdict"], "가", app.Filters(), deadline=time.monotonic() - 1,
            )
        self.assertEqual(words, [])
        called_methods_starts = {(call.args[2], call.kwargs.get("request_timeout")) for call in fetch.call_args_list}
        # 얕은 탐색(20개 남짓)만 불렸고, 깊은 탐색(수십 개 더)은 불리지 않았다.
        self.assertLessEqual(len(fetch.call_args_list), len(app.RARE_FINAL_PRIORITY) + 2)

    def test_prefix_expansion_candidates_marks_time_short_past_deadline(self):
        seed = app.normalize_item({"word": "수산화카드뮴", "sense": {"pos": "품사 미상"}}, "opendict")

        def slow_fetch(*_args, **_kwargs):
            time.sleep(0.05)
            return [], 0

        with patch.object(app, "fetch_dictionary", side_effect=slow_fetch):
            words, warnings = app.prefix_expansion_candidates(
                ["opendict"], "수", [seed], app.Filters(), deadline=time.monotonic() - 1,
            )
        self.assertEqual(words, [])
        self.assertIn(app.REQUEST_TIME_BUDGET_WARNING, warnings)

    def test_search_route_passes_shared_deadline_to_rare_final_candidates(self):
        candidates = [app.normalize_item({"word": f"리가{i}", "sense": {"pos": "명사"}}, "stdict") for i in range(5)]
        with patch.object(app, "paged_search", return_value=(candidates, 495, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])) as rare, \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=all&sort=next&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("deadline", rare.call_args.kwargs)
        self.assertIsInstance(rare.call_args.kwargs["deadline"], float)

    def test_search_route_gives_collection_a_smaller_deadline_than_analysis(self):
        # 회귀 테스트(2026-09-16): 후보 수집(rare_final_candidates)이 판정
        # (analyse_words)과 같은 마감을 쓰면, 후보가 많은 흔한 글자가 수집
        # 단계에서만 예산을 다 써 판정을 한 번도 못 해보는 문제가 있었다.
        # 수집에는 짧은 마감을, 판정에는 전체 예산을 줘야 한다.
        candidates = [app.normalize_item({"word": f"리가{i}", "sense": {"pos": "명사"}}, "stdict") for i in range(5)]
        with patch.object(app, "paged_search", return_value=(candidates, 495, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])) as rare, \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "analyse_words", return_value=([], [])) as analyse:
            response = app.app.test_client().get("/api/search?query=리&dictionary=stdict&mode=all&sort=next&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertLess(rare.call_args.kwargs["deadline"], analyse.call_args.kwargs["deadline"])

    def test_fast_analysis_changes_ryum_to_yum(self):
        candidate = app.normalize_item({"word": "리놀륨", "sense": {"pos": "명사"}}, "opendict")
        follow = app.normalize_item({"word": "윰라대왕", "sense": {"pos": "명사"}}, "opendict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 0), ([follow], 2)]) as fetch:
            analysed, warnings = app.analyse_words(["opendict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["is_one_shot"])
        self.assertEqual(analysed[0]["next_word_count"], 2)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["륨", "윰"])

    def test_dueum_changes_knee_final_to_swamp(self):
        candidate = app.normalize_item({"word": "무릎", "sense": {"pos": "명사"}}, "stdict")
        follow = app.normalize_item({"word": "늪가", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 0), ([follow], 1)]) as fetch:
            analysed, warnings = app.analyse_words(["stdict"], [candidate], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertFalse(analysed[0]["is_one_shot"])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["릎", "늪"])

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

    def test_paged_search_uses_filtered_total_after_scanning_last_api_page(self):
        allowed_words = [
            app.normalize_item({"word": f"는개{index}", "sense": {"pos": "명사"}}, "stdict")
            for index in range(28)
        ]
        with patch.object(app, "fetch_dictionary", return_value=(allowed_words, 92)):
            first_words, first_total, _ = app.paged_search(["stdict"], "는", app.Filters(), 1)
            second_words, second_total, _ = app.paged_search(["stdict"], "는", app.Filters(), 2)
        self.assertEqual(len(first_words), 24)
        self.assertEqual(len(second_words), 4)
        self.assertEqual(first_total, 28)
        self.assertEqual(second_total, 28)

    # --- 조각 A 회귀 테스트: 사전 원본을 조금씩 이어서 훑기 ---
    def test_scan_dictionary_batch_fetches_pages_in_parallel(self):
        def fake_fetch(_dictionary, _query, start, _count, _filters, method="start", **_kwargs):
            page_words = [
                app.normalize_item({"word": f"단어{start}-{i}", "sense": {"pos": "명사"}}, "stdict")
                for i in range(100)
            ]
            return page_words, 1200

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, total, has_more, warnings = app.scan_dictionary_batch("stdict", "단", app.Filters(), 1, 5)
        self.assertEqual(warnings, [])
        self.assertEqual(total, 1200)
        self.assertEqual(len(words), 500)
        self.assertTrue(has_more)

    def test_scan_dictionary_batch_no_more_pages_when_total_exhausted(self):
        def fake_fetch(_dictionary, _query, start, _count, _filters, method="start", **_kwargs):
            return ([app.normalize_item({"word": "단하나", "sense": {"pos": "명사"}}, "stdict")], 1) if start == 1 else ([], 1)

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, total, has_more, warnings = app.scan_dictionary_batch("stdict", "단", app.Filters(), 1, 5)
        self.assertEqual(warnings, [])
        self.assertEqual(total, 1)
        self.assertEqual([w["word"] for w in words], ["단하나"])
        self.assertFalse(has_more)

    def test_scan_dictionary_batch_uses_fast_timeout_and_single_attempt(self):
        def fake_fetch(_dictionary, _query, start, _count, _filters, method="start", **kwargs):
            self.assertEqual(kwargs.get("request_timeout"), app.FAST_REQUEST_TIMEOUT)
            self.assertEqual(kwargs.get("attempts"), 1)
            return [], 0

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            app.scan_dictionary_batch("stdict", "단", app.Filters(), 1, 5)

    def test_scan_dictionary_batch_ignores_invalid_start_value_errors(self):
        def fake_fetch(_dictionary, _query, start, _count, _filters, method="start", **_kwargs):
            if start == 1:
                return [], 250
            raise app.ApiError("Invalid start value")

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            words, total, has_more, warnings = app.scan_dictionary_batch("stdict", "단", app.Filters(), 1, 5)
        self.assertEqual(warnings, [])
        self.assertEqual(total, 250)
        self.assertEqual(words, [])

    def test_scan_dictionary_batch_stops_mid_flight_past_deadline(self):
        # 회귀 테스트(2026-09-16): 마감 시각을 시작 '전'에만 확인하고 묶음을
        # 이미 던진 뒤에는 끝까지 기다리는 버그가 있었다(실 서비스에서 동시
        # 조회 수를 줄였더니 '리'처럼 묶음이 많은 글자가 20초 넘게 응답이
        # 아예 없는 걸로 확인). 이제는 진행 중에도 마감을 넘기면 멈춘다.
        def fake_fetch(_dictionary, _query, start, _count, _filters, method="start", **_kwargs):
            time.sleep(0.1)
            return [app.normalize_item({"word": f"단어{start}", "sense": {"pos": "명사"}}, "stdict")], 900

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch), \
             patch.object(app, "LOOKUP_WORKERS", 1):
            words, total, has_more, warnings = app.scan_dictionary_batch(
                "stdict", "단", app.Filters(), 1, 9, deadline=time.monotonic() + 0.15,
            )
        self.assertEqual(total, 900)
        self.assertLess(len(words), 9)  # 9묶음 다 못 끝내고 마감에 걸려 멈춘다
        self.assertIn(app.REQUEST_TIME_BUDGET_WARNING, warnings)

    # --- 조각 C 회귀 테스트: 한방단어를 조금씩 이어서 찾기(사용자 요청, 2026-09-16) ---
    def test_gather_one_shot_page_finds_rare_final_word_on_first_call(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "rare_final_candidates", return_value=([lithium], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "scan_dictionary_batch", return_value=([], 1, False, [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            new_words, confirmed, has_more, total, warnings = app.gather_one_shot_page(
                ["stdict"], "리", app.Filters(), True,
            )
        self.assertEqual(warnings, [])
        self.assertEqual([w["word"] for w in new_words], ["리튬"])
        self.assertEqual([w["word"] for w in confirmed], ["리튬"])
        self.assertFalse(has_more)

    def test_gather_one_shot_page_finds_word_with_ending_missing_from_rare_final_list(self):
        # 회귀 테스트(2026-09-15/16): '풰'로 끝나는 '차풰'는 희귀 받침 추측
        # 목록에 없어 rare_final_candidates로는 못 찾지만, 사전 원본을
        # 이어서 훑는 scan_dictionary_batch가 후보로 잡아낸다.
        surprise = app.normalize_item({"word": "차풰", "sense": {"pos": "명사", "definition": "'차표'의 방언."}}, "opendict")
        self.assertNotIn(app.last_hangul_syllable("차풰"), app.RARE_FINALS)
        with patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "scan_dictionary_batch", return_value=([surprise], 2552, False, [])), \
             patch.object(app, "continuation_count", return_value=(0, [])) as count:
            new_words, confirmed, has_more, total, warnings = app.gather_one_shot_page(
                ["opendict"], "차", app.Filters(), False,
            )
        self.assertEqual(warnings, [])
        self.assertEqual([w["word"] for w in new_words], ["차풰"])
        self.assertEqual(total, 2552)
        self.assertFalse(has_more)
        self.assertEqual(count.call_args.args[1], "풰")

    def test_gather_one_shot_page_resumes_progress_without_repeating_rare_pass(self):
        common = app.normalize_item({"word": "리본", "sense": {"pos": "명사"}}, "stdict")
        rare_word = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")

        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True, _slow=False):
            return (0, []) if syllable == "튬" else (5, [])

        with patch.object(app, "rare_final_candidates", return_value=([rare_word], [])) as rare, \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "scan_dictionary_batch", return_value=([common], 2, False, [])), \
             patch.object(app, "continuation_count", side_effect=count_for_syllable):
            first_new, first_confirmed, first_more, _total, _warn = app.gather_one_shot_page(
                ["stdict"], "리", app.Filters(), False,
            )
            second_new, second_confirmed, second_more, _total2, _warn2 = app.gather_one_shot_page(
                ["stdict"], "리", app.Filters(), False,
            )
        rare.assert_called_once()  # 두 번째 호출은 진행 상황을 이어받아 역검색을 다시 하지 않는다.
        self.assertEqual([w["word"] for w in first_confirmed], ["리튬"])
        # 두 번째 호출에서도 이미 확정된 '리튬'을 다시 새 결과로 보고하지 않는다.
        self.assertEqual(second_new, [])
        self.assertEqual([w["word"] for w in second_confirmed], ["리튬"])

    def test_gather_one_shot_page_has_more_false_once_everything_checked(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "rare_final_candidates", return_value=([lithium], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "scan_dictionary_batch", return_value=([], 1, False, [])), \
             patch.object(app, "continuation_count", return_value=(0, [])):
            _new, _confirmed, has_more, _total, _warnings = app.gather_one_shot_page(
                ["stdict"], "리", app.Filters(), True,
            )
        self.assertFalse(has_more)

    def test_one_shot_route_page_one_shows_all_confirmed_so_far(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        lithium["is_one_shot"] = True
        with patch.object(
            app, "gather_one_shot_page", return_value=([lithium], [lithium], False, 500, []),
        ) as gather:
            response = app.app.test_client().get(
                "/api/search?query=리&dictionary=stdict&mode=one-shot&sort=alphabet&page=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([w["word"] for w in response.json["words"]], ["리튬"])
        self.assertEqual(response.json["one_shot_count"], 1)
        self.assertFalse(response.json["has_more"])
        gather.assert_called_once()

    def test_one_shot_route_page_two_shows_only_newly_confirmed(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        lithium["is_one_shot"] = True
        cadmium = app.normalize_item({"word": "리듐", "sense": {"pos": "명사"}}, "stdict")
        cadmium["is_one_shot"] = True
        with patch.object(
            app, "gather_one_shot_page", return_value=([cadmium], [lithium, cadmium], True, 5000, []),
        ):
            response = app.app.test_client().get(
                "/api/search?query=리&dictionary=stdict&mode=one-shot&sort=alphabet&page=2"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([w["word"] for w in response.json["words"]], ["리듐"])
        self.assertEqual(response.json["one_shot_count"], 2)
        self.assertTrue(response.json["has_more"])

    def test_one_shot_route_merges_duplicate_headwords_even_when_metadata_differs(self):
        duplicate_a = app.normalize_item({"word": "인듐", "sense": {"pos": "명사", "definition": "은백색의 무른 금속 원소."}}, "stdict")
        duplicate_b = app.normalize_item({"word": "인듐", "sense": {"pos": "품사 없음", "definition": "은백색의 무른 금속 원소. "}}, "stdict")
        with patch.object(app, "gather_one_shot_page", return_value=([duplicate_a, duplicate_b], [duplicate_a, duplicate_b], False, 25, [])):
            response = app.app.test_client().get("/api/search?query=인&dictionary=stdict&mode=one-shot&sort=alphabet")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([word["word"] for word in response.json["words"]], ["인듐"])

    def test_one_shot_route_ignores_defer_counts(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "gather_one_shot_page", return_value=([lithium], [lithium], False, 5000, [])):
            response = app.app.test_client().get(
                "/api/search?query=리&dictionary=stdict&mode=one-shot&sort=alphabet&defer_counts=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["deferred"])
        self.assertEqual([w["word"] for w in response.json["words"]], ["리튬"])

    def test_fast_continuation_counts_marks_unstarted_lookups_as_time_short(self):
        # 목(mock) 호출이 즉시 끝나 버리면 wait(timeout=0)이 우연히 "이미 끝남"으로
        # 볼 수 있으므로, 아주 짧게 멈춰서 시간 부족 경로를 확실히 타게 한다.
        def slow_count(*_args, **_kwargs):
            time.sleep(0.05)
            return (0, [])

        with patch.object(app, "continuation_count", side_effect=slow_count):
            counts, warnings = app.fast_continuation_counts(
                ["stdict"], ["가", "나"], app.Filters(), True, deadline=time.monotonic() - 1,
            )
        self.assertEqual(counts["가"], (0, [app.REQUEST_TIME_BUDGET_WARNING]))
        self.assertEqual(counts["나"], (0, [app.REQUEST_TIME_BUDGET_WARNING]))
        self.assertIn(app.REQUEST_TIME_BUDGET_WARNING, warnings)

    def test_failed_fast_count_is_marked_unavailable_after_retry(self):
        candidate = app.normalize_item({"word": "는개", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "continuation_count", return_value=(0, ["응답 지연"])) as count:
            analysed, warnings = app.analyse_words(["stdict"], [candidate], app.Filters(), True, exact_counts=False, fast_all_counts=True)
        self.assertEqual(count.call_count, 2)
        self.assertFalse(analysed[0]["count_available"])
        self.assertEqual(analysed[0]["next_word_count"], 999999999)
        self.assertIn("응답 지연", warnings)

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

    def test_fast_analysis_verifies_rare_final_before_marking_one_shot(self):
        lithium = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        common = app.normalize_item({"word": "리본", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "continuation_count", return_value=(0, [])) as count:
            analysed, warnings = app.analyse_words(["stdict"], [lithium, common], app.Filters(), True, exact_counts=False)
        self.assertEqual(warnings, [])
        self.assertTrue(analysed[0]["is_one_shot"])
        self.assertFalse(analysed[1]["is_one_shot"])
        count.assert_called_once()

    def test_fast_all_counts_checks_candidates_in_priority_order(self):
        # 회귀 테스트(2026-09-15): 시간이 부족해 일부만 확인해도 '한방단어일
        # 가능성이 큰 후보'(candidate_priority가 앞에 둔 후보)부터 확인하도록,
        # 집합이 아니라 candidates 순서를 보존한 목록으로 조회를 넣는다.
        rare = app.normalize_item({"word": "리튬", "sense": {"pos": "명사"}}, "stdict")
        common = app.normalize_item({"word": "리본", "sense": {"pos": "명사"}}, "stdict")
        candidates = sorted([common, rare], key=app.candidate_priority)  # 리튬(희귀 받침)이 앞으로 온다
        with patch.object(app, "fast_continuation_counts", return_value=({}, [])) as fast_counts:
            app.analyse_words(["stdict"], candidates, app.Filters(), True, exact_counts=False, fast_all_counts=True)
        checked_syllables = fast_counts.call_args.args[1]
        self.assertEqual(checked_syllables, ["튬", "본"])


    # --- 조각 1 회귀 테스트: 한방 판정 정확도 ---
    def test_continuation_checks_second_page_when_first_page_filtered_but_total_large(self):
        follow = app.normalize_item({"word": "가나다", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "fetch_dictionary", side_effect=[([], 500), ([follow], 500)]) as fetch:
            count, warnings = app.continuation_count(["stdict"], "가", app.Filters(), False)
        self.assertEqual(warnings, [])
        self.assertEqual(count, 500)
        self.assertEqual([call.args[2] for call in fetch.call_args_list], [1, 2])

    def test_continuation_reverse_dueum_word_prevents_false_one_shot(self):
        # 앞말이 '여'로 끝나도 다음 사람은 '려'로 시작할 수 있으므로 한방이 아니다.
        follow = app.normalize_item({"word": "려증", "sense": {"pos": "명사"}}, "stdict")

        def fake_fetch(_d, query, _s, _c, _f, **_kwargs):
            return ([follow], 3) if query == "려" else ([], 0)

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            count, warnings = app.continuation_count(["stdict"], "여", app.Filters(), True, exact=False)
        self.assertEqual(warnings, [])
        self.assertGreater(count, 0)

    def test_continuation_count_uses_max_across_dictionaries_not_sum(self):
        a = app.normalize_item({"word": "가가", "sense": {"pos": "명사"}}, "stdict")
        b = app.normalize_item({"word": "가가", "sense": {"pos": "명사"}}, "opendict")

        def fake_fetch(dictionary, _q, _s, _c, _f, **_kwargs):
            return ([a], 100) if dictionary == "stdict" else ([b], 120)

        with patch.object(app, "fetch_dictionary", side_effect=fake_fetch):
            count, warnings = app.continuation_count(["stdict", "opendict"], "가", app.Filters(), False)
        self.assertEqual(warnings, [])
        self.assertEqual(count, 120)

    def test_dueum_reverse_variants_inverts_forward_rules(self):
        self.assertEqual(sorted(app.dueum_reverse_variants("여")), sorted(["려", "녀"]))
        self.assertEqual(sorted(app.dueum_reverse_variants("이")), sorted(["리", "니"]))
        self.assertEqual(app.dueum_reverse_variants("나"), ["라"])
        self.assertEqual(app.dueum_reverse_variants("노"), ["로"])
        self.assertEqual(app.dueum_reverse_variants("뇌"), ["뢰"])
        self.assertEqual(app.dueum_reverse_variants("각"), [])

    # --- 조각 4 회귀 테스트: 동음이의어 뜻 묶음 ---
    def test_two_senses_of_same_headword_merge_into_one_card(self):
        first = app.normalize_item({"word": "배", "sense": {"pos": "명사", "definition": "먹는 배."}}, "stdict")
        second = app.normalize_item({"word": "배", "sense": {"pos": "명사", "definition": "타는 배."}}, "stdict")
        third = app.normalize_item({"word": "배", "sense": {"pos": "명사", "definition": "배 [곱절]."}}, "stdict")
        fourth = app.normalize_item({"word": "배", "sense": {"pos": "명사", "definition": "신체 부위 배."}}, "stdict")
        merged = app.dedupe_display_words([first, second, third, fourth])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["definitions"]), 3)
        self.assertEqual(merged[0]["definition"], "먹는 배.")
        self.assertEqual(merged[0]["definitions"][0]["definition"], "먹는 배.")


    # --- 조각 5 회귀 테스트: 두 단계 로딩 + 이어갈 단어 수 전용 주소 ---
    def test_words_mode_defers_counts_when_requested(self):
        candidate = app.normalize_item({"word": "기쁨", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search", return_value=([candidate], 1, [])), \
             patch.object(app, "continuation_count") as count:
            response = app.app.test_client().get(
                "/api/search?query=기&dictionary=stdict&mode=words&sort=alphabet&defer_counts=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["deferred"])
        word = response.json["words"][0]
        self.assertIsNone(word["next_word_count"])
        self.assertIsNone(word["is_one_shot"])
        self.assertEqual(word["last_syllable"], "쁨")
        count.assert_not_called()

    def test_defer_counts_is_ignored_for_next_sort(self):
        candidate = app.normalize_item({"word": "기쁨", "sense": {"pos": "명사"}}, "stdict")
        with patch.object(app, "paged_search", return_value=([candidate], 1, [])), \
             patch.object(app, "rare_final_candidates", return_value=([], [])), \
             patch.object(app, "prefix_expansion_candidates", return_value=([], [])), \
             patch.object(app, "continuation_count", return_value=(7, [])):
            response = app.app.test_client().get(
                "/api/search?query=기&dictionary=stdict&mode=words&sort=next&defer_counts=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["deferred"])
        self.assertEqual(response.json["words"][0]["next_word_count"], 7)

    def test_continuations_route_returns_counts_and_one_shot_flags(self):
        def count_for_syllable(_dictionaries, syllable, _filters, _dueum, _exact=True, _slow=False):
            return (0, []) if syllable == "쁨" else (12, [])
        with patch.object(app, "continuation_count", side_effect=count_for_syllable):
            response = app.app.test_client().get(
                "/api/continuations?dictionary=stdict&syllables=쁨,가,쁨&dueum=false"
            )
        self.assertEqual(response.status_code, 200)
        counts = response.json["counts"]
        self.assertEqual(set(counts), {"쁨", "가"})
        self.assertTrue(counts["쁨"]["one_shot"])
        self.assertEqual(counts["쁨"]["count"], 0)
        self.assertFalse(counts["가"]["one_shot"])
        self.assertEqual(counts["가"]["count"], 12)

    def test_continuations_route_marks_failed_syllable_unavailable(self):
        with patch.object(app, "continuation_count", return_value=(0, ["응답 지연"])):
            response = app.app.test_client().get("/api/continuations?dictionary=stdict&syllables=가")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["counts"]["가"]["available"])
        self.assertIsNone(response.json["counts"]["가"]["count"])
        self.assertFalse(response.json["counts"]["가"]["one_shot"])

    def test_continuations_route_ignores_blank_and_non_hangul_syllables(self):
        response = app.app.test_client().get("/api/continuations?dictionary=stdict&syllables=,abc,12")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["counts"], {})

    def test_warm_route_returns_warming_status(self):
        response = app.app.test_client().get("/api/warm")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["warming"])

    def test_warm_probes_rare_finals_when_keys_present(self):
        app._last_warm = 0.0
        with patch.object(app.os, "getenv", return_value="fake-key"), \
             patch.object(app, "rare_final_candidates", return_value=([], [])) as rare:
            app._warm_rare_caches()
        self.assertTrue(rare.called)
        self.assertEqual([call.kwargs.get("deep") for call in rare.call_args_list], [False, False])


if __name__ == "__main__":
    unittest.main()
