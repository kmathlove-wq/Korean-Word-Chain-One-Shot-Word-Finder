"""국립국어원 공식 Open API 기반 끝말잇기 한방단어 검색기."""
from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
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
# 검색 요청 하나에 쓸 수 있는 최대 시간(이어갈 단어 적은 순·한방단어 우선
# 정렬, /api/continuations 공통. 한방단어 모드 자체는 아래 ONE_SHOT_PAGE_*를
# 따로 쓴다). 실 서비스(Render) 앞단이 응답을 약 30~32초에서 끊는 걸 직접
# 재현해 확인했다(2026-09-15). 넘기면 그때까지 확인한 결과만 돌려주고,
# 나머지는 다음 검색(캐시가 데워져 더 빨라짐)에 맡긴다.
REQUEST_TIME_BUDGET = 15.0
REQUEST_TIME_BUDGET_WARNING = "시간이 부족해 일부 후보를 확인하지 못했습니다. 같은 글자로 다시 검색하면 이어서 더 찾아냅니다."
# 후보를 '모으는' 단계(collect_matching_words 등)에 예산을 다 뺏기면, 정작
# 한방단어를 가려내는 '판정' 단계(analyse_words)에 쓸 시간이 하나도 안 남는다
# (2026-09-16, 실 서비스에서 흔한 글자가 수집 단계에서만 예산을 다 쓰고
# 판정을 한 번도 못 해 0개가 나오는 걸 확인). 수집에는 예산의 앞부분만
# 떼어 주고, 나머지는 항상 판정에 남긴다.
COLLECTION_TIME_FRACTION = 0.4
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
# 서로 독립적인 끝 글자 조회를 한꺼번에 처리하는 최대 개수. 처음엔 24로 뒀지만
# 실 서비스에서 직접 재본 결과, 우리말샘에 단독으로 한 번만 물으면 2.5~5초인데
# 24개를 한꺼번에 쏘면(한방단어 검색 하나) 15초를 다 써도 몇 개밖에 못 끝냈다
# (2026-09-16). 무료 플랜의 작은 CPU 배분과 국립국어원 서버 양쪽 다 그렇게
# 많은 동시 요청을 감당하지 못해, 늘리는 게 오히려 낱개 요청을 훨씬 느리게
# 만드는 역효과였다. 줄여서 낱개 요청이 원래 속도(몇 초)를 유지하게 한다.
LOOKUP_WORKERS = 6
# 한방단어 모드는 '한 번에 다 찾기'가 아니라 '조금씩 이어서 찾기'로 동작한다
# (2026-09-16, 사용자 요청 — 한 번에 다 찾으려다 시간이 부족하면 다시 검색을
# 여러 번 해도 매번 처음부터 다시 훑어 오래 걸렸다: 실사용 확인, 다시 검색
# 7번·약 1분 만에야 목표 단어를 찾음). 단어를 '모으는' 속도와 '판정하는' 속도는
# 다르다 — 끝 글자 하나를 판정하려면 실제로 국립국어원에 물어봐야 해서 개당
# 2.5~5초 걸린다(2026-09-16 실 서비스 진단). 그래서 500~1000개씩 모아도, 그
# 안의 서로 다른 끝 글자는 ONE_SHOT_PAGE_SYLLABLE_BATCH(=LOOKUP_WORKERS의
# 배수)개만 한 호출에서 새로 판정한다(한방단어가 흔한 글자(예: '치', 끝 글자
# 종류가 수백 가지)일수록 판정 줄이 길어 '다음 결과 보기'를 많이 눌러야 할 수
# 있다, 2026-09-17 사용자 신고로 6→12로 늘림). 후보가 모자라면 사전 원본을
# ONE_SHOT_FORWARD_SCAN_PAGES 묶음만 더 훑는다. 화면의 '다음 결과 보기'를
# 누를 때마다 이어서 더 찾는다.
ONE_SHOT_PAGE_TIME_BUDGET = 10.0
ONE_SHOT_PAGE_SYLLABLE_BATCH = LOOKUP_WORKERS * 2
# 시작 단어 총계가 크면 한 번에 더 많이 훑어야 '다음 결과 보기'를 덜 눌러도
# 된다(사용자 요청, 2026-09-16). 5000개 이상이면 1000개씩(10묶음), 그 미만이면
# 500개씩(5묶음) 훑는다. 총계는 첫 훑기 전까지는 모르므로 그전엔 작은 쪽을 쓴다.
ONE_SHOT_FORWARD_SCAN_PAGES = 5
ONE_SHOT_FORWARD_SCAN_PAGES_LARGE = 10
ONE_SHOT_FORWARD_SCAN_LARGE_THRESHOLD = 5000
ONE_SHOT_FORWARD_SCAN_MAX_PAGES = 30
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

