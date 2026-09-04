# Project Memory

- 실행: `python3`는 이 PC에서 깨진 별칭. `py -3 -m venv .venv` 만든 뒤 항상 `.venv\Scripts\python`으로 실행(Flask/requests/dotenv는 전역에 없음).
- 검사 3종: `.venv\Scripts\python -m unittest discover -s tests -v` / `... -m compileall -q app.py tests` / `node --check static/main.js`.
- 테스트는 모듈 전역 `app.cache`를 공유하므로 캐시가 결과에 영향 주는 새 테스트는 `setUp`에서 `app.cache._items.clear()` 필요.
- 한방단어 모드는 `gather_one_shot_words()`가 페이지 무관하게 전체 목록을 1회 수집·캐시하고 라우트가 슬라이스. 페이지 넘김을 다시 손대면 이 구조 유지.
- 두음 한방 판정은 `continuation_count()`에서만 역방향(`dueum_reverse_variants`)까지 확인. 단어 목록 경로(`paged_search_with_dueum`)는 건드리지 않는다.
- 우리말샘은 옛말의 옛한글을 사용자 지정 영역(PUA, 예: U+E451) 코드로 준다. 표준 글꼴에 그림이 없어 네모(□)로 보인다. 사용자 결정(2026-09-04): 표기는 그대로 두고 `main.js`의 `ARCHAIC_HANGUL` 정규식으로 찾아 카드에 안내 문구만 붙인다. 지우거나 변환하지 않는다.
- 첫 검색이 느린 원인(측정): 단어 목록은 2~3초면 오지만 카드마다 '이어갈 단어 수'를 세느라 국립국어원에 수십 번 물어봄. 캐시가 차면 0.3초.
- 두 단계 로딩(`defer_counts=1`, `next`·`one-shot` 정렬만 제외):
  - `words`/`all`: `search()`가 `describe_words_without_counts()`로 목록만 먼저(`deferred=true`), 화면 `fillDeferredCounts()`가 `GET /api/continuations`로 숫자·한방 뱃지를 채움.
  - `one-shot` 모드: `gather_one_shot_first_phase()`가 후보를 모아 `RARE_FINALS` 끝글자만 빠르게 판정(확정) + 나머지 후보(`one_shot_pending=true`)를 반환. 화면이 2단계로 나머지 확인 후 한방 아닌 카드 제거. `defer_counts` 없으면 `gather_one_shot_words()` 전체 캐시 경로 그대로.
- 끝 글자 병렬 조회는 `fast_continuation_counts()`로 통일(= `analyse_words` 빠른 경로 + `/api/continuations` 공유). `patient_retry`면 재시도를 `PATIENT_FAST_TIMEOUT(3,6)`로. `LOOKUP_WORKERS=36`(= `_http` 풀 크기), `rare_final_candidates`/`prefix_expansion_candidates`도 같은 작업자 수. 연결은 공유 `_http = requests.Session()`.
- `/api/continuations` 는 한 요청에 끝 글자를 많이(20+) 넣고 NIKL이 크게 지연되면 gunicorn 60초 제한을 넘겨 502가 난다. 화면 `fillDeferredCounts()`가 8개씩 잘게 나눠 병렬 호출하고 실패분만 backoff(0·1.5·3·5초)로 재시도한다. 서버 `CONTINUATION_SYLLABLE_LIMIT=60`은 안전장치.
- 첫 한방단어 검색 예열: `GET /api/warm` → 백그라운드로 `rare_final_candidates`의 '끝일치' fetch 캐시를 채움(검색어 무관). 화면이 페이지 로드 시 0·1.5·4초에 3회 호출(워커 2개라 프로세스별 캐시 대비). `_last_warm` 가드(CACHE_TTL/2).
- 측정(2026-09-04 배포): "시작하는 단어" 첫 화면 ~2초(전 18초). "한방단어" 1단계 후보/확정 한방단어 ~1~3초(예열·캐시 후), 2단계는 NIKL 상태 따라 십수 초까지. NIKL 지연은 대체로 일시적.
