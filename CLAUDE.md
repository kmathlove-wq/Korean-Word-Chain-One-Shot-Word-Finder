# 끝말잇기 한방단어 검색기 — CLAUDE.md

## 프로젝트 개요

국립국어원의 표준국어대사전과 우리말샘 공식 Open API를 사용하는 한국어 끝말잇기 보조 웹앱이다. 사용자가 입력한 한글로 시작하는 단어를 찾고, 마지막 유효 한글 음절로 이어갈 단어가 있는지 확인해 한방단어를 판정한다.

프런트엔드는 HTML/CSS/JavaScript, 백엔드는 Python/Flask/requests로 구성된다. Python 서버가 필요하므로 VS Code Live Server나 GitHub Pages만으로는 정상 동작하지 않는다.

## 파일 구조

```text
/
├── app.py                 # Flask 라우트, 사전 API, 캐시, 필터, 한방 판정
├── requirements.txt       # Flask, requests, python-dotenv
├── .env                   # 실제 API 키, Git 제외 대상
├── .env.example           # 키 이름만 담은 공개 예시
├── .gitignore             # .env, 가상환경, 캐시 제외
├── README.md              # 설치·실행·테스트·배포 안내
├── templates/
│   └── index.html         # 검색 폼, 로딩, 결과 카드, 안내 UI
├── static/
│   ├── style.css          # 반응형 디자인과 접근성 스타일
│   └── main.js            # 입력 검증, API 호출, 정렬, 렌더링, 복사
├── tests/
│   └── test_app.py        # 파서·검증·라우트·한방 판정 테스트
├── AGENTS.md              # Codex 및 자동화 에이전트 작업 지침
└── CLAUDE.md              # Claude용 프로젝트 지식
```

## 실행

```bash
python -m venv .venv
pip install -r requirements.txt
python app.py
```

접속 주소는 `http://127.0.0.1:5000`이다. `/api/health`에서 서버 상태와 각 사전 키의 설정 여부를 확인한다. Flask 서버를 재시작해야 변경된 `.env` 값이 반영된다.

## 환경 변수와 보안

```env
STDICT_API_KEY=표준국어대사전_키
OPENDICT_API_KEY=우리말샘_키
```

- 실제 키는 반드시 `.env`에만 저장한다.
- `.env.example`에는 실제 값이나 값의 일부를 넣지 않는다.
- `.env`는 `.gitignore`에서 제외하며 Git에 강제 추가하지 않는다.
- 로그, 테스트 출력, 문서, 채팅에 키를 표시하지 않는다.
- 배포 시 호스팅 서비스의 환경 변수/Secret 기능에 같은 이름으로 등록한다.

## API 라우트

| 라우트 | 역할 |
|---|---|
| `GET /` | Jinja 템플릿으로 메인 화면 렌더링 |
| `GET /api/health` | 서버 상태와 사전별 키 설정 여부 반환 |
| `GET /api/search` | 시작 단어 검색, 필터, 한방 판정, 페이지 응답 |
| `GET /api/continuations` | 끝 글자 목록의 이어갈 단어 수만 병렬 계산 (`syllables=릉,강,…`) |

`/api/search`의 주요 매개변수는 `query`, `dictionary`, `mode`, `page`, `noun_only`, `include_proper`, `include_north`, `include_dialect`, `include_old`, `include_technical`, `include_single`, `dueum`, `defer_counts`이다. `dictionary` 값은 `stdict`, `opendict` 중 하나다. `mode` 값은 `all`, `words`, `one-shot` 중 하나다. 필터 매개변수를 생략하면 화면 체크박스 기본값(`FILTER_UI_DEFAULTS`: 한 글자 포함만 꺼짐, 나머지 켜짐, 두음 켜짐)을 따른다. `page`에 숫자가 아닌 값이 오면 1로 처리한다.

## 백엔드 핵심 규칙

