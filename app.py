"""국립국어원 공식 Open API 기반 끝말잇기 한방단어 검색기."""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

try:
    from dotenv import load_dotenv
except ImportError:  # requirements 설치 전에도 환경 변수 방식으로 실행 가능
    def load_dotenv() -> bool:
        return False

load_dotenv()

app = Flask(__name__)

PAGE_SIZE = 24
API_PAGE_SIZE = 100
MAX_CANDIDATES = 300
SORT_CANDIDATES = MAX_CANDIDATES
MAX_API_SCAN = 10
ONE_SHOT_SCAN_WINDOW = 5
PREFIX_EXPANSION_LIMIT = 6
PREFIX_EXPANSION_PAGE_SIZE = API_PAGE_SIZE
PREFIX_EXPANSION_SCAN_LIMIT = 1
RARE_PROBE_PAGE_SIZE = 100
RARE_PROBE_DEEP_START = 10
RARE_PROBE_SHALLOW_START = 2
RARE_CANDIDATE_LIMIT = 120
ONE_SHOT_CHUNK_SIZE = 8
ONE_SHOT_ANALYSIS_LIMIT = 80
NEXT_SORT_ANALYSIS_LIMIT = PAGE_SIZE * 2
FAST_CONTINUATION_PAGE_SIZE = API_PAGE_SIZE
FAST_REQUEST_TIMEOUT = (2, 3)
MAX_QUERY_LENGTH = 20
CACHE_TTL = 60 * 30
REQUEST_TIMEOUT = (10, 20)
REQUEST_ATTEMPTS = 2
RARE_FINALS = {
    "튬", "듐", "륨", "슘", "븀", "늄", "뮴", "윰", "쥼", "줌",
    "릇", "릎", "릉", "쁨", "쯤", "낌", "깡", "꽝", "쩡",
}
RARE_FINAL_PRIORITY = ["륨", "슘", "튬", "듐", "늄", "븀", "뮴", "윰", "쥼", "줌", "릇", "릎", "릉", "쁨", "쯤", "낌", "깡", "꽝", "쩡"]
DEEP_RARE_FINALS = {"륨", "슘", "튬", "듐", "늄"}
KNOWN_RARE_WORD_PROBES = {
    "리놀륨",
}
PREFIX_PROBE_SUFFIXES = ["산", "산화", "산수소", "화", "화나", "수소", "수산", "수산화"]

HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
HANGUL_INITIALS = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
HANGUL_VOWELS = ["ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ"]
DUEUM_L_TO_IEUNG = {"ㅑ", "ㅕ", "ㅖ", "ㅛ", "ㅠ", "ㅣ"}
DUEUM_L_TO_NIEUN = {"ㅏ", "ㅐ", "ㅓ", "ㅔ", "ㅗ", "ㅚ", "ㅜ", "ㅡ"}
DUEUM_N_TO_IEUNG = {"ㅑ", "ㅕ", "ㅖ", "ㅛ", "ㅠ", "ㅣ"}

DICTIONARIES = {
    "stdict": {
        "name": "표준국어대사전",
        "endpoint": "https://stdict.korean.go.kr/api/search.do",
        "key_env": "STDICT_API_KEY",
        "detail": "https://stdict.korean.go.kr/search/searchResult.do?searchKeyword={word}&pageSize=10",
    },
    "opendict": {
        "name": "우리말샘",
        "endpoint": "https://opendict.korean.go.kr/api/search",
        "key_env": "OPENDICT_API_KEY",
        "detail": "https://opendict.korean.go.kr/search/searchResult?query={word}",
    },
}


class ApiError(RuntimeError):
    pass


class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._items: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            item = self._items.get(key)
            if not item or time.monotonic() - item[0] >= self.ttl:
                self._items.pop(key, None)
                return None
            return item[1]

    def set(self, key: tuple, value: Any):
        with self._lock:
            self._items[key] = (time.monotonic(), value)


cache = TTLCache()