# 한 번 확인한 '이 끝 글자로 시작하는 단어가 몇 개인지'(0개=막다른 골목
# 포함)를 오래 기억해 둔다(사용자 요청, 2026-09-17). 한방단어 판정뿐 아니라
# '이어갈 단어가 적은 순' 정렬도 이 값을 그대로 쓴다. 다음에 다른 검색이
# 같은 끝 글자를 물어보면 국립국어원에 다시 묻지 않고 바로 답한다 — 쓸수록
# 빨라진다. 6개월(대략) 지나면 잊혀져 다시 확인한다(그 사이 사전에 새 단어가
# 생겨 개수가 달라졌을 수 있으니). 서버 메모리에만 있어 서버가 다시
# 시작되면(배포·재시작) 지워진다 — 완전히 영구 보존하려면 별도 저장소
# (데이터베이스)가 필요하다.
SYLLABLE_COUNT_TTL = 60 * 60 * 24 * 180
syllable_count_cache = TTLCache(ttl=SYLLABLE_COUNT_TTL)

# 국립국어원 서버에 매번 새로 접속(TLS 악수)하지 않고 연결을 재사용한다.
_http = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0)
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
    deadline: float | None = None,
) -> tuple[list[dict], list[str]]:
    """희귀 끝글자로 끝나는 단어를 역으로 찾아 한방 후보를 보강한다.

    `deadline`(`time.monotonic()` 기준 시각)을 주면 그 시각을 넘기지 않는다
    (2026-09-15, 모든 검색 응답을 `REQUEST_TIME_BUDGET` 안에 마치기 위한
    안전장치). 아직 시작 못 한 조회는 포기하고, 이미 실행 중인 조회는
    배경에서 계속 끝나되 결과는 버린다.
    """
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
        unique_jobs = list(dict.fromkeys(jobs))
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        executor = ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(unique_jobs)))
        futures = {executor.submit(probe, job): job for job in unique_jobs}
        done, not_done = wait(futures, timeout=remaining)
        for future in done:
            words, notes = future.result()
            warnings.extend(notes)
            for word in words:
                if not word["word"].startswith(query) or last_hangul_syllable(word["word"]) not in RARE_FINALS:
                    continue
                merge_word(merged, word)
        if not_done:
            warnings.append(REQUEST_TIME_BUDGET_WARNING)
        executor.shutdown(wait=False, cancel_futures=True)
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
        if deadline is None or time.monotonic() < deadline:
            collect(deep_jobs)
    elif not deep:
        collect(shallow_jobs)
    return list(merged.values())[:RARE_CANDIDATE_LIMIT], list(dict.fromkeys(warnings))


def prefix_expansion_candidates(
    dictionaries: list[str], query: str, seeds: list[dict], filters: Filters, deadline: float | None = None,
) -> tuple[list[dict], list[str]]:
    """이미 찾은 희귀 끝글자 후보의 앞부분으로 다시 좁혀 숨은 같은 계열 후보를 찾는다.

    `deadline`을 주면 그 시각을 넘기지 않는다(`rare_final_candidates`와 같은
    안전장치, 2026-09-15).
    """
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
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        executor = ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(jobs)))
        futures = {executor.submit(probe, job): job for job in jobs}
        done, not_done = wait(futures, timeout=remaining)
        for future in done:
            batch, notes = future.result()
            warnings.extend(notes)
            for word in batch:
                if not word["word"].startswith(query) or last_hangul_syllable(word["word"]) not in RARE_FINALS:
                    continue
                merge_word(merged, word)
        if not_done:
            warnings.append(REQUEST_TIME_BUDGET_WARNING)
        executor.shutdown(wait=False, cancel_futures=True)
    return list(merged.values())[:RARE_CANDIDATE_LIMIT], list(dict.fromkeys(warnings))