- 공식 엔드포인트만 사용하며 네이버 사전 크롤링이나 가짜 데이터를 추가하지 않는다.
- 표준국어대사전은 `https://stdict.korean.go.kr/api/search.do`, 우리말샘은 `https://opendict.korean.go.kr/api/search`를 사용한다.
- JSON 응답을 우선 처리하고 JSON이 아니면 `xml.etree.ElementTree`로 XML을 파싱한다.
- 검색 방식은 `type_search=search`, `method=start`인 시작 일치 검색이다.
- 요청 제한 시간은 연결 10초/응답 20초이며 실패 시 한 번 재시도한다. 빠른 조회 경로는 연결 2초/응답 3초·1회다.
- 국립국어원 서버 연결은 공유 `requests.Session`(`_http`)으로 재사용한다. `fetch_dictionary()`는 `_http.get`을 쓴다.
- 독립적인 끝 글자 조회는 `fast_continuation_counts()`가 `LOOKUP_WORKERS`(20)개 작업자로 병렬 처리하고 실패분을 1회 재시도한다. `analyse_words()` 빠른 경로와 `/api/continuations`가 이 함수를 공유한다.
- 두 단계 로딩: `defer_counts=1`이고 정렬이 가나다·짧은·긴 순이면 `search()`가 `describe_words_without_counts()`로 단어 목록만(`deferred=true`, `next_word_count=null`) 먼저 돌려주고, 화면이 `/api/continuations`로 숫자·한방 표시를 채운다. `next`·`one-shot` 정렬과 `mode=one-shot`은 예전처럼 한 번에 계산한다.
- 화면 페이지 크기는 24개, 공식 API 묶음 크기는 100개다.
- 필터로 앞쪽 결과가 모두 제거될 수 있으므로 `paged_search()`는 필요한 결과가 모일 때까지 최대 `MAX_API_SCAN`(10)묶음 × 100개 ≈ 1000개 범위에서 다음 묶음을 확인한다.
- 메모리 `TTLCache`의 기본 만료 시간은 30분이다. 서버 재시작 시 사라지며 프로세스 간 공유되지 않는다. `fetch_dictionary()`는 캐시 원본 오염을 막으려고 항상 `copy.deepcopy`한 복사본을 돌려준다.
- 화면에서는 표준국어대사전 또는 우리말샘 중 하나만 선택해 검색한다.
- 한방단어 모드(`mode=one-shot`)는 `gather_one_shot_words()`가 전체 한방단어 목록을 한 번 모아 `(검색어, 사전, 필터, 두음)` 키로 캐시하고, 라우트는 그 목록을 페이지 크기로 자른다. 페이지 2 이상도 빈 결과 없이 정확히 동작한다.

## 한방단어 판정

- `last_hangul_syllable()`은 공백, 숫자, 괄호, 하이픈, 특수문자를 무시하고 마지막 `[가-힣]` 음절을 찾는다.
- `continuation_count()`는 마지막 음절로 시작하는 단어를 선택 사전에서 다시 검색한다.
- 첫 항목이 한 글자 제외 등의 필터에 걸리는 오판을 방지하기 위해 후속 검색은 최대 100개 묶음을 확인한다.
- 필터를 통과한 후속 단어가 하나라도 있으면 한방단어가 아니다.
- `dueum=true`이면 `continuation_count()`는 원음 + 정방향 변환음(`dueum_variant`) + 역방향 원래 소리(`dueum_reverse_variants`, 예: 여→려·녀)를 모두 검사한다. 어느 하나라도 단어가 있으면 한방단어가 아니다. (단어 목록 경로 `paged_search_with_dueum`은 바꾸지 않는다.)
- 1페이지가 전부 필터에 걸려도 API 전체 수(`total`)가 한 페이지보다 크면 딱 한 페이지(`start=2`)를 더 확인한 뒤 0으로 판정한다.
- 이어갈 단어 수는 근사치다. 같은 음절을 두 사전에서 조회하면 겹치는 단어가 이중 계산되므로 사전 간에는 `max`, 서로 다른 두음 변형 간에는 `sum`으로 합친다. 이 수는 정렬·카드 표시용이며 한방 O/X 판정에는 쓰지 않는다.

## 두음법칙

두음 변환은 프런트엔드에 복제하지 않고 `app.py`의 초성/모음 규칙 집합(`DUEUM_L_TO_IEUNG`, `DUEUM_L_TO_NIEUN`, `DUEUM_N_TO_IEUNG`)과 `dueum_variant()` / `get_dueum_variants()` / `dueum_reverse_variants()`에서만 관리한다. 정방향 예: `려→여`, `라→나`, `녀→여`, `로→노`. 역방향(한방 판정 전용) 예: `여→려·녀`, `이→리·니`, `나→라`, `노→로`, `뇌→뢰`.

## 프런트엔드 규칙