def compose_hangul(initial: str, vowel_index: int, final_index: int) -> str:
    return chr(HANGUL_BASE + HANGUL_INITIALS.index(initial) * 588 + vowel_index * 28 + final_index)


def dueum_variant(syllable: str) -> str:
    if len(syllable) != 1 or not (HANGUL_BASE <= ord(syllable) <= HANGUL_END):
        return syllable
    offset = ord(syllable) - HANGUL_BASE
    initial_index = offset // 588
    vowel_index = (offset % 588) // 28
    final_index = offset % 28
    initial = HANGUL_INITIALS[initial_index]
    vowel = HANGUL_VOWELS[vowel_index]
    if initial == "ㄹ" and vowel in DUEUM_L_TO_IEUNG:
        return compose_hangul("ㅇ", vowel_index, final_index)
    if initial == "ㄹ" and vowel in DUEUM_L_TO_NIEUN:
        return compose_hangul("ㄴ", vowel_index, final_index)
    if initial == "ㄴ" and vowel in DUEUM_N_TO_IEUNG:
        return compose_hangul("ㅇ", vowel_index, final_index)
    return syllable


def get_dueum_variants(syllable: str) -> list[str]:
    """원음과 두음법칙 변환음을 중복 없이 반환한다."""
    return list(dict.fromkeys([syllable, dueum_variant(syllable)]))


def last_hangul_syllable(word: str) -> str:
    matches = re.findall(r"[가-힣]", word or "")
    return matches[-1] if matches else ""


def clean_word(word: str) -> str:
    return re.sub(r"[\-^\s]", "", word or "").strip()


