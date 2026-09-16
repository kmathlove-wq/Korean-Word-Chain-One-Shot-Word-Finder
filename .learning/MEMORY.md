# Project Memory

- 실행: `python3`는 이 PC에서 깨진 별칭. `py -3 -m venv .venv` 만든 뒤 항상 `.venv\Scripts\python`으로 실행(Flask/requests/dotenv는 전역에 없음).
- 검사 3종: `.venv\Scripts\python -m unittest discover -s tests -v` / `... -m compileall -q app.py tests` / `node --check static/main.js`.
- 테스트는 모듈 전역 `app.cache`를 공유하므로 캐시가 결과에 영향 주는 새 테스트는 `setUp`에서 `app.cache._items.clear()` 필요.
- 두음 한방 판정은 `continuation_count()`에서만 역방향(`dueum_reverse_variants`)까지 확인. 단어 목록 경로(`paged_search_with_dueum`)는 건드리지 않는다.
- 우리말샘은 옛말의 옛한글을 사용자 지정 영역(PUA, 예: U+E451) 코드로 준다. 표준 글꼴에 그림이 없어 네모(□)로 보인다. 사용자 결정(2026-09-04): 표기는 그대로 두고 `main.js`의 `ARCHAIC_HANGUL` 정규식으로 찾아 카드에 안내 문구만 붙인다. 지우거나 변환하지 않는다.
- 두 단계 로딩(`defer_counts=1`, `next`·`one-shot` 정렬·`mode=one-shot` 모두 제외): `words`/`all`에서만 `search()`가 `describe_words_without_counts()`로 목록만 먼저(`deferred=true`), 화면 `fillDeferredCounts()`가 `GET /api/continuations`로 숫자·한방 뱃지를 채움.

## 한방단어 모드: 현재 구조(2026-09-16 재설계, 사용자 요청)

한방단어 모드는 **한 번에 다 찾지 않고 조금씩 이어서 찾는다**. `gather_one_shot_page()`가
같은 검색(검색어·사전·필터·두음)의 진행 상황을 `cache`에 저장해 두고, 호출될 때마다
끝 글자를 `ONE_SHOT_PAGE_SYLLABLE_BATCH`(=`LOOKUP_WORKERS`=6)개만 새로 확인해 새로
확정된 한방단어만 돌려준다(자체 시간 제한 `ONE_SHOT_PAGE_TIME_BUDGET`=10초). 후보가
모자라면 `scan_dictionary_batch()`로 사전 원본을 `ONE_SHOT_FORWARD_SCAN_PAGES`(5묶음
=500개)만 더 훑는다. 처음 한 번만 `rare_final_candidates()`/`prefix_expansion_candidates()`
(희귀 받침 역검색, 적중률 높고 빠름)로 후보를 보강한다. 라우트는 `page==1`이면 지금까지
확정된 전체를, `page>1`("다음 결과 보기")이면 새로 확정된 것만 돌려준다. 페이지 넘김을
다시 손대면 이 구조(진행 상황 캐시 + 소량씩 확인) 유지.

**여기 도달하기까지의 핵심 교훈** (자세한 시행착오는 git log 참고, 여기서는 결론만):
1. 손으로 정해둔 '희귀 받침 추측 목록'(`RARE_FINALS`, 20개)만으로 후보를 좁히면 그
   목록에 없는 받침(예: 차풰→풰, 치미는아픔→픔)으로 끝나는 한방단어를 통째로 놓친다.
   → 실제 사전 원본을 넓게 훑는 경로(`scan_dictionary_batch`)가 반드시 있어야 한다.
2. 하지만 "넓게 순서대로 훑기"만 쓰면(추측 목록을 아예 안 쓰면) 희귀 받침 후보를
   우연히 만날 확률이 낮아 적중률이 나쁘다. → `rare_final_candidates`(끝 글자로
   역검색, 적중률 높음)를 **먼저** 쓰고, 넓게 훑기는 보조로 남는 시간에만 쓴다.
3. 새로 만드는 '넓게 훑는' 함수는 항상 `FAST_REQUEST_TIMEOUT`·`attempts=1`을 쓸 것
   (기본 제한 시간을 쓰면 실 서비스에서 502/500이 났다, 2026-09-15 확인).