- 폼 제출 또는 Enter 입력 시에만 검색한다.
- 클라이언트에서도 완성형 한글 1~20자인지 검사하지만 서버 검증을 항상 유지한다.
- 사용자/API 문자열은 `escapeHtml()`을 거쳐 렌더링한다. 동음이의어는 `word.definitions`(최대 3개)를 번호 목록으로 그린다.
- 로딩·메시지·결과 영역은 `hidden` 속성으로 제어하며 `[hidden]{display:none!important}` 규칙을 유지한다.
- 정렬 `select`를 바꾸면 서버에 새로 요청한다(정렬 기준별 후보 수집 방식이 다르기 때문). 브라우저 안에서도 `sortedWords()`로 한 번 더 정리하지만 최종 정렬은 서버 응답 순서를 따른다.
- 느린 이전 응답이 새 응답을 덮어쓰지 않도록 `searchSeq`로 순번을 확인한다. `fillDeferredCounts()`도 `mySeq`가 어긋나면 조용히 멈춘다.
- `data.deferred`면 단어를 먼저 그리고, `fillDeferredCounts()`가 `/api/continuations`로 이어갈 단어 수·한방 뱃지를 채운 뒤 다시 그린다. 그동안 카드는 "확인 중…"으로 둔다.
- 옛한글(첫가끝 낱자모·확장·PUA)이 든 낱말은 `ARCHAIC_HANGUL` 정규식으로 찾아 카드에 안내 문구를 붙인다. 표기는 지우지 않고 그대로 둔다. 우리말샘은 이런 글자를 U+E451 같은 사용자 지정 영역 코드로 준다.
- 모바일에서 상세 설정은 `details` 요소로 접을 수 있어야 한다.

## 테스트

```bash
python -m unittest discover -s tests -v
python -m compileall -q app.py tests
node --check static/main.js
```

실제 키가 있을 때는 `기`, `가`, `트`, `슘`처럼 결과 규모가 다른 음절을 검색한다. 특히 `한 글자 포함`을 끈 상태에서도 `가`나 `트`로 이어갈 단어가 존재하면 한방단어로 나오지 않아야 한다.

## 변경 절차와 작업 규칙

1. 작업 전 `git status --short`로 기존 사용자 변경을 확인한다.
2. 관련 코드와 테스트를 읽고 기존 기능과 디자인을 보존한다.
3. API 키나 `.env` 내용을 출력하지 않는다.
4. 판정 로직 변경에는 재현 가능한 회귀 테스트를 추가한다.
5. 단위 테스트, Python/JavaScript 구문 검사, `git diff --check`를 실행한다.
6. 실제 API 검사는 키가 설정된 경우 최소 호출로 수행한다.
7. 변경 파일, 검증 결과, 남은 제한을 보고한다.

- 사용자 요청 없이 기존 변경을 되돌리지 않는다.
- 공식 API 대신 크롤링, 비공식 프록시, 가짜 데이터를 추가하지 않는다.
- API 필드는 공식 문서 또는 실제 응답으로 확인하며 추측하지 않는다.
- 요청받지 않은 커밋이나 push는 수행하지 않는다.
- 동작이나 실행 방법이 바뀌면 README, AGENTS.md, CLAUDE.md를 함께 갱신한다.
- 두 에이전트 문서는 각각 200줄 이하로 유지한다.

## 배포

- GitHub Pages는 Flask를 실행하지 못한다. GitHub는 소스 저장소로 사용하고 Render, Railway, PythonAnywhere 등 Python WSGI 호스팅을 연결한다.
- 운영 환경에서는 Flask 개발 서버를 사용하지 않고 Gunicorn 같은 WSGI 서버를 사용한다.
- API 신청 URL은 로컬 개발 시 `http://127.0.0.1:5000`, 배포 후 실제 HTTPS 서비스 주소로 갱신한다.
- 운영 전 요청 빈도 제한, 공유 캐시, 오류 로깅, HTTPS를 준비한다.

## 알려진 제한

- 국립국어원 API 응답 속도와 일일 호출 제한에 영향을 받는다.
- API 분류 필드가 일정하지 않아 일부 필터가 사전 웹사이트의 상세 검색과 완전히 같지 않을 수 있다.
- 두음 변형 결과 수는 중복 제거된 정확한 합계가 아닐 수 있으나 한방 여부는 하나라도 존재하는지를 기준으로 한다.
- 우리말샘 옛말의 옛한글은 사용자 지정 영역(PUA) 코드라 표준 글꼴에서 네모로 보인다. 표기는 그대로 두고 안내 문구만 붙인다.
- 한방 판정은 선택한 사전과 필터 기준이며 실제 게임 규칙을 보장하지 않는다.
