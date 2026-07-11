const form = document.querySelector('#search-form');
const queryInput = document.querySelector('#query');
const message = document.querySelector('#message');
const loading = document.querySelector('#loading');
const results = document.querySelector('#results');
const grid = document.querySelector('#word-grid');
const moreButton = document.querySelector('#more-button');
const sortSelect = document.querySelector('#sort-select');
let state = { page: 1, words: [], hasMore: false, params: null, recentKeys: new Set(), prefetch: null };

const setHidden = (element, hidden) => { element.hidden = hidden; };
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const wordKey = word => [word.word, word.dictionary, word.detail_url || '', word.definition].join('\u001f');

function showMessage(text, kind = 'error') {
  message.textContent = text;
  message.style.borderColor = kind === 'notice' ? '#176b45' : '#a83b37';
  setHidden(message, false);
}

function buildParams(page = 1) {
  const data = new FormData(form);
  const params = new URLSearchParams({query: data.get('query').trim(), dictionary: data.get('dictionary'), mode: data.get('mode'), sort: sortSelect.value, page});
  ['noun_only','include_proper','include_north','include_dialect','include_old','include_technical','include_single','dueum'].forEach(name => params.set(name, data.has(name)));
  return params;
}

function card(word) {
  const details = word.detail_url ? `<a href="${escapeHtml(word.detail_url)}" target="_blank" rel="noopener">사전에서 검색하기 ↗</a>` : '<span>검색 링크 없음</span>';
  const isNew = state.recentKeys.has(wordKey(word));
  return `<article class="word-card ${word.is_one_shot ? 'one-shot' : ''}"${isNew ? ' data-new-result="true"' : ''}>
    <div class="card-top"><h3>${escapeHtml(word.word)}</h3>${word.is_one_shot ? '<span class="badge">한방단어</span>' : ''}</div>
    <p class="pos">${escapeHtml(word.part_of_speech)} · ${escapeHtml(word.dictionary)}</p>
    <p class="definition">${escapeHtml(word.definition)}</p>
    <div class="stats"><span>마지막 글자 <strong>${escapeHtml(word.last_syllable)}</strong></span><span>이어갈 단어 <strong>${word.next_word_count}개</strong></span></div>
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
  document.querySelector('#result-meta').textContent = `한방단어 ${data.one_shot_count}개 · 기준 사전: ${data.dictionary_name} · 분석 ${data.analysed_count}개`;
  setHidden(results, false);
  setHidden(moreButton, !state.hasMore);
}

function scrollToNewResults() {
  const firstNewResult = grid.querySelector('[data-new-result="true"]');
  if (!firstNewResult) return;
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  requestAnimationFrame(() => firstNewResult.scrollIntoView({behavior, block: 'start'}));
}

async function requestSearch(params) {
  const response = await fetch(`/api/search?${params}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '검색 중 오류가 발생했습니다.');
  return data;
}

function prefetchNextPage() {
  if (!state.hasMore || !state.params) return;
  const params = new URLSearchParams(state.params);
  params.set('page', state.page + 1);
  const key = params.toString();
  state.prefetch = {
    key,
    promise: requestSearch(params)
      .then(data => ({data}))
      .catch(error => ({error})),
  };
}

async function search(page = 1, append = false) {
  const params = append ? new URLSearchParams(state.params) : buildParams(page);
  params.set('page', page);
  const query = params.get('query');
  if (!/^[가-힣]{1,20}$/.test(query)) { showMessage(query ? '완성된 한글을 20자 이하로 입력해 주세요.' : '검색할 한글 글자나 단어를 입력해 주세요.'); queryInput.focus(); return; }
  setHidden(message, true); setHidden(loading, false); if (!append) setHidden(results, true);
  moreButton.disabled = true;
  try {
    const key = params.toString();
    const cached = append && state.prefetch?.key === key ? await state.prefetch.promise : null;
    if (cached?.error) throw cached.error;
    const data = cached?.data || await requestSearch(params);
    state = {page, words: append ? [...state.words, ...data.words] : data.words, hasMore: data.has_more, params: key, recentKeys: append ? new Set(data.words.map(wordKey)) : new Set(), prefetch: null};
    render(data);
    if (append) scrollToNewResults();
    if (!data.words.length) showMessage('조건에 맞는 단어를 찾지 못했습니다. 필터를 바꿔 보세요.', 'notice');
    else if (data.warnings?.length) showMessage(`일부 결과 안내: ${data.warnings.join(' ')}`, 'notice');
    prefetchNextPage();
  } catch (error) { showMessage(error.message); }
  finally { setHidden(loading, true); moreButton.disabled = false; }
}

form.addEventListener('submit', event => { event.preventDefault(); search(); });
form.addEventListener('reset', () => setTimeout(() => { queryInput.value = ''; state = {page:1, words:[], hasMore:false, params:null, recentKeys:new Set(), prefetch:null}; setHidden(results,true); setHidden(message,true); }, 0));
document.querySelector('#clear-query').addEventListener('click', () => { queryInput.value = ''; queryInput.focus(); });
moreButton.addEventListener('click', () => search(state.page + 1, true));
sortSelect.addEventListener('change', () => { if (state.words.length) search(); });
grid.addEventListener('click', async event => { const button = event.target.closest('[data-copy]'); if (!button) return; try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = '복사됨'; setTimeout(() => button.textContent = '복사', 1200); } catch { showMessage('클립보드에 복사하지 못했습니다.'); } });
