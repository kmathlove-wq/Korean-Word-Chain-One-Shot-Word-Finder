const form = document.querySelector('#search-form');
const queryInput = document.querySelector('#query');
const message = document.querySelector('#message');
const loading = document.querySelector('#loading');
const results = document.querySelector('#results');
const grid = document.querySelector('#word-grid');
const moreButton = document.querySelector('#more-button');
const moreTopButton = document.querySelector('#more-top-button');
const sortSelect = document.querySelector('#sort-select');
let state = { page: 1, words: [], hasMore: false, params: null, recentKeys: new Set(), prefetch: null, lastData: null };
// 옛한글(아주 오래된 한글)·특수 코드 글자. 표준 글꼴엔 그림이 없어 네모로 보인다.
const ARCHAIC_HANGUL = /[ᄀ-ᇿꥠ-꥿ힰ-퟿-]/;
let searchSeq = 0;

const setHidden = (element, hidden) => { element.hidden = hidden; };
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const wordKey = word => word.word;

function uniqueWords(words) {
  const unique = new Map();
  words.forEach(word => {
    if (!unique.has(word.word)) unique.set(word.word, word);
  });
  return [...unique.values()];
}

function showMessage(text, kind = 'error') {
  message.textContent = text;
  message.style.borderColor = kind === 'notice' ? '#176b45' : '#a83b37';
  setHidden(message, false);
}

function buildParams(page = 1) {
  const data = new FormData(form);
  const params = new URLSearchParams({query: data.get('query').trim(), dictionary: data.get('dictionary'), mode: data.get('mode'), sort: sortSelect.value, page});
  ['noun_only','include_proper','include_north','include_dialect','include_old','include_technical','include_single','dueum'].forEach(name => params.set(name, data.has(name)));
  // 두 단계 로딩: 목록을 먼저 받고 '이어갈 단어 수'·한방 여부는 두 번째 요청
  // (/api/continuations)으로 채운다. '이어갈 단어 적은 순'·'한방단어 우선' 정렬은
  // 개수가 정렬에 필요하므로 예전처럼 한 번에 계산한다.
  if (sortSelect.value !== 'next' && sortSelect.value !== 'one-shot') params.set('defer_counts', '1');
  return params;
}

function card(word) {
  const details = word.detail_url ? `<a href="${escapeHtml(word.detail_url)}" target="_blank" rel="noopener">사전에서 검색하기 ↗</a>` : '<span>검색 링크 없음</span>';
  const isNew = state.recentKeys.has(wordKey(word));
  const nextCount = word.next_word_count == null
    ? (word.count_available === false ? '확인 실패' : '확인 중…')
    : `${word.next_word_count}개`;
  const archaicNote = ARCHAIC_HANGUL.test(word.word)
    ? '<p class="archaic-note">이 낱말에는 아주 오래된 한글이 들어 있어요. 쓰는 기기에 따라 네모(□)로 보일 수 있으니 아래 뜻과 사전 링크로 확인하세요.</p>'
    : '';
  const hangulLength = (word.word.match(/[가-힣]/g) || []).length;
  const widthClass = hangulLength >= 18 ? ' very-wide' : hangulLength >= 10 ? ' wide' : '';
  const definitionHtml = Array.isArray(word.definitions) && word.definitions.length > 1
    ? `<ol class="definition definition-list">${word.definitions.map(sense => `<li>${escapeHtml(sense.definition)}</li>`).join('')}</ol>`
    : `<p class="definition">${escapeHtml(word.definition)}</p>`;
  const badgeHtml = word.is_one_shot ? '<span class="badge">한방단어</span>'
    : word.one_shot_pending ? '<span class="badge badge--pending">한방단어인지 확인 중…</span>' : '';
  return `<article class="word-card ${word.is_one_shot ? 'one-shot' : ''}${widthClass}"${isNew ? ' data-new-result="true"' : ''}>
    <div class="card-top"><h3>${escapeHtml(word.word)}</h3>${badgeHtml}</div>
    <p class="pos">${escapeHtml(word.part_of_speech)} · ${escapeHtml(word.dictionary)}</p>
    ${archaicNote}
    ${definitionHtml}
    <div class="stats"><span>마지막 글자 <strong>${escapeHtml(word.last_syllable)}</strong></span><span>이어갈 단어 <strong>${escapeHtml(nextCount)}</strong></span></div>
    <div class="card-actions">${details}<button class="copy" type="button" data-copy="${escapeHtml(word.word)}">복사</button></div>
  </article>`;
}

function sortedWords() {
  const words = [...state.words];
  const ko = (a, b) => a.word.localeCompare(b.word, 'ko');
  if (sortSelect.value === 'short') words.sort((a,b) => a.word.length - b.word.length || ko(a,b));
  else if (sortSelect.value === 'long') words.sort((a,b) => b.word.length - a.word.length || ko(a,b));
  else if (sortSelect.value === 'next') words.sort((a,b) => a.next_word_count - b.next_word_count || ko(a,b));
  else if (sortSelect.value === 'one-shot') words.sort((a,b) => Number(b.is_one_shot) - Number(a.is_one_shot) || ko(a,b));
  else words.sort(ko);
  return words;
}

