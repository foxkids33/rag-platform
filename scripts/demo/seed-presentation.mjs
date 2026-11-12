import { mkdir, readFile, writeFile } from 'node:fs/promises';

const corpus = JSON.parse(await readFile('.storage/presentation/corpus-manifest.json', 'utf8'));
const base = corpus.base_url;
const topics = [
  { key: 'ostor', name: '01 · Ostor — эксплуатация', kb: 'Ostor · эксплуатация и диагностика', pages: ['182289032','189233071','189233077','189210629'] },
  { key: 'tls', name: '02 · TLS — диагностика', kb: 'TLS · сертификаты и troubleshooting', pages: ['172547593','196293992','196293996','196294004'] },
  { key: 's3', name: '03 · S3 — архитектура', kb: 'S3 · архитектура и границы знаний', pages: ['189231226','196303813','196289650','185993444'] },
];
const cases = [
  { key:'env', topic:'ostor', title:'01 · ostor-agentd.env — назначение и источник', question:'Для чего нужен файл ostor-agentd.env и где он используется?', checks:[/ostor-agentd\.env/i], page:'182289032' },
  { key:'top-stat', topic:'ostor', title:'02 · vstorage top и stat — разница режимов', question:'Чем отличаются vstorage top и vstorage stat по способу отображения состояния?', checks:[/\btop\b/i,/\bstat\b/i], page:'182289032' },
  { key:'commands', topic:'ostor', title:'03 · Команды Ostor — дословный аудит', question:'Какими командами проверить состояние служб Ostor? Приведи полные исходные блоки дословно, не объединяй разные блоки.', checks:[/systemctl|service|vstorage/i], exactCode:true },
  { key:'unknown', topic:'ostor', title:'04 · BLUE-KB-942 — корректный отказ', question:'Как исправить ошибку BLUE-KB-942 в Ostor? Приведи подтверждённую инструкцию и команды из документации.', refusal:true },
  { key:'tls-termination', topic:'tls', title:'01 · TLS и mTLS — где завершается соединение', question:'Где в схеме соединения клиента S3 завершается TLS и что дополнительно проверяется при включённом mTLS?', checks:[/s3gateway-proxy-server/i,/сертификат.{0,50}клиент|клиент.{0,50}сертификат/is], page:'172547593' },
  { key:'tls-chain', topic:'tls', title:'02 · TLS — неполная цепочка сертификатов', question:'Почему ошибка unable to verify the first certificate возможна даже при установленном корневом УЦ? Что требуется проверить в цепочке сертификатов?', checks:[/промежуточн/i,/цепочк/i], page:'196293992' },
  { key:'round-robin', topic:'s3', title:'01 · Round-Robin — документированные ограничения', question:'Какие ограничения Round-Robin перечислены на странице схемы обработки S3-запросов?', checks:[/(?:на|за)грузк/i,/врем|длительн/i,/производител|мощност/i], page:'189231226' },
  { key:'compression', topic:'s3', title:'02 · Compression Server — условие и алгоритм', question:'При каком условии подключается Compression Server и какой алгоритм сжатия указан в схеме обработки S3-запросов?', checks:[/ZSTD/i,/сжати/i], page:'189231226' },
  { key:'node-id', topic:'s3', title:'03 · Shaman — источник NODE_ID', question:'Где хранится NODE_ID хоста Shaman?', checks:[/\/etc\/vstorage\/host_id/], page:'196289650' },
];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function request(endpoint, body, method = body === undefined ? 'GET' : 'POST') {
  const r = await fetch(base + '/api/v1' + endpoint, {
    method, headers:body === undefined ? undefined : {'Content-Type':'application/json'},
    body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(240000),
  });
  if (!r.ok) throw new Error(endpoint + ': HTTP ' + r.status);
  return r.status === 204 ? null : r.json();
}
const kbs = await request('/knowledge-bases');
const workspaces = await request('/workspaces');
for (const topic of topics) {
  const found = kbs.filter(kb => kb.name === topic.kb);
  if (found.length !== 1) throw new Error('Missing or ambiguous curated KB: ' + topic.kb);
  topic.kb_id = found[0].id;
  const deadline = Date.now() + 120000;
  let version;
  while (Date.now() < deadline) {
    const versions = await request('/knowledge-bases/' + topic.kb_id + '/versions');
    if (versions.length !== 1 || versions[0].version !== 1) throw new Error('Unexpected curated version');
    version = versions[0];
    if (['READY','ACTIVE'].includes(version.status)) break;
    if (version.failed_document_count) throw new Error('Curated KB indexing failed');
    await sleep(2000);
  }
  if (!version || !['READY','ACTIVE'].includes(version.status) || version.searchable_document_count !== 4) throw new Error('Curated version is not fully ready');
  const documents = await request('/knowledge-bases/' + topic.kb_id + '/versions/' + version.id + '/documents');
  if (documents.length !== 4 || documents.some(doc => doc.source_type !== 'CONFLUENCE' || doc.status !== 'READY' || !doc.search_enabled || !topic.pages.includes(doc.external_id))) throw new Error('Unexpected curated document provenance');
  topic.version_id = version.id;
  topic.page_versions = Object.fromEntries(documents.map(doc => [doc.external_id, doc.external_version]));
  if (version.status !== 'ACTIVE') await request('/knowledge-bases/' + topic.kb_id + '/versions/' + version.id + '/publish', {});
  const existing = workspaces.filter(ws => ws.name === topic.name);
  if (existing.length > 1) throw new Error('Ambiguous demo workspace');
  const ws = existing[0] || await request('/workspaces', {name:topic.name,base_knowledge_base_id:topic.kb_id,source_mode:'KNOWLEDGE_BASE'});
  if (ws.base_knowledge_base_id !== topic.kb_id || ws.source_mode !== 'KNOWLEDGE_BASE') throw new Error('Demo workspace uses an unexpected scope');
  topic.workspace_id = ws.id;
  console.log(JSON.stringify({phase:'published',topic:topic.key,workspace:ws.id,kb:topic.kb_id,documents:4}));
}
const results = [];
for (const item of cases) {
  const topic = topics.find(topic => topic.key === item.topic);
  const prefix = '/workspaces/' + topic.workspace_id;
  const history = await request(prefix + '/conversations');
  const old = history.filter(chat => chat.title === item.title);
  if (old.length > 1) throw new Error('Ambiguous prepared chat title');
  const conversation = old[0] || await request(prefix + '/conversations', {title:item.title});
  let answer;
  let latency;
  const detail = await request(prefix + '/conversations/' + conversation.id);
  const previous = detail.messages.filter(message => message.role === 'assistant').at(-1);
  if (previous) {
    answer = { ...previous.metadata, answer:previous.content };
    latency = null;
  } else {
    const start = Date.now();
    answer = await request(prefix + '/answer', {question:item.question,conversation_id:conversation.id,mode:'hybrid',rerank:false,retrieval_limit:20,source_limit:6,max_tokens:2048,temperature:0});
    latency = Date.now() - start;
  }
  // Preserve the human-readable prepared title even if auto-title logic runs.
  await request(prefix + '/conversations/' + conversation.id, {title:item.title}, 'PATCH');
  const cited = (answer.sources || []).filter(source => (answer.cited_source_indices || []).includes(source.index));
  const failures = [];
  if (!answer.citation_valid) failures.push('citation_contract');
  if (item.refusal) {
    if (!answer.abstained || answer.abstention_reason !== 'insufficient_retrieval_evidence' || answer.required_anchor_count !== 1 || answer.matched_anchor_count !== 0) failures.push('expected_anchor_refusal');
  } else {
    if (answer.abstained) failures.push('unexpected_refusal');
    if (!cited.length) failures.push('missing_cited_sources');
    if (item.page && !cited.some(source => source.source_page_id === item.page)) failures.push('expected_page_not_cited');
    if (item.exactCode && (answer.code_integrity?.status !== 'exact_match' || !answer.code_integrity.total_block_count)) failures.push('full_code_not_verified');
    for (const check of item.checks || []) if (!check.test(answer.answer || '')) failures.push('expected_fact_' + check.source);
  }
  const result = {
    key:item.key,topic:item.topic,title:item.title,question:item.question,
    workspace_id:topic.workspace_id,conversation_id:conversation.id,
    url:base + '/?workspace=' + topic.workspace_id + '&conversation=' + conversation.id + '#chat',
    verified:failures.length === 0,checks_failed:failures,latency_ms:latency,
    abstained:answer.abstained,abstention_reason:answer.abstention_reason,
    citation_valid:answer.citation_valid,
    code_status:answer.code_integrity?.status,code_blocks:answer.code_integrity?.total_block_count,
    cited_pages:cited.map(source=>({id:source.source_page_id,version:source.source_version})),
  };
  results.push(result);
  console.log(JSON.stringify({phase:'chat',key:item.key,verified:result.verified,failures,latency_ms:latency,code_status:result.code_status,blocks:result.code_blocks}));
}
const manifest = {generated_at:new Date().toISOString(),base_url:base,live_workspace_id:corpus.workspace_id,visible_spaces:corpus.visible_spaces,live_pages:corpus.page_count,topics,chats:results,reviewer:{live_verified:false,historical_url:'http://127.0.0.1:23990/confluence',safe_test_page_id:'5636125',saved_result:'assets/reviewer-saved-result.png'}};
await mkdir('.storage/presentation', {recursive:true});
await writeFile('.storage/presentation/demo-manifest.json', JSON.stringify(manifest,null,2) + '\n', {mode:0o600});
await writeFile('apps/web/public/presentation/demo-manifest.json', JSON.stringify(manifest,null,2) + '\n');
console.log(JSON.stringify({phase:'complete',verified:results.filter(result=>result.verified).length,total:results.length,manifest:'.storage/presentation/demo-manifest.json'}));
if (results.some(result => !result.verified)) process.exitCode = 2;