def validate_query(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("검색할 한글 글자나 단어를 입력해 주세요.")
    if len(value) > MAX_QUERY_LENGTH:
        raise ValueError(f"검색어는 {MAX_QUERY_LENGTH}자 이하로 입력해 주세요.")
    if not re.fullmatch(r"[가-힣]+", value):
        raise ValueError("완성된 한글 글자만 입력해 주세요.")
    return value


def as_bool(name: str, default: bool = False) -> bool:
    return request.args.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass
class Filters:
    noun_only: bool = False
    include_proper: bool = False
    include_north: bool = False
    include_dialect: bool = False
    include_old: bool = False
    include_technical: bool = False
    include_single: bool = False

    def key(self) -> tuple:
        return tuple(vars(self).values())


def scalar(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("#text", value.get("text", default))
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value if value is not None else default).strip()


def parse_json(data: dict, dictionary: str) -> tuple[list[dict], int]:
    channel = data.get("channel", data)
    raw_items = channel.get("item", []) if isinstance(channel, dict) else []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    total = int(scalar(channel.get("total", 0), "0") or 0)
    return [normalize_item(item, dictionary) for item in raw_items], total


def parse_xml(text: str, dictionary: str) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ApiError("사전에서 올바르지 않은 응답을 받았습니다.") from exc
    error = root.findtext(".//error") or root.findtext(".//message")
    if error and not root.findall(".//item"):
        raise ApiError(error)
    total = int(root.findtext(".//total") or 0)
    items = []
    for node in root.findall(".//item"):
        item = {child.tag: child.text or "" for child in node}
        sense = node.find("sense")
        if sense is not None:
            item["sense"] = {child.tag: child.text or "" for child in sense}
        items.append(normalize_item(item, dictionary))
    return items, total


def normalize_item(item: dict, dictionary: str) -> dict:
    sense = item.get("sense") or {}
    if isinstance(sense, list):
        sense = sense[0] if sense else {}
    if not isinstance(sense, dict):
        sense = {}
    word = clean_word(scalar(item.get("word")))
    detail = scalar(item.get("link"))
    if not detail and word:
        detail = DICTIONARIES[dictionary]["detail"].format(word=quote(word))
    return {
        "word": word,
        "part_of_speech": scalar(sense.get("pos") or item.get("pos"), "품사 미상"),
        "definition": scalar(sense.get("definition") or item.get("definition"), "뜻풀이 정보가 없습니다."),
        "category": scalar(sense.get("category") or item.get("category")),
        "type": scalar(sense.get("type") or item.get("type")),
        "dictionary_codes": [dictionary],
        "detail_url": detail,
    }


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def merge_word(target: dict, word: dict, key: tuple | None = None) -> None:
    merge_key = key or (word["word"],)
    current = target.get(merge_key)
    if current:
        current["dictionary_codes"].extend(code for code in word["dictionary_codes"] if code not in current["dictionary_codes"])
    else:
        target[merge_key] = word


def exact_word_key(word: dict) -> tuple[str, str, str]:
    return (compact_text(word.get("word", "")), compact_text(word.get("part_of_speech", "")), compact_text(word.get("definition", "")))


def dedupe_display_words(words: list[dict]) -> list[dict]:
    """API가 반복해서 준 같은 표제어는 화면에서 하나로 합친다."""
    merged: dict[tuple[str], dict] = {}
    for word in words:
        merge_word(merged, word, (compact_text(word.get("word", "")),))
    return list(merged.values())


def allowed(word: dict, filters: Filters) -> bool:
    word_text = word["word"]
    if not word_text or not last_hangul_syllable(word_text):
        return False
    if not filters.include_single and len(re.findall(r"[가-힣]", word_text)) == 1:
        return False
    pos, category, kind = word["part_of_speech"], word["category"], word["type"]
    joined = f"{pos} {category} {kind}"
    if filters.noun_only and "명사" not in pos and pos not in {"품사 미상", "품사 없음", ""}:
        return False
    exclusions = [
        (filters.include_proper, "고유 명사"), (filters.include_north, "북한어"),
        (filters.include_dialect, "방언"), (filters.include_old, "옛말"),
    ]
    if any(not enabled and marker in joined for enabled, marker in exclusions):
        return False
    if not filters.include_technical and category and category not in {"일반", ""}:
        return False
    return True


def fetch_dictionary(
    dictionary: str,
    query: str,
    start: int,
    count: int,
    filters: Filters,
    method: str = "start",
    request_timeout: tuple[int, int] = REQUEST_TIMEOUT,
    attempts: int = REQUEST_ATTEMPTS,
) -> tuple[list[dict], int]:
    config = DICTIONARIES[dictionary]
    key = os.getenv(config["key_env"], "").strip()
    if not key:
        raise ApiError(f"{config['name']} API 키가 설정되지 않았습니다.")
    cache_key = (dictionary, query, method, filters.key(), start, count)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {"key": key, "q": query, "req_type": "json", "type_search": "search", "method": method, "start": start, "num": count, "advanced": "y"}
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(config["endpoint"], params=params, timeout=request_timeout)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.35)
    else:
        if isinstance(last_error, requests.Timeout):
            raise ApiError(f"{config['name']} 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.") from last_error
        raise ApiError(f"{config['name']}에 연결할 수 없습니다.") from last_error
    try:
        data = response.json()
        if isinstance(data, dict) and (data.get("error") or data.get("message")) and not data.get("channel"):
            raise ApiError(scalar(data.get("error") or data.get("message")))
        result = parse_json(data, dictionary)
    except ValueError:
        result = parse_xml(response.text, dictionary)
    result = ([word for word in result[0] if allowed(word, filters)], result[1])
    cache.set(cache_key, result)
    return result


def selected_dictionaries(value: str) -> list[str]:
    if value not in DICTIONARIES:
        raise ValueError("표준국어대사전 또는 우리말샘 중 하나를 선택해 주세요.")
    return [value]


