"""국립국어원 공식 Open API 기반 끝말잇기 한방단어 검색기."""
from __future__ import annotations

import copy
import logging
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
from flask import Flask, jsonify, redirect, render_template, request, url_for

try:
    from dotenv import load_dotenv
except ImportError:  # requirements 설치 전에도 환경 변수 방식으로 실행 가능
    def load_dotenv() -> bool:
        return False

load_dotenv()

logger = logging.getLogger(__name__)

app = Flask(__name__)

PAGE_SIZE = 24
API_PAGE_SIZE = 100
MAX_API_SCAN = 10
PREFIX_EXPANSION_LIMIT = 6
PREFIX_EXPANSION_PAGE_SIZE = API_PAGE_SIZE
PREFIX_EXPANSION_SCAN_LIMIT = 1
RARE_PROBE_PAGE_SIZE = 100
RARE_PROBE_DEEP_START = 10
RARE_PROBE_SHALLOW_START = 2
RARE_CANDIDATE_LIMIT = 120
ONE_SHOT_ANALYSIS_LIMIT = 80
FAST_CONTINUATION_PAGE_SIZE = API_PAGE_SIZE
FAST_REQUEST_TIMEOUT = (2, 3)
# 빠른 경로 재시도용. 공식 API가 지연될 때 첫 조회(3초)에서 놓친 끝 글자를
# 조금 더 기다려 받아 낸다. 운영 서버 제한 시간(gunicorn 60초)을 넘기지 않도록
# 한 요청이 다루는 글자 수는 화면에서 잘게 나눈다.
PATIENT_FAST_TIMEOUT = (3, 6)
MAX_QUERY_LENGTH = 20
CACHE_TTL = 60 * 30
REQUEST_TIMEOUT = (10, 20)
REQUEST_ATTEMPTS = 2
# 서로 독립적인 끝 글자 조회를 한꺼번에 처리하는 최대 개수. 대부분 네트워크
# 대기(소켓)라 GIL 영향이 적어 스레드를 넉넉히 둔다. 아래 _http 연결 풀 크기와 맞춘다.
LOOKUP_WORKERS = 36
RARE_FINALS = {
    "튬", "듐", "륨", "슘", "븀", "늄", "뮴", "윰", "쥼", "줌",
    "릇", "릎", "릉", "쁨", "쯤", "낌", "깡", "꽝", "쩡", "슛",
}
RARE_FINAL_PRIORITY = ["륨", "슘", "튬", "듐", "늄", "븀", "뮴", "윰", "쥼", "줌", "릇", "릎", "릉", "쁨", "쯤", "낌", "깡", "꽝", "쩡", "슛"]
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

# 국립국어원 서버에 매번 새로 접속(TLS 악수)하지 않고 연결을 재사용한다.
_http = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=48, max_retries=0)
_http.mount("https://", _http_adapter)
_http.mount("http://", _http_adapter)


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