4. 시간 제한(`deadline`) 안전장치는 **시작 전 확인만으로는 부족**하다 — 일단 시작한
   병렬 조회 안에서도 `concurrent.futures.wait(timeout=remaining)` +
   `shutdown(wait=False, cancel_futures=True)`로 진행 중에 계속 확인해야 한다(시작
   전 확인만 있으면 동시 조회 수가 적을 때 마감을 한참 넘겨서까지 기다린다).
5. '후보 수집'과 '판정'이 같은 시간 예산을 공유하면, 후보 많은 흔한 글자가 수집에서만
   예산을 다 써 판정을 한 번도 못 해본다 — 수집엔 짧은 몫만(`COLLECTION_TIME_FRACTION`
   =40%), 판정엔 전체를 준다(broad_sort 경로의 `sort=next`/`sort=one-shot`, `REQUEST_TIME_BUDGET`
   =15초에 여전히 적용 중).
6. 동시 요청 수(`LOOKUP_WORKERS`)를 늘리는 게 항상 빠르게 만들지 않는다 — 실 서비스
   진단(끝 글자 하나만 단독 호출: 2.5~5초, vs 24개 동시: 15초를 다 써도 몇 개 못 끝냄)
   결과 24→6으로 낮췄다. 무료 플랜 CPU와 국립국어원 서버 둘 다 그렇게 많은 동시
   요청을 감당하지 못해, 늘리는 게 자기 유발 정체로 낱개 요청을 오히려 느리게 했다.
7. "한 번에 다 모아서 판정, 실패하면 통째로 재시도"는 사용자 체감상 나쁘다(다시 검색
   7번·약 1분 만에야 목표 단어를 찾은 적 있음, 2026-09-16). 진행 상황을 캐시에
   보존해 "조금씩 이어서" 하는 편이 매 호출을 짧게 유지하면서도 결국 다 찾아낸다.
8. 이런 종류의 성능 버그는 로컬 목(mock) 테스트로 못 잡는다 — 배포된 사이트에
   직접 `curl`/진단 요청을 보내 재현·비교하는 절차가 결정적이었다.

- 끝 글자 병렬 조회는 `fast_continuation_counts()`로 통일(= `analyse_words` 빠른 경로 + `/api/continuations` 공유). `patient_retry`면 재시도를 `PATIENT_FAST_TIMEOUT(3,6)`로. `LOOKUP_WORKERS=6`, `rare_final_candidates`/`prefix_expansion_candidates`도 같은 작업자 수. 연결은 공유 `_http = requests.Session()`.
- (2026-09-16) `시간이 부족` 경고가 뜨면 화면에 "다시 검색" 버튼(`#retry-button`)이 함께 나온다. `showMessage(text, kind, retry)`의 세 번째 인자로 제어하며, 눌리면 `search()`를 그대로 다시 부른다(같은 폼 값으로 재검색, 데워진 캐시를 탐). 한방단어 모드는 이제 "다음 결과 보기"가 기본 진행 수단이라 이 버튼은 주로 `sort=next`/`sort=one-shot`·`/api/continuations` 쪽 시간 부족에 쓰인다.
- `/api/continuations` 는 한 요청에 끝 글자를 많이(20+) 넣고 NIKL이 크게 지연되면 gunicorn 60초 제한을 넘겨 502가 난다. 화면 `fillDeferredCounts()`가 8개씩 잘게 나눠 병렬 호출하고 실패분만 backoff(0·1.5·3·5초)로 재시도한다. 서버 `CONTINUATION_SYLLABLE_LIMIT=60`은 안전장치.
- 첫 검색 예열: `GET /api/warm` → 백그라운드로 `rare_final_candidates`의 '끝일치' fetch 캐시를 채움(검색어 무관, `sort=next`·`sort=one-shot` 정렬용). 화면이 페이지 로드 시 0·1.5·4초에 3회 호출(워커 2개라 프로세스별 캐시 대비). `_last_warm` 가드(CACHE_TTL/2).