def continuation_count(dictionaries: list[str], syllable: str, filters: Filters, dueum: bool, exact: bool = True, slow: bool = False) -> tuple[int, list[str]]:
    count_cache_key = (tuple(dictionaries), syllable, filters.key(), dueum)
    remembered = syllable_count_cache.get(count_cache_key)
    if remembered is not None:
        # 예전에 이 끝 글자로 시작하는 단어 수를 확정해 둔 적이 있으면(0개=
        # 막다른 골목 포함) 다시 묻지 않고 바로 돌려준다(사용자 요청, 2026-09-17).
        return remembered
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
                        fast_result = (total_count + total, list(dict.fromkeys(warnings)))
                        if not fast_result[1]:
                            syllable_count_cache.set(count_cache_key, fast_result)
                        return fast_result
                    # 근사치: 같은 음절을 두 사전에서 더하면 겹치는 단어가 이중 계산된다
                    # (우리말샘이 표준국어대사전을 대부분 포함). 정확한 단어 목록이 없어
                    # 사전 간에는 max로만 합친다. 서로 다른 두음 변형(연 vs 련)은
                    # 겹치지 않으므로 변형끼리는 그대로 더한다.
                    variant_total = max(variant_total, total)
            except ApiError as exc:
                warnings.append(str(exc))
        if exact and variant_has_word:
            total_count += variant_total
    result_warnings = list(dict.fromkeys(warnings))
    if not result_warnings:
        # 오류 없이 끝까지 확인됐을 때만(0개든 그 이상이든) 오래 기억해 둔다.
        syllable_count_cache.set(count_cache_key, (total_count, result_warnings))
    return total_count, result_warnings


def fast_continuation_counts(
    dictionaries: list[str],
    syllables,
    filters: Filters,
    dueum: bool,
    patient_retry: bool = False,
    deadline: float | None = None,
) -> tuple[dict[str, tuple[int, list[str]]], list[str]]:
    """여러 끝 글자의 '이어갈 단어 수'를 한꺼번에 병렬로 빠르게 확인한다.

    각 값은 (개수, 경고목록) 꼴이다. 첫 조회는 짧은 제한 시간으로 빠르게 훑고,
    실패한 글자는 한 번 더 병렬로 확인한다. `patient_retry`면 재시도는
    긴 제한 시간(정확 조회)으로 해서 지연된 공식 API에서도 값을 받아 낸다.
    운영 서버 제한 시간을 넘기지 않도록 재시도 대상은 12개로 제한한다.

    `deadline`(`time.monotonic()` 기준 시각)을 주면 그 시각까지 끝나지 않은
    조회는 포기하고 '시간 부족' 경고로 남긴다(넘겨받지 못한 값은 한방단어로
    잘못 단정하지 않도록 판정에서 빠진다). 이미 시작한 조회는 배경에서
    계속 끝나지만 결과는 버린다.
    """
    unique = [syllable for syllable in dict.fromkeys(syllables) if syllable]
    counts: dict[str, tuple[int, list[str]]] = {}
    if not unique:
        return counts, []

    def run(subset: list[str], slow: bool) -> None:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        executor = ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(subset)))
        futures = {executor.submit(continuation_count, dictionaries, syllable, filters, dueum, False, slow): syllable for syllable in subset}
        done, not_done = wait(futures, timeout=remaining)
        for future in done:
            syllable = futures[future]
            try:
                counts[syllable] = future.result()
            except Exception:
                counts[syllable] = (0, [f"'{syllable}' 이어갈 단어 수를 확인하지 못했습니다."])
        for future in not_done:
            counts[futures[future]] = (0, [REQUEST_TIME_BUDGET_WARNING])
        executor.shutdown(wait=False, cancel_futures=True)

    run(unique, False)
    if deadline is None or time.monotonic() < deadline:
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
    deadline: float | None = None,
) -> tuple[list[dict], list[str]]:
    if not exact_counts:
        # 순서를 담는 목록으로 만든다(집합은 순서를 안 지킨다). candidates가
        # candidate_priority로 이미 정렬돼 있으면(희귀 받침 후보 먼저) 그 순서
        # 그대로 조회를 넣어서, 시간이 부족해 다 못 봐도 '한방단어일 가능성이
        # 큰 후보'부터 확인하게 한다(2026-09-15, 시간을 아무 후보에나 쓰지
        # 않도록 하는 개선).
        if fast_all_counts:
            uncertain_syllables = list(dict.fromkeys(last_hangul_syllable(word["word"]) for word in candidates))
        else:
            uncertain_syllables = list(dict.fromkeys(
                last_hangul_syllable(word["word"])
                for word in candidates
                if last_hangul_syllable(word["word"]) in RARE_FINALS
            ))
        counts, _count_warnings = fast_continuation_counts(dictionaries, uncertain_syllables, filters, dueum, deadline=deadline)
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