def dueum_reverse_variants(syllable: str) -> list[str]:
    """두음법칙으로 이 음절이 되는 '원래 소리' 음절들을 반환한다(역방향).

    끝말잇기에서 두음법칙을 허용하면, 앞말이 '여'로 끝나도 다음 사람은
    '려'나 '녀'로 시작할 수 있다. 그래서 한방(막다른) 판정을 할 때는
    원래 소리 표기까지 함께 확인해야 성급하게 0으로 판정하지 않는다.
    예: 여 -> [려, 녀], 이 -> [리, 니], 나 -> [라], 노 -> [로], 뇌 -> [뢰]
    """
    if len(syllable) != 1 or not (HANGUL_BASE <= ord(syllable) <= HANGUL_END):
        return []
    offset = ord(syllable) - HANGUL_BASE
    initial = HANGUL_INITIALS[offset // 588]
    vowel_index = (offset % 588) // 28
    final_index = offset % 28
    vowel = HANGUL_VOWELS[vowel_index]
    results: list[str] = []
    if initial == "ㅇ" and vowel in DUEUM_L_TO_IEUNG:
        results.append(compose_hangul("ㄹ", vowel_index, final_index))
        results.append(compose_hangul("ㄴ", vowel_index, final_index))
    if initial == "ㄴ" and vowel in DUEUM_L_TO_NIEUN:
        results.append(compose_hangul("ㄹ", vowel_index, final_index))
    return list(dict.fromkeys(results))


def convert_dueum_word(word: str) -> str:
    """단어 첫 음절에 두음법칙을 적용한 표기를 반환한다."""
    return dueum_variant(word[0]) + word[1:] if word else word


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


# 주소창 직접 호출용 필터 기본값 = 화면(index.html) 체크박스 기본 상태.
# 켜짐: 명사만/고유명사/북한어/방언/옛말/전문어 포함, 꺼짐: 한 글자 포함.
FILTER_UI_DEFAULTS = {
    "noun_only": True,
    "include_proper": True,
    "include_north": True,
    "include_dialect": True,
    "include_old": True,
    "include_technical": True,
    "include_single": False,
}


def safe_int(value: Any, default: int = 0) -> int:
    """API 응답의 숫자 필드가 비었거나 이상해도 영어 오류 대신 기본값을 쓴다."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


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
    total = safe_int(scalar(channel.get("total", 0), "0") or 0)
    return [normalize_item(item, dictionary) for item in raw_items], total


def parse_xml(text: str, dictionary: str) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ApiError("사전에서 올바르지 않은 응답을 받았습니다.") from exc
    error = root.findtext(".//error") or root.findtext(".//message")
    if error and not root.findall(".//item"):
        raise ApiError(error)
    total = safe_int(root.findtext(".//total") or 0)
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
    # API가 준 링크가 http/https가 아니면(javascript: 등) 버린다.
    if not detail.startswith(("http://", "https://")):
        detail = ""
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


DISPLAY_SENSE_LIMIT = 3


def dedupe_display_words(words: list[dict]) -> list[dict]:
    """같은 표제어는 화면에서 한 카드로 합치되, 서로 다른 뜻을 최대 3개까지 모은다.

    동음이의어(예: 배 - 과일/신체/탈것)는 같은 표제어라 하나로 묶이지만
    뜻이 다르므로 `definitions` 목록에 구분해 담는다. 첫 뜻은 기존 코드
    호환을 위해 `definition`/`part_of_speech`로도 그대로 남긴다.
    """
    merged: dict[str, dict] = {}
    for word in words:
        wkey = compact_text(word.get("word", ""))
        current = merged.get(wkey)
        if current is None:
            word["definitions"] = [{
                "definition": word.get("definition", ""),
                "part_of_speech": word.get("part_of_speech", ""),
            }]
            merged[wkey] = word
            continue
        for code in word["dictionary_codes"]:
            if code not in current["dictionary_codes"]:
                current["dictionary_codes"].append(code)
        if len(current["definitions"]) >= DISPLAY_SENSE_LIMIT:
            continue
        incoming = compact_text(word.get("definition", ""))
        if incoming and not any(compact_text(sense["definition"]) == incoming for sense in current["definitions"]):
            current["definitions"].append({
                "definition": word.get("definition", ""),
                "part_of_speech": word.get("part_of_speech", ""),
            })
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
        # 호출자가 analyse_words 등에서 dict를 직접 수정하므로 캐시 원본이
        # 오염되지 않도록 항상 개인 복사본을 돌려준다.
        return copy.deepcopy(cached)
    # 가정: 공식 API의 start 는 '페이지 번호'(1,2,3...)다. 만약 실제로는
    # '레코드 오프셋'이라면 100개를 넘는 페이지 넘김이 어긋난다. 키가 생기면
    # 보고서의 '키 없이 확인 불가' 항목대로 실제 응답으로 검증할 것.
    params = {"key": key, "q": query, "req_type": "json", "type_search": "search", "method": method, "start": start, "num": count, "advanced": "y"}
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = _http.get(config["endpoint"], params=params, timeout=request_timeout)
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
    return copy.deepcopy(result)


def selected_dictionaries(value: str) -> list[str]:
    if value not in DICTIONARIES:
        raise ValueError("표준국어대사전 또는 우리말샘 중 하나를 선택해 주세요.")
    return [value]


def rare_final_candidates(
    dictionaries: list[str],
    query: str,
    filters: Filters,
    deep: bool = True,
) -> tuple[list[dict], list[str]]:
    """희귀 끝글자로 끝나는 단어를 역으로 찾아 한방 후보를 보강한다."""
    merged: dict[str, dict] = {}
    warnings = []

    def probe(job: tuple[str, str, str, int]) -> tuple[list[dict], list[str]]:
        dictionary, term, method, start = job
        try:
            # 보조 후보 탐색 하나가 느려져 전체 웹 요청이 Render 제한 시간을
            # 넘기지 않도록 짧은 조회로만 시도한다. 실패는 경고로 남긴다.
            words, _total = fetch_dictionary(
                dictionary,
                term,
                start,
                RARE_PROBE_PAGE_SIZE,
                filters,
                method,
                request_timeout=FAST_REQUEST_TIMEOUT,
                attempts=1,
            )
            return words, []
        except ApiError as exc:
            if "Invalid start value" in str(exc):
                return [], []
            return [], [str(exc)]

    def collect(jobs: list[tuple[str, str, str, int]]) -> bool:
        if not jobs:
            return False
        with ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(jobs))) as executor:
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

    if deep and not collect(shallow_jobs):
        collect(deep_jobs)
    elif not deep:
        collect(shallow_jobs)
    return list(merged.values()), list(dict.fromkeys(warnings))


def prefix_expansion_candidates(dictionaries: list[str], query: str, seeds: list[dict], filters: Filters) -> tuple[list[dict], list[str]]:
    """이미 찾은 희귀 끝글자 후보의 앞부분으로 다시 좁혀 숨은 같은 계열 후보를 찾는다."""
    prefixes: list[str] = []
    has_rare_seed = any(
        word["word"].startswith(query) and last_hangul_syllable(word["word"]) in RARE_FINALS
        for word in seeds
    )
    if len(query) == 1 and has_rare_seed:
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
        with ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(jobs))) as executor:
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


def continuation_count(dictionaries: list[str], syllable: str, filters: Filters, dueum: bool, exact: bool = True, slow: bool = False) -> tuple[int, list[str]]:
    if dueum:
        # 두음법칙 허용 시: 원음 + 정방향 변환음 + 역방향(원래 소리)까지 모두 확인한다.
        variants = list(dict.fromkeys(
            [syllable, dueum_variant(syllable), *dueum_reverse_variants(syllable)]
        ))
    else:
        variants = [syllable]
    total_count = 0
    warnings = []
    page_size = API_PAGE_SIZE if exact else FAST_CONTINUATION_PAGE_SIZE
    # slow: 빠른 경로이되 지연된 공식 API에서도 값을 받아 내도록 조금 더 기다린다.
    request_timeout = REQUEST_TIMEOUT if exact else (PATIENT_FAST_TIMEOUT if slow else FAST_REQUEST_TIMEOUT)
    attempts = REQUEST_ATTEMPTS if exact else 1
    for variant in variants:
        variant_total = 0
        variant_has_word = False
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
                if not words and total > page_size:
                    # 1페이지가 전부 필터로 걸러졌지만 API 전체 결과 수는 더 많다면
                    # 성급하게 0으로 판정하지 않도록 딱 다음 한 페이지만 더 확인한다.
                    try:
                        words, total = fetch_dictionary(
                            dictionary, variant, 2, page_size, filters,
                            request_timeout=request_timeout, attempts=attempts,
                        )
                    except ApiError as exc:
                        if "Invalid start value" not in str(exc):
                            warnings.append(str(exc))
                if words:
                    variant_has_word = True
                    if not exact:
                        # 빠른 경로: 이어갈 단어가 하나라도 확인되면 즉시 종료한다.
                        return total_count + total, list(dict.fromkeys(warnings))
                    # 근사치: 같은 음절을 두 사전에서 더하면 겹치는 단어가 이중 계산된다
                    # (우리말샘이 표준국어대사전을 대부분 포함). 정확한 단어 목록이 없어
                    # 사전 간에는 max로만 합친다. 서로 다른 두음 변형(연 vs 련)은
                    # 겹치지 않으므로 변형끼리는 그대로 더한다.
                    variant_total = max(variant_total, total)
            except ApiError as exc:
                warnings.append(str(exc))
        if exact and variant_has_word:
            total_count += variant_total
    return total_count, list(dict.fromkeys(warnings))


def fast_continuation_counts(
    dictionaries: list[str],
    syllables,
    filters: Filters,
    dueum: bool,
    patient_retry: bool = False,
) -> tuple[dict[str, tuple[int, list[str]]], list[str]]:
    """여러 끝 글자의 '이어갈 단어 수'를 한꺼번에 병렬로 빠르게 확인한다.

    각 값은 (개수, 경고목록) 꼴이다. 첫 조회는 짧은 제한 시간으로 빠르게 훑고,
    실패한 글자는 한 번 더 병렬로 확인한다. `patient_retry`면 재시도는
    긴 제한 시간(정확 조회)으로 해서 지연된 공식 API에서도 값을 받아 낸다.
    운영 서버 제한 시간을 넘기지 않도록 재시도 대상은 12개로 제한한다.
    """
    unique = [syllable for syllable in dict.fromkeys(syllables) if syllable]
    counts: dict[str, tuple[int, list[str]]] = {}
    if not unique:
        return counts, []

    def run(subset: list[str], slow: bool) -> None:
        with ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(subset))) as executor:
            futures = {executor.submit(continuation_count, dictionaries, syllable, filters, dueum, False, slow): syllable for syllable in subset}
            for future in as_completed(futures):
                syllable = futures[future]
                try:
                    counts[syllable] = future.result()
                except Exception:
                    counts[syllable] = (0, [f"'{syllable}' 이어갈 단어 수를 확인하지 못했습니다."])

    run(unique, False)
    retry_syllables = [syllable for syllable, (_count, notes) in counts.items() if notes][:12]
    if retry_syllables:
        run(retry_syllables, patient_retry)
    warnings: list[str] = []
    for _syllable, (_count, notes) in counts.items():
        warnings.extend(notes)
    return counts, list(dict.fromkeys(warnings))


def describe_words_without_counts(words: list[dict]) -> list[dict]:
    """'이어갈 단어 수' 계산을 화면 뒤 단계로 미룰 때, 카드에 필요한
    나머지 정보(마지막 글자, 사전 이름)만 채우고 수치는 비워 둔다."""
    for word in words:
        word.update(
            last_syllable=last_hangul_syllable(word["word"]),
            next_word_count=None,
            is_one_shot=None,
            count_available=None,
            fast_judgement=True,
            dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"],
        )
    return words


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
        counts, _count_warnings = fast_continuation_counts(dictionaries, uncertain_syllables, filters, dueum)
        warnings = []
        analysed = []
        for word in candidates:
            last = last_hangul_syllable(word["word"])
            checked = last in counts
            count, notes = counts.get(last, (0 if last in RARE_FINALS else 999999999, []))
            warnings.extend(notes)
            if notes and fast_all_counts:
                count = 999999999
            is_one_shot = (last in RARE_FINALS or fast_all_counts) and count == 0 and not notes
            word.update(last_syllable=last, next_word_count=count, is_one_shot=is_one_shot,
                        dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"],
                        fast_judgement=True, count_available=checked and not notes)
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
            syllable = futures[future]
            try:
                counts[syllable] = future.result()
            except Exception:
                counts[syllable] = (0, [f"'{syllable}' 이어갈 단어 수를 확인하지 못했습니다."])
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
            scanned_all = not dictionary_total or (api_start - 1) * API_PAGE_SIZE >= dictionary_total
            # 마지막 API 묶음까지 확인했다면 필터를 통과한 실제 개수를 사용한다.
            total += len(words) if scanned_all else dictionary_total
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


def paged_search_with_dueum(dictionaries: list[str], query: str, filters: Filters, page: int, dueum: bool) -> tuple[list[dict], int, list[str]]:
    """한 음절 검색어는 원음과 두음 변환음의 시작 검색 결과를 합친다."""
    queries = get_dueum_variants(query) if dueum and len(query) == 1 else [query]
    merged: dict[str, dict] = {}
    total = 0
    warnings: list[str] = []
    for search_query in queries:
        words, query_total, notes = paged_search(dictionaries, search_query, filters, page)
        total += query_total
        warnings.extend(notes)
        for word in words:
            merge_word(merged, word)
    return list(merged.values()), total, list(dict.fromkeys(warnings))


def gather_one_shot_candidates(dictionaries: list[str], query: str, filters: Filters, dueum: bool) -> tuple[list[dict], int, list[str]]:
    """한방단어 후보 묶음만 모은다(판정 전). 시작 검색 + 희귀 끝글자 역검색 + 접두 확장."""
    candidates, starting_total, warnings = paged_search_with_dueum(dictionaries, query, filters, 1, dueum)
    if starting_total > PAGE_SIZE:
        rare_candidates, rare_warnings = rare_final_candidates(dictionaries, query, filters, deep=False)
        warnings.extend(rare_warnings)
        for word in rare_candidates:
            if not any(existing["word"] == word["word"] for existing in candidates):
                candidates.append(word)
        expanded_candidates, expanded_warnings = prefix_expansion_candidates(dictionaries, query, candidates, filters)
        warnings.extend(expanded_warnings)
        for word in expanded_candidates:
            if not any(existing["word"] == word["word"] for existing in candidates):
                candidates.append(word)
    return sorted(candidates, key=candidate_priority), starting_total, warnings


def gather_one_shot_first_phase(dictionaries: list[str], query: str, filters: Filters, dueum: bool) -> tuple[list[dict], list[dict], int, list[str]]:
    """한방단어 모드 1단계: 후보를 모아 '희귀 끝글자' 후보만 빠르게 판정한다.

    (확정된 한방단어[희귀 끝글자], 아직 판정 안 된 후보[비희귀 끝글자, is_one_shot=None],
    시작 단어 총계, 경고)를 돌려준다. 화면이 2단계에서 `/api/continuations`로
    나머지 후보의 끝글자를 확인해 한방단어를 추가한다.
    """
    ordered, starting_total, warnings = gather_one_shot_candidates(dictionaries, query, filters, dueum)
    rare_pool = [word for word in ordered if last_hangul_syllable(word["word"]) in RARE_FINALS]
    other_pool = [word for word in ordered if last_hangul_syllable(word["word"]) not in RARE_FINALS]
    analysed_rare, notes = analyse_words(dictionaries, rare_pool, filters, dueum, exact_counts=False, fast_all_counts=False)
    warnings.extend(notes)
    confirmed = [word for word in analysed_rare if word["is_one_shot"]]
    pending = describe_words_without_counts(other_pool)
    return confirmed, pending, starting_total, list(dict.fromkeys(warnings))


def gather_one_shot_words(dictionaries: list[str], query: str, filters: Filters, dueum: bool) -> tuple[list[dict], int, list[str]]:
    """한방단어 모드의 '확정된 한방단어 전체 목록'을 한 번에 모은다.

    시작 검색 + 희귀 끝글자 역검색 + 접두 확장 + 한방 판정을 모두 거친
    결과를 (한방단어 목록, 시작 단어 총계, 경고)로 돌려준다. 페이지와
    무관한 값이므로 (검색어, 사전들, 필터, 두음)으로 캐시하고, 라우트는
    이 목록을 페이지 크기로 잘라서 보여 준다. 더 이상 빈 페이지 반복 없음.
    """
    cache_key = ("one_shot_full", query, tuple(dictionaries), filters.key(), dueum)
    cached = cache.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    candidates, starting_total, warnings = paged_search_with_dueum(dictionaries, query, filters, 1, dueum)
    if starting_total > PAGE_SIZE:
        # 결과가 한 화면보다 많을 때만 얕은 역검색으로 뒤쪽의 희귀 후보를 보강한다.
        rare_candidates, rare_warnings = rare_final_candidates(dictionaries, query, filters, deep=False)
        warnings.extend(rare_warnings)
        for word in rare_candidates:
            if not any(existing["word"] == word["word"] for existing in candidates):
                candidates.append(word)
        expanded_candidates, expanded_warnings = prefix_expansion_candidates(dictionaries, query, candidates, filters)
        warnings.extend(expanded_warnings)
        for word in expanded_candidates:
            if not any(existing["word"] == word["word"] for existing in candidates):
                candidates.append(word)

    analysed, notes = analyse_words(
        dictionaries,
        sorted(candidates, key=candidate_priority),
        filters,
        dueum,
        exact_counts=False,
        fast_all_counts=True,
    )
    warnings.extend(notes)
    one_shots = [word for word in analysed if word["is_one_shot"]]
    result = (one_shots, starting_total, list(dict.fromkeys(warnings)))
    cache.set(cache_key, result)
    return copy.deepcopy(result)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/dueum")
def dueum_legacy():
    return redirect(url_for("dueum_guide"), code=301)


@app.get("/두음법칙 보기")
def dueum_guide():
    word = (request.args.get("word") or "").strip()
    converted = ""
    error = ""
    if word:
        try:
            word = validate_query(word)
            converted = convert_dueum_word(word)
        except ValueError as exc:
            error = str(exc)
    return render_template("dueum.html", word=word, converted=converted, error=error)


@app.after_request
def prevent_api_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def health():
    return jsonify(status="ok", dictionaries={key: bool(os.getenv(value["key_env"], "").strip()) for key, value in DICTIONARIES.items()})


_warm_lock = threading.Lock()
_last_warm = 0.0


def _warm_rare_caches() -> None:
    """희귀 끝글자 '끝일치' 검색 결과를 미리 받아 캐시에 채운다.

    이 결과는 검색어와 무관하므로(모든 '…륨' 단어 목록 등) 첫 한방단어
    검색이 이 캐시를 재사용해 훨씬 빨라진다. 화면이 페이지를 열 때 한 번
    부른다. 이미 최근에 데웠으면 건너뛴다.
    """
    global _last_warm
    with _warm_lock:
        if time.monotonic() - _last_warm < CACHE_TTL / 2:
            return
        _last_warm = time.monotonic()
    filters = Filters(**FILTER_UI_DEFAULTS)
    for dictionary in DICTIONARIES:
        if not os.getenv(DICTIONARIES[dictionary]["key_env"], "").strip():
            continue
        try:
            rare_final_candidates([dictionary], "￿", filters, deep=False)
        except Exception:
            logger.exception("cache warm failed")


@app.get("/api/warm")
def warm():
    threading.Thread(target=_warm_rare_caches, daemon=True).start()
    return jsonify(warming=True)


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
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        # 필터 기본값은 화면(index.html) 체크 상태와 맞춘다. 그래야 주소창에서
        # 바로 /api/search?query=기&dictionary=stdict 를 불러도 화면과 같은
        # 결과가 나온다. 화면 기본: 한 글자 포함만 꺼짐, 나머지는 켜짐.
        filters = Filters(**{name: as_bool(name, FILTER_UI_DEFAULTS.get(name, False)) for name in Filters.__annotations__})
        dueum = as_bool("dueum", True)
        broad_sort = sort in {"one-shot", "next"} or mode == "one-shot"
        # 화면이 목록을 먼저 그린 뒤 '이어갈 단어 수'를 뒤 단계(/api/continuations)에서
        # 채우고 싶을 때 defer_counts=1 을 보낸다. 한방단어 모드는 후보를 먼저 보여 주고
        # 화면이 이어서 한방 여부를 확인한다. `이어갈 단어 적은 순`·`한방단어 우선`
        # 정렬은 개수가 정렬에 필요하므로 예전처럼 한 번에 계산한다.
        defer_counts = as_bool("defer_counts", False) and (mode == "one-shot" or sort not in {"one-shot", "next"})
        deferred = False
        if mode == "one-shot" and defer_counts:
            # 1단계: 후보를 모으고 '희귀 끝글자' 후보만 빠르게 판정해 돌려준다.
            # 나머지 후보(is_one_shot=None)는 화면이 /api/continuations 로 확인한다.
            confirmed, pending, raw_total, warnings = gather_one_shot_first_phase(dictionaries, query, filters, dueum)
            for word in pending:
                word["one_shot_pending"] = True
            analysed = confirmed + pending
            safe_sort = sort if sort in {"alphabet", "short", "long"} else "alphabet"
            visible = order_words(confirmed, safe_sort) + pending
            deferred = True
            has_more = False
        elif mode == "one-shot":
            # 페이지 1에서 한방단어 전체 목록을 모아 캐시하고, 이후 페이지는
            # 그 목록을 잘라서 보여 준다. 빈 페이지 무한 반복이 사라진다.
            full_list, raw_total, warnings = gather_one_shot_words(dictionaries, query, filters, dueum)
            analysed = full_list
            start_index, end_index = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
            visible = order_words(full_list, sort)[start_index:end_index]
            has_more = end_index < len(full_list)
        elif broad_sort:
            candidates, raw_total, warnings = paged_search_with_dueum(dictionaries, query, filters, page, dueum)
            if sort == "one-shot" or (sort == "next" and page == 1):
                # 정렬 요청에서는 운영 서버 제한 시간을 넘기는 역검색 심층
                # 페이지까지 한 번에 훑지 않는다. 일반 시작 결과와 얕은 희귀
                # 후보를 먼저 보여 주고, 다음 요청은 캐시를 재사용한다.
                rare_candidates, rare_warnings = rare_final_candidates(
                    dictionaries,
                    query,
                    filters,
                    deep=(sort != "one-shot"),
                )
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
            analysis_limit = len(candidates) if sort == "next" else ONE_SHOT_ANALYSIS_LIMIT
            analysis_pool = sorted(candidates, key=candidate_priority)
            preliminary, notes = analyse_words(
                dictionaries,
                analysis_pool[:analysis_limit],
                filters,
                dueum,
                exact_counts=False,
                fast_all_counts=(sort == "next"),
            )
            warnings.extend(notes)
            analysed = preliminary
            ordered = order_words(preliminary, sort)
            visible_pool = [word for word in ordered if mode != "one-shot" or word["is_one_shot"]]
            if sort == "next":
                # paged_search가 이미 요청한 사전 페이지를 골랐으므로 다시
                # page 오프셋을 적용하지 않는다. 다음 버튼은 API 전체 수로 판단한다.
                visible = visible_pool[:PAGE_SIZE]
                has_more = page * PAGE_SIZE < raw_total
            else:
                # paged_search가 이미 현재 사전 페이지를 선택했다. 화면에
                # 실제로 올릴 카드만 다시 확인해 일반 끝글자의 임시값 1을
                # 실제 후속 단어 수로 교체한다.
                visible_candidates = visible_pool[:PAGE_SIZE]
                analysed, count_notes = analyse_words(
                    dictionaries,
                    visible_candidates,
                    filters,
                    dueum,
                    exact_counts=False,
                    fast_all_counts=True,
                )
                warnings.extend(count_notes)
                visible = order_words(analysed, sort)
                if sort == "one-shot":
                    # '한방단어 우선' 정렬은 시작 단어 전체 수(raw_total, 수천)로
                    # has_more를 부풀리지 않는다. 이번에 분석한 후보가 한 페이지를
                    # 넘칠 때만 다음 페이지를 제안한다.
                    has_more = len(visible_pool) > PAGE_SIZE
                else:
                    has_more = len(visible_pool) > PAGE_SIZE or page * PAGE_SIZE < raw_total
        else:
            candidates, raw_total, warnings = paged_search_with_dueum(dictionaries, query, filters, page, dueum)
            if defer_counts:
                # 1단계: 단어 목록만 빠르게 돌려준다. 개수·한방 표시는 화면이
                # /api/continuations 로 이어서 채운다. 이 분기의 정렬은
                # 가나다·짧은·긴 순뿐이라 개수 없이도 순서가 확정된다.
                analysed = describe_words_without_counts(candidates)
                visible = order_words(analysed, sort)
                deferred = True
            else:
                analysed, notes = analyse_words(
                    dictionaries,
                    candidates,
                    filters,
                    dueum,
                    exact_counts=False,
                    # 일반 목록에서도 임시값 1이 아니라 마지막 글자별 API 수를 표시한다.
                    fast_all_counts=True,
                )
                warnings.extend(notes)
                visible = order_words(analysed, sort)
            has_more = page * PAGE_SIZE < raw_total
        visible = dedupe_display_words(visible)
        one_shot_count = sum(1 for word in analysed if word.get("is_one_shot"))
        return jsonify(query=query, dictionary=request.args.get("dictionary", "stdict"), dictionary_name=" + ".join(DICTIONARIES[x]["name"] for x in dictionaries),
                       total=raw_total, api_total=raw_total, one_shot_count=one_shot_count,
                       page=page, page_size=PAGE_SIZE, has_more=has_more, deferred=deferred,
                       analysed_count=len(analysed), broad_sort=broad_sort, words=visible, warnings=list(dict.fromkeys(warnings)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except ApiError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        # 예기치 않은 오류에도 HTML 오류 문서 대신 프런트가 읽을 수 있는
        # JSON 계약을 유지한다. 내부 예외나 요청 정보는 응답에 노출하지 않는다.
        # 서버 로그에는 추적을 남기되 검색어/키는 절대 기록하지 않는다.
        logger.exception("search failed")
        return jsonify(error="검색 처리 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."), 500


CONTINUATION_SYLLABLE_LIMIT = 60


@app.get("/api/continuations")
def continuations():
    """카드의 '이어갈 단어 수'만 따로, 병렬로 빠르게 계산해 돌려준다.

    화면은 단어 목록을 먼저 그린 뒤 이 주소로 끝 글자들을 한꺼번에 물어
    각 카드의 숫자와 한방단어 표시를 나중에 채운다. 응답은
    {"counts": {"릉": {"count": 0, "available": true, "one_shot": true}, ...}} 꼴이다.
    """
    try:
        dictionaries = selected_dictionaries(request.args.get("dictionary", "stdict"))
        raw = request.args.get("syllables", "")
        syllables = [s for s in dict.fromkeys(raw.split(",")) if re.fullmatch(r"[가-힣]", s or "")]
        syllables = syllables[:CONTINUATION_SYLLABLE_LIMIT]
        filters = Filters(**{name: as_bool(name, FILTER_UI_DEFAULTS.get(name, False)) for name in Filters.__annotations__})
        dueum = as_bool("dueum", True)
        if not syllables:
            return jsonify(counts={}, warnings=[])
        counts, warnings = fast_continuation_counts(dictionaries, syllables, filters, dueum, patient_retry=True)
        payload = {}
        for syllable in syllables:
            count, notes = counts.get(syllable, (0, ["확인하지 못했습니다."]))
            available = syllable in counts and not notes
            payload[syllable] = {
                "count": count if available else None,
                "available": available,
                "one_shot": available and count == 0,
            }
        return jsonify(counts=payload, warnings=list(dict.fromkeys(warnings)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except ApiError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        logger.exception("continuations failed")
        return jsonify(error="이어갈 단어 수를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."), 500


if __name__ == "__main__":
    # 이 경로는 로컬 개발 전용이다. 운영 배포는 gunicorn(render.yaml)이
    # app:app 을 직접 실행하므로 이 블록을 절대 거치지 않는다.
    # FLASK_DEBUG=true 는 운영에서 켜지 않는다(FLASK_ENV=production 이면 무시).
    debug_enabled = (
        os.getenv("FLASK_DEBUG", "false").lower() == "true"
        and os.getenv("FLASK_ENV") != "production"
    )
    app.run(debug=debug_enabled)