def merged_search(dictionaries: list[str], query: str, filters: Filters, limit: int = MAX_CANDIDATES) -> tuple[list[dict], int, list[str]]:
    merged: dict[str, dict] = {}
    totals, warnings = 0, []
    for dictionary in dictionaries:
        try:
            words, total = [], 0
            api_start = 1
            # 필터로 특정 API 묶음이 모두 제외되어도 뒤쪽에 허용 단어가 있을 수 있으므로
            # 공식 API가 허용하는 시작값 범위 안에서 제한된 깊이까지 계속 훑는다.
            while len(words) < limit and api_start <= MAX_API_SCAN:
                try:
                    batch, reported_total = fetch_dictionary(dictionary, query, api_start, API_PAGE_SIZE, filters)
                except ApiError as exc:
                    if "Invalid start value" in str(exc):
                        break
                    raise
                total = reported_total
                words.extend(batch)
                if api_start * API_PAGE_SIZE >= reported_total:
                    break
                api_start += 1
            totals += total
            for word in words:
                merge_word(merged, word)
        except ApiError as exc:
            warnings.append(str(exc))
    if not merged and warnings:
        raise ApiError(" ".join(warnings))
    return list(merged.values())[:limit], totals, warnings


def rare_final_candidates(dictionaries: list[str], query: str, filters: Filters) -> tuple[list[dict], list[str]]:
    """희귀 끝글자로 끝나는 단어를 역으로 찾아 한방 후보를 보강한다."""
    merged: dict[str, dict] = {}
    warnings = []

    def probe(job: tuple[str, str, str, int]) -> tuple[list[dict], list[str]]:
        dictionary, term, method, start = job
        try:
            words, _total = fetch_dictionary(dictionary, term, start, RARE_PROBE_PAGE_SIZE, filters, method)
            return words, []
        except ApiError as exc:
            if "Invalid start value" in str(exc):
                return [], []
            return [], [str(exc)]

    def collect(jobs: list[tuple[str, str, str, int]]) -> bool:
        if not jobs:
            return False
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as executor:
            futures = [executor.submit(probe, job) for job in dict.fromkeys(jobs)]
            for future in as_completed(futures):
                words, notes = future.result()
                warnings.extend(notes)
                for word in words:
                    if not word["word"].startswith(query) or last_hangul_syllable(word["word"]) not in RARE_FINALS:
                        continue
                    merge_word(merged, word)
                    if len(merged) >= RARE_CANDIDATE_LIMIT:
                        return True
        return bool(merged)

    shallow_jobs: list[tuple[str, str, str, int]] = []
    deep_jobs: list[tuple[str, str, str, int]] = []
    for dictionary in dictionaries:
        shallow_jobs.extend((dictionary, final, "end", 1) for final in RARE_FINAL_PRIORITY)
        for final in RARE_FINAL_PRIORITY:
            max_start = RARE_PROBE_DEEP_START if final in DEEP_RARE_FINALS else RARE_PROBE_SHALLOW_START
            deep_jobs.extend((dictionary, final, "end", start) for start in range(2, max_start + 1))
        if last_hangul_syllable(query) in RARE_FINALS:
            shallow_jobs.append((dictionary, query, "start", 1))
        shallow_jobs.extend((dictionary, word, "start", 1) for word in sorted(KNOWN_RARE_WORD_PROBES) if word.startswith(query))

    if not collect(shallow_jobs):
        collect(deep_jobs)
    return list(merged.values()), list(dict.fromkeys(warnings))


def one_shot_scan_candidates(dictionaries: list[str], query: str, filters: Filters, page: int) -> tuple[list[dict], int, bool, list[str]]:
    """한방단어 모드에서 시작 검색 결과를 구간별로 정밀 탐색한다."""
    merged: dict[str, dict] = {}
    total, warnings = 0, []
    start_from = (page - 1) * ONE_SHOT_SCAN_WINDOW + 1
    scan_until = page * ONE_SHOT_SCAN_WINDOW
    for dictionary in dictionaries:
        dictionary_total = 0
        for api_start in range(start_from, scan_until + 1):
            try:
                batch, dictionary_total = fetch_dictionary(dictionary, query, api_start, API_PAGE_SIZE, filters)
            except ApiError as exc:
                if "Invalid start value" in str(exc):
                    break
                warnings.append(str(exc))
                break
            for word in batch:
                merge_word(merged, word)
            if api_start * API_PAGE_SIZE >= dictionary_total:
                break
        total += dictionary_total
    if not merged and warnings:
        raise ApiError(" ".join(warnings))
    return list(merged.values()), total, scan_until * API_PAGE_SIZE < total, list(dict.fromkeys(warnings))