def scan_dictionary_batch(
    dictionary: str, query: str, filters: Filters, start_api_page: int, page_count: int,
    deadline: float | None = None,
) -> tuple[list[dict], int, bool, list[str]]:
    """사전 원본 목록을 `start_api_page`부터 `page_count`묶음만 병렬로 가져온다.

    한방단어 페이지 스캔에 쓴다(2026-09-16, 사용자 요청). 한 번에 다 모으지
    않고 조금씩(기본 5묶음=500개) 이어서 훑어, '다음 결과 보기' 한 번이
    항상 빠르게 끝나게 한다. (단어 목록, 이 사전의 전체 개수, 더 훑을 페이지가
    남았는지, 경고)를 돌려준다.
    """
    api_starts = list(range(start_api_page, start_api_page + page_count))

    def probe(api_start: int) -> tuple[list[dict], int, list[str]]:
        try:
            words, total = fetch_dictionary(
                dictionary, query, api_start, API_PAGE_SIZE, filters,
                request_timeout=FAST_REQUEST_TIMEOUT, attempts=1,
            )
            return words, total, []
        except ApiError as exc:
            return ([], 0, []) if "Invalid start value" in str(exc) else ([], 0, [str(exc)])

    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    executor = ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(api_starts)))
    futures = {executor.submit(probe, api_start): api_start for api_start in api_starts}
    done, not_done = wait(futures, timeout=remaining)
    collected: list[dict] = []
    total = 0
    warnings: list[str] = []
    for future in done:
        words, batch_total, notes = future.result()
        collected.extend(words)
        total = max(total, batch_total)
        warnings.extend(notes)
    if not_done:
        warnings.append(REQUEST_TIME_BUDGET_WARNING)
    executor.shutdown(wait=False, cancel_futures=True)
    last_scanned_page = start_api_page + page_count - 1
    has_more_pages = last_scanned_page * API_PAGE_SIZE < total and last_scanned_page < ONE_SHOT_FORWARD_SCAN_MAX_PAGES
    return collected, total, has_more_pages, list(dict.fromkeys(warnings))