function render(data) {
  grid.innerHTML = sortedWords().map(card).join('');
  document.querySelector('#result-title').textContent = `‘${data.query}’로 시작하는 단어 ${data.total}개`;
  const scope = data.broad_sort ? '우선 분석' : '분석';
  document.querySelector('#result-meta').textContent = `한방단어 ${data.one_shot_count}개 · 기준 사전: ${data.dictionary_name} · ${scope} ${data.analysed_count}개`;
  setHidden(results, false);
  setHidden(moreButton, !state.hasMore);
  setHidden(moreTopButton, !state.hasMore);
}

function scrollToNewResults() {
  const firstNewResult = grid.querySelector('[data-new-result="true"]');
  if (!firstNewResult) return false;
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  requestAnimationFrame(() => requestAnimationFrame(() => firstNewResult.scrollIntoView({behavior, block: 'start'})));
  return true;
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function requestSearch(params, attempt = 1) {
  const response = await fetch(`/api/search?${params}`, {cache: 'no-store'});
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    if (attempt < 2) {
      await wait(700);
      return requestSearch(params, attempt + 1);
    }
    throw new Error('서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.');
  }
  if ([500, 502, 503, 504].includes(response.status) && attempt < 2) {
    await wait(700);
    return requestSearch(params, attempt + 1);
  }
  if (!response.ok) throw new Error(data?.error || '검색 중 오류가 발생했습니다.');
  if (!data) throw new Error('서버 응답이 비어 있습니다. 잠시 후 다시 시도해 주세요.');
  return data;
}

function prefetchNextPage() {
  if (!state.hasMore || !state.params) return;
  const params = new URLSearchParams(state.params);
  if (params.get('sort') === 'one-shot' || params.get('mode') === 'one-shot') return;
  params.set('page', state.page + 1);
  const key = params.toString();
  state.prefetch = {
    key,
    promise: requestSearch(params)
      .then(data => ({data}))
      .catch(error => ({error})),
  };
}

// 2단계: 목록을 그린 뒤, 아직 비어 있는 '이어갈 단어 수'와 한방단어 표시를 채운다.
// 끝 글자를 8개씩 잘게 나눠 /api/continuations 를 병렬로 부른다(한 요청이 운영
// 서버 제한 시간에 걸리지 않도록). 국립국어원 응답이 지연되면 실패분만 다시 확인한다.
// 검색이 새로 시작되면(mySeq 불일치) 조용히 멈춘다.
async function fillDeferredCounts(mySeq, searchKey) {
  const source = new URLSearchParams(searchKey);
  const oneShotMode = source.get('mode') === 'one-shot';
  const filterNames = ['noun_only','include_proper','include_north','include_dialect','include_old','include_technical','include_single','dueum'];

  const fetchChunk = async syllables => {
    const params = new URLSearchParams({dictionary: source.get('dictionary'), syllables: syllables.join(',')});
    filterNames.forEach(name => { if (source.has(name)) params.set(name, source.get(name)); });
    try {
      const response = await fetch(`/api/continuations?${params}`, {cache: 'no-store'});
      if (!response.ok) return {};
      const body = await response.json().catch(() => null);
      return body?.counts || {};
    } catch { return {}; }
  };

  const fetchAll = async syllables => {
    const chunks = [];
    for (let i = 0; i < syllables.length; i += 8) chunks.push(syllables.slice(i, i + 8));
    const results = await Promise.all(chunks.map(fetchChunk));
    return Object.assign({}, ...results);
  };

  const applyAndRender = counts => {
    state.words.forEach(w => {
      const info = counts[w.last_syllable];
      if (info && info.available) {
        w.next_word_count = info.count;
        w.is_one_shot = info.one_shot;
        w.count_available = true;
      }
    });
    // 한방단어 모드: 한방이 아니라고 확인된 후보는 목록에서 뺀다.
    if (oneShotMode) state.words = state.words.filter(w => !(w.one_shot_pending && w.is_one_shot === false));
    if (state.lastData) {
      state.lastData.one_shot_count = state.words.filter(w => w.is_one_shot).length;
      render(state.lastData);
    }
  };

  const waits = [0, 1500, 3000, 5000];
  for (let attempt = 0; attempt < waits.length; attempt++) {
    const pending = [...new Set(state.words.filter(w => w.next_word_count == null && w.last_syllable).map(w => w.last_syllable))];
    if (!pending.length) break;
    if (waits[attempt]) await wait(waits[attempt]);
    if (mySeq !== searchSeq) return;
    const counts = await fetchAll(pending);
    if (mySeq !== searchSeq) return;
    applyAndRender(counts);
  }
  if (oneShotMode) {
    // 여러 번 시도해도 확인 못 한 후보는 한방단어라고 단정하지 않고 뺀다.
    state.words = state.words.filter(w => !w.one_shot_pending || w.is_one_shot === true);
  } else {
    // 일반 목록: 못 채운 끝 글자는 '확인 실패'로 표시한다.
    state.words.forEach(w => { if (w.next_word_count == null) w.count_available = false; });
  }
  if (state.lastData) {
    state.lastData.one_shot_count = state.words.filter(w => w.is_one_shot).length;
    render(state.lastData);
  }
  if (oneShotMode && !state.words.length) {
    showMessage('확인된 한방단어가 없습니다. 오류가 아니라, 선택한 사전과 필터 기준에서 끝까지 확인했지만 한방단어를 찾지 못한 상태입니다.', 'notice');
  }
}

