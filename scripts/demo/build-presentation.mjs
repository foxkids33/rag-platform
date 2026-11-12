import { mkdir, readFile, writeFile } from 'node:fs/promises';

const directory = 'apps/web/public/presentation';
const manifest = JSON.parse(await readFile('.storage/presentation/demo-manifest.json', 'utf8'));
try {
  const status = JSON.parse(await readFile('.storage/presentation/reviewer-status.json', 'utf8'));
  manifest.reviewer = { ...manifest.reviewer, ...status };
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
try {
  const ui = JSON.parse(await readFile('.storage/presentation/reviewer-ui-check.json', 'utf8'));
  manifest.reviewer = { ...manifest.reviewer, ...ui };
  if (ui.editor_review_verified) {
    manifest.reviewer.live_verified = true;
    manifest.reviewer.local_editor_verified = true;
    manifest.reviewer.safe_notes = ['Local native editor review, minimal Apply and full native Undo were verified on the own fixture; no publication.', 'Remote entry requires a separate proxy/editor check.'];
    delete manifest.reviewer.current_model_calls;
    delete manifest.reviewer.recorder_status;
  }
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
try {
  const relay = JSON.parse(await readFile('.storage/presentation/reviewer-relay-status.json', 'utf8'));
  const fields = ['remote_url','remote_editor_url','gateway_verified','remote_editor_verified',
    'remote_review_verified','remote_apply_minimal_verified','remote_undo_full_html_verified',
    'published_version_unchanged','remote_review_request_ms','tls_pinned_fingerprint',
    'tls_validation_disabled','external_tunnel_provider','remote_ui_checked_at'];
  for (const field of fields) if (field in relay) manifest.reviewer[field] = relay[field];
  if (relay.remote_review_verified) {
    manifest.reviewer.saved_result = relay.saved_result;
    manifest.reviewer.safe_notes = ['Native review, minimal Apply, full Undo and unchanged published version verified through the internal HTTPS relay on the own fixture.', 'The application remains on the laptop; the laptop and reverse SSH must remain available.'];
  }
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
if (manifest.chats.length !== 9 || manifest.chats.some(chat => !chat.verified)) {
  throw new Error('Presentation requires all nine saved cases to pass the case-specific checks');
}
const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const topic = key => manifest.topics.find(item => item.key === key);
const chat = key => manifest.chats.find(item => item.key === key);
const base = manifest.base_url;
const acceptance = JSON.parse(await readFile(`${directory}/acceptance-summary.json`, 'utf8'));
const workspaceUrl = (id, view = 'chat') => `${base}/?workspace=${id}#${view}`;
const replacements = {
  WORKSPACE_LIVE_CONFLUENCE: '00 · Confluence · живой KBR',
  WORKSPACE_OPERATIONS: topic('ostor').name,
  WORKSPACE_STORAGE: topic('s3').name,
  KNOWLEDGE_BASE_OPERATIONS: topic('ostor').kb,
  KNOWLEDGE_BASE_STORAGE: topic('s3').kb,
  CHAT_ENV: chat('env').title,
  CHAT_STORAGE: chat('round-robin').title,
  CHAT_OSTOR_COMMANDS: chat('commands').title,
  CHAT_UNKNOWN_ID: chat('unknown').title,
  REVIEWER_URL: manifest.reviewer.remote_editor_verified ? manifest.reviewer.remote_url : 'http://127.0.0.1:23990/confluence',
  REVIEWER_TEST_PAGE: 'собственная тестовая страница 5636125',
};
// Mechanical marker resolution; generated HTML and the source script use the same real names.
const scriptPath = 'docs/presentation/2026-10-09-speaker-script.md';
let script = await readFile(scriptPath, 'utf8');
for (const [key, value] of Object.entries(replacements)) script = script.replaceAll(`{{${key}}}`, value);
if (/\{\{[A-Z_]+\}\}/.test(script)) throw new Error('Unresolved speaker marker');
await writeFile(scriptPath, script);

function inline(value) {
  return escape(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|[a-zA-Z0-9-]+\.md)\)/g, (_match, label, url) => {
      const documents = {'2026-10-09-speaker-script.md':'speaker-script.html',
        '2026-10-09-slide-content.md':'slide-content.html','2026-10-09-demo-runbook.md':'demo-runbook.html'};
      const resolved = documents[url] || url;
      return `<a href="${resolved}">${label}</a>`;
    });
}
function markdown(value) {
  const lines = value.split('\n');
  const parts = [];
  let paragraph = [], list = null, code = null, table = [];
  const flushParagraph = () => { if (paragraph.length) parts.push('<p>' + inline(paragraph.join(' ')) + '</p>'); paragraph = []; };
  const flushList = () => { if (list) parts.push(list.close); list = null; };
  const flushTable = () => {
    if (!table.length) return;
    parts.push('<div class="table-wrap"><table>' + table.filter(line => !/^\|[\s|:-]+\|$/.test(line)).map((line, index) => '<tr>' + line.slice(1,-1).split('|').map(cell => `<${index ? 'td' : 'th'}>${inline(cell.trim())}</${index ? 'td' : 'th'}>`).join('') + '</tr>').join('') + '</table></div>');
    table = [];
  };
  for (const line of lines) {
    if (line.startsWith('```')) {
      flushParagraph(); flushList(); flushTable();
      if (code === null) code = [];
      else { parts.push('<pre><code>' + escape(code.join('\n')) + '</code></pre>'); code = null; }
    } else if (code !== null) code.push(line);
    else if (line.startsWith('|')) { flushParagraph(); flushList(); table.push(line); }
    else {
      flushTable();
      if (!line.trim()) { flushParagraph(); flushList(); }
      else if (/^#{1,4} /.test(line)) {
        flushParagraph(); flushList(); const level = line.match(/^#+/)[0].length;
        parts.push(`<h${level}>${inline(line.slice(level + 1))}</h${level}>`);
      } else if (/^(?:- |\d+\. )/.test(line)) {
        flushParagraph(); const kind = line.startsWith('- ') ? 'ul' : 'ol';
        if (list?.kind !== kind) { flushList(); parts.push(`<${kind}>`); list = {kind,close:`</${kind}>`}; }
        parts.push('<li>' + inline(line.replace(/^(?:- |\d+\. )/, '')) + '</li>');
      } else { flushList(); paragraph.push(line.trim()); }
    }
  }
  flushParagraph(); flushList(); flushTable();
  return parts.join('\n');
}
const style = `*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#1c2c43;font:16px/1.65 Inter,"Segoe UI",Arial,sans-serif}a{color:#505ee8}header{background:#fff;border-bottom:1px solid #dfe6ee;padding:22px max(24px,calc((100vw - 1160px)/2));display:flex;align-items:center;justify-content:space-between;gap:22px;flex-wrap:wrap}header strong{font-size:22px;letter-spacing:-.5px}nav{display:flex;gap:18px;flex-wrap:wrap}nav a{text-decoration:none;font-size:14px}main{max-width:1160px;margin:auto;padding:35px 24px 70px}h1{font-size:36px;line-height:1.2;letter-spacing:-1px}h2{margin:34px 0 12px;font-size:25px;line-height:1.35}h3{font-size:20px;margin:0 0 8px}.lead,.muted{color:#60738b}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:23px 0}.pill{padding:7px 13px;background:#e9edfc;border-radius:9px;font-size:14px;color:#4355a2}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.card{background:#fff;border:1px solid #dfe6ee;border-radius:16px;padding:22px}.card p{margin:8px 0;color:#60738b}.action{display:inline-block;padding:9px 14px;background:#505ee8;color:#fff;border-radius:8px;border:0;text-decoration:none;font:inherit;cursor:pointer}.secondary{background:#edf0fb;color:#4050a8}button:hover,a.action:hover{filter:brightness(.96)}.links{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 0}.question{background:#fff;border:1px solid #dfe6ee;border-radius:13px;margin:12px 0;padding:20px;display:grid;grid-template-columns:1fr auto;gap:16px;align-items:center}.question strong{display:block;margin-bottom:7px}.question p{margin:0;white-space:pre-wrap}.question small{display:block;color:#60738b;margin-top:8px}details{margin:14px 0}summary{cursor:pointer;font-weight:650}img{max-width:100%;height:auto;border:1px solid #dfe6ee;border-radius:10px}figure{margin:22px 0}figcaption{color:#60738b;font-size:14px;margin:8px 0}.notice{padding:17px 20px;border-left:3px solid #505ee8;background:#edf0fb;border-radius:0 9px 9px 0;margin:20px 0}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}td,th{border:1px solid #dfe6ee;padding:11px;text-align:left;vertical-align:top}code{font:14px/1.6 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;background:#edf0f6;padding:2px 4px;border-radius:3px}pre{white-space:pre-wrap;background:#edf0f6;border-radius:10px;padding:18px;break-inside:avoid}pre code{padding:0}article{max-width:860px;margin:auto}article h2{border-top:1px solid #dfe6ee;padding-top:25px}article li{margin:7px 0}footer{margin-top:35px;color:#60738b;font-size:14px}.copy-status{min-height:24px;color:#18715e;font-size:14px}.timeline{line-height:2}.download{display:flex;gap:14px;flex-wrap:wrap}.saved-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:800px){.grid,.saved-grid{grid-template-columns:1fr}.question{grid-template-columns:1fr}h1{font-size:29px}header{padding:16px 24px}.question .links{margin:0}}@media print{@page{size:A4;margin:16mm}body{background:white;font-size:11pt}header,.no-print{display:none}main{padding:0;max-width:none}h1{font-size:22pt}h2{font-size:16pt;break-after:avoid}h3{break-after:avoid}a{color:inherit;text-decoration:none}.card,.question{break-inside:avoid}p,li{orphans:3;widows:3}}`;
const shell = (title, content) => `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(title)}</title><style>${style}</style></head><body><header><strong>Скала^р · пульт выступления</strong><nav><a href="index.html">5 слайдов</a><a href="demo.html">Демонстрация</a><a href="speaker-script.html">Текст ведущего</a><a href="slides.pdf">PDF слайдов</a></nav></header><main>${content}</main></body></html>`;
await mkdir(directory, {recursive:true});
await writeFile(`${directory}/speaker-script.html`, shell('Текст выступления · 9 октября', `<article>${markdown(script)}</article>`));
await writeFile(`${directory}/slide-content.html`, shell('Текст пяти слайдов', `<article>${markdown(await readFile('docs/presentation/2026-10-09-slide-content.md','utf8'))}</article>`));
await writeFile(`${directory}/demo-manifest.json`, JSON.stringify(manifest,null,2) + '\n');
await writeFile(`${directory}/acceptance.html`, shell('Что действительно проверено', `<article><h1>Проверка подготовленных сценариев</h1><p>Это приёмка демонстрации, не независимая оценка общей точности.</p><table><tr><th>Проверка</th><th>Наблюдаемый результат</th></tr><tr><td>Живой импорт KBR</td><td>309 страниц READY</td></tr><tr><td>Сверка сохранённых ответов с цитируемыми фрагментами</td><td>9 подготовленных случаев проверены отдельно</td></tr><tr><td>Дословность полного fenced-кода</td><td>4 из 4 блоков в подготовленном ответе Ostor</td></tr><tr><td>Отсутствующий идентификатор</td><td>BLUE-KB-942: 0 из 1 обязательных якорей; отказ без придуманной инструкции</td></tr>${Object.entries(acceptance.modes).map(([mode,result])=>`<tr><td>${escape(mode)} · ожидаемая страница среди первых 5 чанков</td><td>${result.expected_page_hits_at_5}/${result.positive_case_count}</td></tr>`).join('')}<tr><td>Регрессионные тесты</td><td>166 целевых API-тестов + 32 теста connector прошли</td></tr><tr><td>Reviewer: собственный черновик</td><td>Проверка ~72 с; одна минимальная замена; Ctrl+Z восстановил HTML; 3 таблицы и 2 макроса сохранены</td></tr></table><h2>Границы этих чисел</h2><p>Поисковая проверка: 8 заранее подготовленных положительных вопросов, каждый ищет только в 4 документах своей KB; всего 27 запросов, без LLM. Hit@5 означает хотя бы одну ожидаемую страницу среди первых пяти чанков, не полноту инструкции и не Recall по всем релевантным страницам.</p><p>Это не holdout, не SLA, не доказательство преимущества над Just AI. Дословный код не гарантирует безопасность или применимость. Изображения и схемы сейчас отмечаются как неизвлечённые, OCR/vision не заявлены.</p><p><a href="acceptance-summary.json">Машиночитаемый отчёт поискового прогона</a></p></article>`));
const questionCard = key => {
  const item = chat(key);
  return `<div class="question"><div><strong>${escape(item.title)}</strong><p>${escape(item.question)}</p><small>Реальный сохранённый ответ · ${item.code_status === 'exact_match' ? item.code_blocks + ' полных блока подтверждены дословно' : item.abstained ? 'Проверенный отказ: точный идентификатор найден 0 из 1' : 'Сверен с процитированными фрагментами'} · не benchmark</small></div><div class="links"><button class="action" data-copy="${escape(key)}">Скопировать вопрос</button><a class="action secondary" href="${escape(item.url)}" target="_blank" rel="noopener">Сохранённый ответ ↗</a></div></div>`;
};
const content = `<h1>22 минуты показа.<br>Три технических акцента.</h1><p class="lead">Прозрачный поиск → дословный контроль команд → отказ без основания.<br>Кнопка «Сохранённый ответ» открывает историю, а не запускает новый запрос.</p>
<div class="stats"><span class="pill">${manifest.visible_spaces} доступных пространств</span><span class="pill">${manifest.live_pages} страниц KBR импортированы</span><span class="pill">3 активные KB · по 4 статьи</span><span class="pill">9 отрепетированных чатов</span></div>
<div class="download"><a class="action" href="index.html#slide-1" target="_blank">Открыть слайды</a><a class="action secondary" href="speaker-script.html" target="_blank">Полный текст и действия</a><a class="action secondary" href="slides.pdf">Скачать слайды PDF</a><a class="action secondary" href="speaker-script.pdf">Текст ведущего PDF</a><a href="../presentation-offline.zip">Весь комплект ZIP</a><a href="demo-runbook.html">Короткая памятка</a></div>
<h2>Подготовленные рабочие области</h2><div class="grid">${manifest.topics.map(item=>`<section class="card"><h3>${escape(item.name)}</h3><p>${escape(item.kb)}</p><p>Активная v1 · 4 реальные статьи<br>Исходные версии Confluence сохранены.</p><div class="links"><a class="action" href="${workspaceUrl(item.workspace_id)}" target="_blank" rel="noopener">Открыть область ↗</a></div></section>`).join('')}</div>
<div class="links"><a class="action secondary" href="${workspaceUrl(manifest.live_workspace_id,'confluence')}" target="_blank" rel="noopener">Живой Confluence и импорт ↗</a><a class="action secondary" href="${workspaceUrl(topic('ostor').workspace_id,'retrieval')}" target="_blank" rel="noopener">Разбор поиска ↗</a><a class="action secondary" href="${base}/#knowledge" target="_blank" rel="noopener">Менеджер баз знаний ↗</a></div>
<div class="notice">Для live-вопроса нажать «+ Новый» в разделе «Диалоги» нужной области. Вопросы ниже копируются без отправки. Команды не выполнять; опубликованные статьи не менять.</div>
<h2>Основной живой показ</h2>${['env','commands','unknown'].map(questionCard).join('')}<p id="copy-status" class="copy-status" role="status" aria-live="polite"></p>
<details><summary>Ещё шесть реальных вопросов — резерв и репетиция</summary>${['top-stat','tls-termination','tls-chain','round-robin','compression','node-id'].map(questionCard).join('')}</details>
<h2>Confluence AI Reviewer</h2><section class="card"><h3>Замороженная сборка · внутренний HTTPS-вход</h3><p>${manifest.reviewer.server_ready ? 'Сервер восстановлен; привязка к замороженному runtime проверена.' : 'Перед показом проверить запуск локального стенда.'} ${manifest.reviewer.remote_review_verified ? 'Через корпоративный сервер проверены вход, review, минимальная правка и полный Ctrl+Z; опубликованная версия не изменилась.' : 'Удалённый редактор и живую проверку ещё нужно подтвердить; готовность gateway не доказывает работу review.'}</p><div class="links">${manifest.reviewer.remote_editor_verified ? `<a class="action" href="${escape(manifest.reviewer.remote_editor_url)}" target="_blank" rel="noopener">Редактор через корпоративный сервер ↗</a>` : ''}<a class="action secondary" href="${escape(manifest.reviewer.saved_result)}" target="_blank">Сохранённый результат ↗</a></div><p class="muted">Приложение остаётся на ноутбуке: он должен быть включён и подключён, SSH-туннель — активен. HTTPS-сертификат self-signed: заранее сверить отпечаток, затем войти с отдельным gateway-паролем и в Confluence. Пароли в пульте не хранятся. «Применить» / Ctrl+Z только в своём черновике, без публикации; без ngrok.</p></section>
<details><summary>Сохранённый результат Reviewer · резерв, не live</summary><figure><img src="${escape(manifest.reviewer.saved_result)}" alt="Сохранённая проверка собственной тестовой страницы в нативном редакторе Confluence"><figcaption>Проверка собственного тестового черновика, сохранённая 8 октября 2026. Это скриншот, не текущий запрос.</figcaption></figure></details>
<details><summary>Резерв RAG · снимки подготовленного стенда, не live</summary><div class="saved-grid">${['rag-code-audit','rag-search'].map(name=>`<figure><a href="assets/${name}.png" target="_blank"><img src="assets/${name}.png" alt="Сохранённый экран ${name}" loading="lazy"></a><figcaption>Сохранённый экран · не новый ответ</figcaption></figure>`).join('')}</div></details>
<h2>Порядок переключений</h2><div class="timeline">0:00 — слайд 1 · задача инженера<br>1:30 — слайд 2 → запустить Reviewer<br>2:15 — Confluence, корпус, тематические KB<br>5:15 — Reviewer: замечание → Apply → Undo<br>8:30 — слайд 3 → dense / lexical / hybrid<br>11:30 — живой вопрос про ostor-agentd.env → источник<br>14:30 — слайд 4 → полные блоки Ostor и audit<br>17:30 — BLUE-KB-942 → отказ<br>18:45 — слайд 5 → пределы и измеримый пилот<br>22:00–25:00 — вопросы</div>
<div class="notice">Все пространства, доступные аккаунту connector, можно просматривать; не все их страницы индексированы. Стенд внутренний и без авторизации: индивидуальные ACL Confluence не перенесены. Reviewer ↔ RAG — направление развития, не готовая автоматическая интеграция.</div>
<p><a href="acceptance.html">Реальные проверки и границы метрик ↗</a> · 8 подготовленных положительных поисковых случаев, не независимый benchmark.</p>
<footer>Подготовлено 8 октября для выступления 9 октября 2026. Слайды, текст и сохранённый Reviewer-скриншот открываются локально без внешних ресурсов. Живой продукт и история чатов требуют сеть. Just AI — только при сопоставимых измерениях.</footer>`;
const questions = JSON.stringify(Object.fromEntries(manifest.chats.map(item=>[item.key,item.question]))).replace(/</g,'\\u003c');
const copyScript = `<script>const questions=${questions};document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{const value=questions[button.dataset.copy];let copied=false;try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(value);copied=true;}}catch{}if(!copied){const area=document.createElement('textarea');area.value=value;area.style.position='fixed';area.style.top='0';area.style.left='0';area.style.opacity='0';document.body.append(area);area.select();try{copied=document.execCommand('copy');}catch{}area.remove();}document.querySelector('#copy-status').textContent=copied?'Вопрос скопирован. Переключись в нужную область и создай новый чат.':'Не удалось скопировать автоматически: выдели текст вопроса вручную.';}));</script>`;
await writeFile(`${directory}/demo.html`, shell('Пульт живой демонстрации', content).replace('</body>',copyScript+'</body>'));
try {
  const runbook = await readFile('docs/presentation/2026-10-09-demo-runbook.md','utf8');
  await writeFile(`${directory}/demo-runbook.html`, shell('Короткая памятка ведущего', `<article>${markdown(runbook)}</article>`));
} catch (error) { if (error.code !== 'ENOENT') throw error; }
console.log(JSON.stringify({slides:5,chats:manifest.chats.length,reviewer_server_ready:Boolean(manifest.reviewer.server_ready),output:directory}));