def gather_one_shot_page(
    dictionaries: list[str], query: str, filters: Filters, dueum: bool,
) -> tuple[list[dict], list[dict], bool, int, list[str]]:
    """한방단어 모드를 '조금씩 이어서' 확인한다(사용자 요청, 2026-09-16).

    한 번에 다 모아서 판정하면(예전 방식) 시간이 부족할 때 사용자가 몇 번을
    다시 검색해도 매번 처음부터 다시 훑어 오래 걸렸다(실사용 확인: 다시
    검색 7번·약 1분 만에야 '치미는아픔'을 찾음). 대신 같은 검색(검색어·
    사전·필터·두음)의 진행 상황을 캐시에 저장해 두고, 호출될 때마다 아직
    안 본 끝 글자를 조금(`ONE_SHOT_PAGE_SYLLABLE_BATCH`개)만 더 확인해 새로
    확정된 한방단어만 돌려준다. 그래서 한 번 호출은 대개 한 라운드(몇 초)
    안에 끝나고, 화면의 '다음 결과 보기'를 여러 번 누르면 이어서 더 찾는다.

    후보는 두 곳에서 모은다: ① `rare_final_candidates`/`prefix_expansion_candidates`
    (희귀 받침 역검색, 적중률 높음, 처음 한 번만) ② `scan_dictionary_batch`로
    사전 원본을 조금씩 이어서 훑기(목록 밖 받침도 잡아냄).

    (이번 페이지에서 새로 확정된 한방단어, 지금까지 확정된 한방단어 전체,
    더 볼 게 남았는지, 시작 단어 총계, 경고)를 돌려준다.
    """
    dictionary = dictionaries[0]
    progress_key = ("one_shot_progress", query, dictionary, filters.key(), dueum)
    progress = cache.get(progress_key)
    if progress is None:
        progress = {
            "candidates": {},
            "syllables": [],
            "checked": {},
            "confirmed": {},
            "next_api_page": 1,
            "exhausted": False,
            "starting_total": 0,
            "warnings": [],
            "rare_pass_done": False,
        }

    deadline = time.monotonic() + ONE_SHOT_PAGE_TIME_BUDGET

    if not progress["rare_pass_done"]:
        rare_candidates, rare_warnings = rare_final_candidates(dictionaries, query, filters, deep=False, deadline=deadline)
        progress["warnings"].extend(rare_warnings)
        for word in rare_candidates:
            progress["candidates"].setdefault(word["word"], word)
        expanded_candidates, expanded_warnings = prefix_expansion_candidates(
            dictionaries, query, list(progress["candidates"].values()), filters, deadline=deadline,
        )
        progress["warnings"].extend(expanded_warnings)
        for word in expanded_candidates:
            progress["candidates"].setdefault(word["word"], word)
        progress["rare_pass_done"] = True

    unchecked = [s for s in progress["syllables"] if s not in progress["checked"]]
    if len(unchecked) < ONE_SHOT_PAGE_SYLLABLE_BATCH and not progress["exhausted"]:
        # 시작 단어 총계가 크면 한 번에 더 많이 훑는다(사용자 요청, 2026-09-16).
        # 총계를 아직 모르면(첫 훑기) 작은 쪽을 쓴다.
        scan_pages = (
            ONE_SHOT_FORWARD_SCAN_PAGES_LARGE
            if progress["starting_total"] >= ONE_SHOT_FORWARD_SCAN_LARGE_THRESHOLD
            else ONE_SHOT_FORWARD_SCAN_PAGES
        )
        new_words, total, has_more_pages, scan_warnings = scan_dictionary_batch(
            dictionary, query, filters, progress["next_api_page"], scan_pages, deadline,
        )
        progress["warnings"].extend(scan_warnings)
        progress["starting_total"] = max(progress["starting_total"], total)
        for word in new_words:
            progress["candidates"].setdefault(word["word"], word)
        progress["next_api_page"] += scan_pages
        if not has_more_pages:
            progress["exhausted"] = True
        ordered = sorted(progress["candidates"].values(), key=candidate_priority)
        progress["syllables"] = list(dict.fromkeys(last_hangul_syllable(w["word"]) for w in ordered))
        unchecked = [s for s in progress["syllables"] if s not in progress["checked"]]

    # 확인할 개수를 미리 자르지 않는다 — '이어갈 단어가 적은 순'(next 정렬)이
    # 후보 전체를 시간 예산 안에서 확인해 훨씬 빨리 한방단어를 찾아낸다는
    # 사용자 신고(2026-09-17)로 비교해 보니, 여기서만 12개로 잘라 놓아서
    # 남은 시간이 있어도 못 쓰고 있었다. `fast_continuation_counts`가
    # `deadline`으로 이미 알아서 멈추므로, 큐 전체를 넘겨 시간이 허락하는
    # 만큼 최대한 확인한다(sort=next의 analyse_words와 같은 방식).
    to_check = unchecked
    if to_check:
        counts, count_warnings = fast_continuation_counts(dictionaries, to_check, filters, dueum, deadline=deadline)
        # 시간이 부족해 경고가 붙은 항목은 '확인 완료'로 기록하지 않는다.
        # 그대로 기록하면 실제로는 못 물어본 끝 글자가 checked에 남아 다음
        # 호출에서도 다시 시도되지 않고 영영 버려진다(2026-09-17 발견).
        resolved = {syllable: value for syllable, value in counts.items() if not value[1]}
        progress["checked"].update(resolved)
        progress["warnings"].extend(count_warnings)

    new_this_round = []
    for word in progress["candidates"].values():
        text = word["word"]
        if text in progress["confirmed"]:
            continue
        last = last_hangul_syllable(text)
        if last not in progress["checked"]:
            continue
        count, notes = progress["checked"][last]
        if count == 0 and not notes:
            word.update(
                last_syllable=last, next_word_count=0, is_one_shot=True,
                dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"],
            )
            progress["confirmed"][text] = word
            new_this_round.append(word)

    has_more = not progress["exhausted"] or any(s not in progress["checked"] for s in progress["syllables"])
    warnings = list(dict.fromkeys(progress["warnings"]))
    cache.set(progress_key, progress)
    return new_this_round, list(progress["confirmed"].values()), has_more, progress["starting_total"], warnings


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
        # 채우고 싶을 때 defer_counts=1 을 보낸다. `이어갈 단어 적은 순`·`한방단어 우선`
        # 정렬은 개수가 정렬에 필요하므로 예전처럼 한 번에 계산한다.
        # 한방단어 모드는 '조금씩 이어서' 찾는다(사용자 요청, 2026-09-16).
        # 매 요청이 짧게 끝나도록 gather_one_shot_page()가 자체 시간 제한
        # (ONE_SHOT_PAGE_TIME_BUDGET)을 쓴다. '다음 결과 보기'를 누를 때마다
        # 새로 확정된 한방단어만 더 받아 화면에 이어 붙인다.
        defer_counts = as_bool("defer_counts", False) and mode != "one-shot" and sort not in {"one-shot", "next"}
        deferred = False
        # 요청 하나에 걸리는 시간이 REQUEST_TIME_BUDGET을 넘지 않도록, 이
        # 요청에서 하는 모든 넓은 탐색·다중 판정이 같은 마감 시각을 쓴다
        # (2026-09-15, 기기·필터와 무관하게 적용). 후보를 '모으는' 단계
        # (rare_final_candidates 등)에는 예산 앞부분만 주고, 나머지는 항상
        # '판정'(analyse_words)에 남긴다 — 안 그러면 흔한 글자가 수집에서만
        # 예산을 다 써 판정을 한 번도 못 해본다(2026-09-16, 실 서비스 확인).
        request_start = time.monotonic()
        deadline = request_start + REQUEST_TIME_BUDGET
        collection_deadline = request_start + REQUEST_TIME_BUDGET * COLLECTION_TIME_FRACTION
        if mode == "one-shot":
            new_words, confirmed_words, has_more, raw_total, warnings = gather_one_shot_page(
                dictionaries, query, filters, dueum,
            )
            analysed = confirmed_words
            visible = order_words(confirmed_words if page == 1 else new_words, sort)
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
                    deadline=collection_deadline,
                )
                warnings.extend(rare_warnings)
                for word in rare_candidates:
                    if not any(existing["word"] == word["word"] for existing in candidates):
                        candidates.append(word)
                expanded_candidates, expanded_warnings = prefix_expansion_candidates(dictionaries, query, candidates, filters, deadline=collection_deadline)
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
                deadline=deadline,
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
                    deadline=deadline,
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
                    deadline=deadline,
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
        deadline = time.monotonic() + REQUEST_TIME_BUDGET
        counts, warnings = fast_continuation_counts(dictionaries, syllables, filters, dueum, patient_retry=True, deadline=deadline)
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