async function search(page = 1, append = false) {
  const params = append ? new URLSearchParams(state.params) : buildParams(page);
  params.set('page', page);
  const query = params.get('query');
  if (!/^[가-힣]{1,20}$/.test(query)) { showMessage(query ? '완성된 한글을 20자 이하로 입력해 주세요.' : '검색할 한글 글자나 단어를 입력해 주세요.'); queryInput.focus(); return; }
  setHidden(message, true); setHidden(loading, false); if (!append) setHidden(results, true);
  moreButton.disabled = true;
  moreTopButton.disabled = true;
  const mySeq = ++searchSeq;
  try {
    const key = params.toString();
    const cached = append && state.prefetch?.key === key ? await state.prefetch.promise : null;
    if (mySeq !== searchSeq) return;
    if (cached?.error) throw cached.error;
    const data = cached?.data || await requestSearch(params);
    if (mySeq !== searchSeq) return;
    const existingKeys = new Set(state.words.map(wordKey));
    const incomingWords = uniqueWords(data.words);
    const newWords = append ? incomingWords.filter(word => !existingKeys.has(wordKey(word))) : [];
    const nextWords = uniqueWords(append ? [...state.words, ...incomingWords] : incomingWords);
    state = {page, words: nextWords, hasMore: data.has_more, params: key, recentKeys: append ? new Set(newWords.map(wordKey)) : new Set(), prefetch: null, lastData: data};
    render(data);
    if (data.deferred) fillDeferredCounts(mySeq, key);
    if (append && !scrollToNewResults()) requestAnimationFrame(() => moreButton.scrollIntoView({behavior: 'smooth', block: 'center'}));
    const warningText = data.warnings?.length ? `일부 결과 안내: ${data.warnings.join(' ')}` : '';
    if (!data.words.length) {
      let emptyText;
      if (params.get('mode') === 'one-shot' && data.has_more) emptyText = '이번 페이지에서는 한방단어를 찾지 못했습니다. 아래의 다음 결과 보기를 눌러 보세요.';
      else if (params.get('mode') === 'one-shot') emptyText = '확인된 한방단어가 없습니다. 오류가 아니라, 선택한 사전과 필터 기준에서 끝까지 확인했지만 한방단어를 찾지 못한 상태입니다.';
      else emptyText = '조건에 맞는 단어를 찾지 못했습니다. 필터를 바꿔 보세요.';
      showMessage(warningText ? `${warningText}\n${emptyText}` : emptyText, 'notice');
    } else if (warningText) {
      showMessage(warningText, 'notice');
    }
    prefetchNextPage();
  } catch (error) { showMessage(error.message); }
  finally { setHidden(loading, true); moreButton.disabled = false; moreTopButton.disabled = false; }
}

form.addEventListener('submit', event => { event.preventDefault(); search(); });
form.addEventListener('reset', () => setTimeout(() => { queryInput.value = ''; state = {page:1, words:[], hasMore:false, params:null, recentKeys:new Set(), prefetch:null}; setHidden(results,true); setHidden(message,true); }, 0));
document.querySelector('#clear-query').addEventListener('click', () => { queryInput.value = ''; queryInput.focus(); });
moreButton.addEventListener('click', () => search(state.page + 1, true));
moreTopButton.addEventListener('click', () => search(state.page + 1, true));
sortSelect.addEventListener('change', () => { if (state.words.length) search(); });
grid.addEventListener('click', async event => { const button = event.target.closest('[data-copy]'); if (!button) return; try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = '복사됨'; setTimeout(() => button.textContent = '복사', 1200); } catch { showMessage('클립보드에 복사하지 못했습니다.'); } });

// 페이지를 여는 동안 한방단어 검색용 '희귀 끝글자' 캐시를 서버에서 미리 데운다.
// 사용자가 단어를 입력하는 사이에 준비돼 첫 한방단어 검색이 빨라진다.
// 운영 서버는 프로세스가 여럿이라 캐시가 프로세스별이므로 몇 번 나눠 부른다.
const warmUp = () => fetch('/api/warm', {cache: 'no-store'}).catch(() => {});
warmUp();
[1500, 4000].forEach(delay => setTimeout(warmUp, delay));