def prefix_expansion_candidates(dictionaries: list[str], query: str, seeds: list[dict], filters: Filters) -> tuple[list[dict], list[str]]:
    """이미 찾은 희귀 끝글자 후보의 앞부분으로 다시 좁혀 숨은 같은 계열 후보를 찾는다."""
    prefixes: list[str] = []
    if len(query) == 1:
        for suffix in PREFIX_PROBE_SUFFIXES:
            prefixes.append(query + suffix)
    for seed in sorted(seeds, key=lambda word: (len(word["word"]), word["word"])):
        text = seed["word"]
        if not text.startswith(query) or last_hangul_syllable(text) not in RARE_FINALS:
            continue
        size = len(query) + 2
        if len(text) <= size:
            continue
        prefix = text[:size]
        if prefix not in prefixes:
            prefixes.append(prefix)
        if len(prefixes) >= PREFIX_EXPANSION_LIMIT:
            break

    merged: dict[str, dict] = {}
    warnings: list[str] = []

    def probe(job: tuple[str, str, int]) -> tuple[list[dict], list[str]]:
        dictionary, prefix, api_start = job
        try:
            batch, _total = fetch_dictionary(
                dictionary, prefix, api_start, PREFIX_EXPANSION_PAGE_SIZE, filters,
                request_timeout=FAST_REQUEST_TIMEOUT, attempts=1,
            )
            return batch, []
        except ApiError as exc:
            return ([], []) if "Invalid start value" in str(exc) else ([], [str(exc)])

    jobs = [
        (dictionary, prefix, api_start)
        for prefix in dict.fromkeys(prefixes)
        for dictionary in dictionaries
        for api_start in range(1, PREFIX_EXPANSION_SCAN_LIMIT + 1)
    ]
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as executor:
            futures = [executor.submit(probe, job) for job in jobs]
            for future in as_completed(futures):
                batch, notes = future.result()
                warnings.extend(notes)
                for word in batch:
                    if not word["word"].startswith(query) or last_hangul_syllable(word["word"]) not in RARE_FINALS:
                        continue
                    merge_word(merged, word)
                    if len(merged) >= RARE_CANDIDATE_LIMIT:
                        break
                if len(merged) >= RARE_CANDIDATE_LIMIT:
                    for pending in futures:
                        pending.cancel()
                    break
    return list(merged.values())[:RARE_CANDIDATE_LIMIT], list(dict.fromkeys(warnings))


def continuation_count(dictionaries: list[str], syllable: str, filters: Filters, dueum: bool, exact: bool = True) -> tuple[int, list[str]]:
    variants = get_dueum_variants(syllable) if dueum else [syllable]
    total_count = 0
    warnings = []
    page_size = API_PAGE_SIZE if exact else FAST_CONTINUATION_PAGE_SIZE
    request_timeout = REQUEST_TIMEOUT if exact else FAST_REQUEST_TIMEOUT
    attempts = REQUEST_ATTEMPTS if exact else 1
    for variant in variants:
        for dictionary in dictionaries:
            try:
                # 첫 항목이 한 글자 등의 필터에 걸려도 오판하지 않도록 한 묶음을 확인한다.
                words, total = fetch_dictionary(
                    dictionary,
                    variant,
                    1,
                    page_size,
                    filters,
                    request_timeout=request_timeout,
                    attempts=attempts,
                )
                if words:
                    # 필터를 통과한 항목이 확인되면 원 API의 시작 일치 결과 수를 표시한다.
                    total_count += total
                    if not exact:
                        return total_count, list(dict.fromkeys(warnings))
            except ApiError as exc:
                warnings.append(str(exc))
    return total_count, list(dict.fromkeys(warnings))


def starting_total(dictionaries: list[str], query: str, filters: Filters) -> tuple[int, list[str]]:
    """시작 검색의 전체 개수를 가볍게 조회한다."""
    total, warnings = 0, []
    for dictionary in dictionaries:
        try:
            _words, dictionary_total = fetch_dictionary(dictionary, query, 1, API_PAGE_SIZE, filters)
            total += dictionary_total
        except ApiError as exc:
            warnings.append(str(exc))
    return total, list(dict.fromkeys(warnings))


def analyse_words(
    dictionaries: list[str],
    candidates: list[dict],
    filters: Filters,
    dueum: bool,
    exact_counts: bool = True,
    fast_all_counts: bool = False,
) -> tuple[list[dict], list[str]]:
    if not exact_counts:
        if fast_all_counts:
            uncertain_syllables = {last_hangul_syllable(word["word"]) for word in candidates}
        else:
            uncertain_syllables = {
                last_hangul_syllable(word["word"])
                for word in candidates
                if last_hangul_syllable(word["word"]) in RARE_FINALS
            }
        counts: dict[str, tuple[int, list[str]]] = {}
        warnings = []
        if uncertain_syllables:
            with ThreadPoolExecutor(max_workers=min(4, len(uncertain_syllables))) as executor:
                futures = {executor.submit(continuation_count, dictionaries, syllable, filters, dueum, False): syllable for syllable in uncertain_syllables}
                for future in as_completed(futures):
                    counts[futures[future]] = future.result()
        analysed = []
        for word in candidates:
            last = last_hangul_syllable(word["word"])
            count, notes = counts.get(last, (0 if last in RARE_FINALS else 1, []))
            warnings.extend(notes)
            if notes and fast_all_counts:
                count = 999999999
            is_one_shot = (last in RARE_FINALS or fast_all_counts) and count == 0 and not notes
            word.update(last_syllable=last, next_word_count=count, is_one_shot=is_one_shot,
                        dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"],
                        fast_judgement=True)
            analysed.append(word)
        return analysed, list(dict.fromkeys(warnings))
    syllables = {last_hangul_syllable(word["word"]) for word in candidates}
    counts: dict[str, tuple[int, list[str]]] = {}
    warnings = []
    # 서로 독립적인 끝 글자 조회를 병렬 처리해 순차 네트워크 대기를 없앤다.
    worker_limit = 8 if exact_counts else 4
    with ThreadPoolExecutor(max_workers=min(worker_limit, max(1, len(syllables)))) as executor:
        futures = {executor.submit(continuation_count, dictionaries, syllable, filters, dueum, exact_counts): syllable for syllable in syllables}
        for future in as_completed(futures):
            counts[futures[future]] = future.result()
    analysed = []
    for word in candidates:
        last = last_hangul_syllable(word["word"])
        count, notes = counts.get(last, (0, []))
        warnings.extend(notes)
        word.update(last_syllable=last, next_word_count=count, is_one_shot=count == 0,
                    dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"])
        analysed.append(word)
    return analysed, warnings


def order_words(words: list[dict], sort: str) -> list[dict]:
    if sort == "short":
        return sorted(words, key=lambda word: (len(word["word"]), word["word"]))
    if sort == "long":
        return sorted(words, key=lambda word: (-len(word["word"]), word["word"]))
    if sort == "next":
        return sorted(words, key=lambda word: (word["next_word_count"], word["word"]))
    if sort == "one-shot":
        return sorted(words, key=lambda word: (not word["is_one_shot"], word["word"]))
    return sorted(words, key=lambda word: word["word"])


def candidate_priority(word: dict) -> tuple[int, int, str]:
    last = last_hangul_syllable(word["word"])
    return (0 if last in RARE_FINALS else 1, len(word["word"]), word["word"])


def one_shot_page(dictionaries: list[str], candidates: list[dict], filters: Filters, dueum: bool, page: int) -> tuple[list[dict], list[dict], bool, list[str]]:
    ordered = sorted(candidates, key=candidate_priority)
    needed = page * PAGE_SIZE
    analysed: list[dict] = []
    one_shots: list[dict] = []
    warnings: list[str] = []
    for start in range(0, min(len(ordered), ONE_SHOT_ANALYSIS_LIMIT), ONE_SHOT_CHUNK_SIZE):
        chunk = ordered[start:start + ONE_SHOT_CHUNK_SIZE]
        checked, notes = analyse_words(dictionaries, chunk, filters, dueum, exact_counts=False)
        analysed.extend(checked)
        warnings.extend(notes)
        one_shots.extend(word for word in checked if word["is_one_shot"])
        if len(one_shots) >= needed:
            break
    start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
    reached_limit = len(analysed) >= min(len(ordered), ONE_SHOT_ANALYSIS_LIMIT)
    has_more = end < len(one_shots) or (len(one_shots) < needed and not reached_limit)
    return analysed, one_shots[start:end], has_more, warnings


def paged_search(dictionaries: list[str], query: str, filters: Filters, page: int) -> tuple[list[dict], int, list[str]]:
    """화면에 필요한 한 페이지만 가져와 첫 응답 시간을 제한한다."""
    merged: dict[str, dict] = {}
    total, warnings = 0, []
    needed = page * PAGE_SIZE
    for dictionary in dictionaries:
        try:
            words: dict[str, dict] = {}
            api_start, dictionary_total = 1, 0
            # 한 글자·전문어 등이 앞쪽을 채워 모두 걸러지는 경우 다음 묶음도 확인한다.
            while len(words) < needed and (not dictionary_total or (api_start - 1) * API_PAGE_SIZE < dictionary_total) and api_start <= MAX_API_SCAN:
                batch, dictionary_total = fetch_dictionary(dictionary, query, api_start, API_PAGE_SIZE, filters)
                for word in batch:
                    current = words.get(word["word"])
                    if current:
                        current["dictionary_codes"].extend(code for code in word["dictionary_codes"] if code not in current["dictionary_codes"])
                    else:
                        words[word["word"]] = word
                api_start += 1
            selected = list(words.values())[(page - 1) * PAGE_SIZE:needed]
            total += dictionary_total
            for word in selected:
                current = merged.get(word["word"])
                if current:
                    current["dictionary_codes"].extend(code for code in word["dictionary_codes"] if code not in current["dictionary_codes"])
                else:
                    merged[word["word"]] = word
        except ApiError as exc:
            warnings.append(str(exc))
    if not merged and warnings:
        raise ApiError(" ".join(warnings))
    return list(merged.values()), total, warnings


@app.get("/")
def index():
    return render_template("index.html")


@app.after_request
def prevent_api_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def health():
    return jsonify(status="ok", dictionaries={key: bool(os.getenv(value["key_env"])) for key, value in DICTIONARIES.items()})


@app.get("/api/search")
def search():
    try:
        query = validate_query(request.args.get("query", ""))
        dictionaries = selected_dictionaries(request.args.get("dictionary", "stdict"))
        mode = request.args.get("mode", "all")
        if mode not in {"all", "words", "one-shot"}:
            raise ValueError("올바른 검색 유형을 선택해 주세요.")
        sort = request.args.get("sort", "alphabet")
        if sort not in {"alphabet", "short", "long", "next", "one-shot"}:
            raise ValueError("올바른 정렬 기준을 선택해 주세요.")
        page = max(1, int(request.args.get("page", 1)))
        filters = Filters(**{name: as_bool(name) for name in Filters.__annotations__})
        dueum = as_bool("dueum", True)
        broad_sort = sort == "one-shot" or mode == "one-shot"
        if mode == "one-shot":
            warnings: list[str] = []
            if page == 1:
                candidates: list[dict] = []
                # 한 접두 계열을 찾았더라도 다른 계열을 놓치지 않도록
                # 희귀 끝글자 역검색 결과를 항상 합친다(예: 무수…륨 + 무릎).
                rare_candidates, rare_warnings = rare_final_candidates(dictionaries, query, filters)
                warnings.extend(rare_warnings)
                for word in rare_candidates:
                    if not any(existing["word"] == word["word"] for existing in candidates):
                        candidates.append(word)
                expanded_candidates, expanded_warnings = prefix_expansion_candidates(dictionaries, query, candidates, filters)
                warnings.extend(expanded_warnings)
                for word in expanded_candidates:
                    if not any(existing["word"] == word["word"] for existing in candidates):
                        candidates.append(word)
                if candidates:
                    raw_total, total_warnings = starting_total(dictionaries, query, filters)
                    warnings.extend(total_warnings)
                    raw_total = max(raw_total, len(candidates))
                    has_more = raw_total > ONE_SHOT_SCAN_WINDOW * API_PAGE_SIZE
                else:
                    candidates, raw_total, has_more, scan_warnings = one_shot_scan_candidates(dictionaries, query, filters, page)
                    warnings.extend(scan_warnings)
            else:
                candidates, raw_total, has_more, scan_warnings = one_shot_scan_candidates(dictionaries, query, filters, page)
                warnings.extend(scan_warnings)
            analysed, notes = analyse_words(dictionaries, candidates, filters, dueum, exact_counts=False)
            warnings.extend(notes)
            visible = order_words([word for word in analysed if word["is_one_shot"]], sort)[:PAGE_SIZE]
        elif broad_sort:
            candidates, raw_total, warnings = paged_search(dictionaries, query, filters, page)
            if sort == "one-shot":
                rare_candidates, rare_warnings = rare_final_candidates(dictionaries, query, filters)
                warnings.extend(rare_warnings)
                for word in rare_candidates:
                    if not any(existing["word"] == word["word"] for existing in candidates):
                        candidates.append(word)
                expanded_candidates, expanded_warnings = prefix_expansion_candidates(dictionaries, query, candidates, filters)
                warnings.extend(expanded_warnings)
                for word in expanded_candidates:
                    if not any(existing["word"] == word["word"] for existing in candidates):
                        candidates.append(word)
                raw_total = max(raw_total, len(candidates))
            analysis_limit = NEXT_SORT_ANALYSIS_LIMIT if sort == "next" else ONE_SHOT_ANALYSIS_LIMIT
            analysis_pool = candidates if sort == "next" else sorted(candidates, key=candidate_priority)
            analysed, notes = analyse_words(
                dictionaries,
                analysis_pool[:analysis_limit],
                filters,
                dueum,
                exact_counts=False,
                fast_all_counts=(sort == "next"),
            )
            warnings.extend(notes)
            ordered = order_words(analysed, sort)
            visible_pool = [word for word in ordered if mode != "one-shot" or word["is_one_shot"]]
            start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
            visible = visible_pool[start:end]
            has_more = end < len(visible_pool)
        else:
            candidates, raw_total, warnings = paged_search(dictionaries, query, filters, page)
            analysed, notes = analyse_words(
                dictionaries,
                candidates,
                filters,
                dueum,
                exact_counts=False,
                fast_all_counts=(sort == "next"),
            )
            warnings.extend(notes)
            visible = [w for w in order_words(analysed, sort) if mode != "one-shot" or w["is_one_shot"]]
            has_more = page * PAGE_SIZE < raw_total
        visible = dedupe_display_words(visible)
        one_shot_count = sum(word["is_one_shot"] for word in analysed)
        return jsonify(query=query, dictionary=request.args.get("dictionary", "stdict"), dictionary_name=" + ".join(DICTIONARIES[x]["name"] for x in dictionaries),
                       total=raw_total, api_total=raw_total, one_shot_count=one_shot_count,
                       page=page, page_size=PAGE_SIZE, has_more=has_more,
                       analysed_count=len(analysed), broad_sort=broad_sort, words=visible, warnings=list(dict.fromkeys(warnings)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except ApiError as exc:
        return jsonify(error=str(exc)), 502


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
