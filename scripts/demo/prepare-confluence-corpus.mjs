import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = (process.env.DEMO_RAG_URL || 'http://192.0.2.122:15173').replace(/\/$/, '');
const name = '00 · Confluence · живой KBR';
const priority = new Set([
  '182289032', '189233071', '189233077', '189210629',
  '172547974', '172547593', '196293992', '196293996', '196294004',
  '189231226', '196303813', '196289650', '185993444',
]);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function request(endpoint, body) {
  const response = await fetch(`${base}/api/v1${endpoint}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(45000),
  });
  if (!response.ok) throw new Error(`${endpoint}: HTTP ${response.status}`);
  return response.json();
}
async function operation(workspaceId, kind, body = {}) {
  const prefix = `/workspaces/${workspaceId}/integrations/confluence`;
  const created = await request(`${prefix}/${kind}`, body);
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    const state = await request(`${prefix}/operations/${created.operation_id}`);
    if (state.status === 'SUCCEEDED') return state.result;
    if (['FAILED', 'CANCELLED'].includes(state.status)) {
      throw new Error(`${kind} operation ${created.operation_id}: ${state.status}`);
    }
    await sleep(700);
  }
  throw new Error(`${kind} operation timed out`);
}

const integration = await request('/integrations/confluence/connector');
const online = integration.connectors.filter(item => item.online);
if (!online.length) throw new Error('Confluence connector is offline');
const existing = (await request('/workspaces')).filter(item => item.name === name);
if (existing.length > 1) throw new Error('Ambiguous live workspace name');
const workspace = existing[0] || await request('/workspaces', { name, source_mode: 'USER_DOCUMENTS' });
if (workspace.source_mode !== 'USER_DOCUMENTS') throw new Error('Live workspace must use its own documents');
console.log(JSON.stringify({ phase: 'workspace', id: workspace.id, name }));
const spaces = await operation(workspace.id, 'spaces');
console.log(JSON.stringify({ phase: 'spaces', visible: spaces.spaces.length }));
if (!spaces.spaces.some(item => item.key === 'KBR')) throw new Error('KBR not visible');
const pages = new Map();
let start = 0;
while (start !== null) {
  const listing = await operation(workspace.id, 'space-pages', { space_key: 'KBR', start });
  const previous = pages.size;
  for (const page of listing.pages) pages.set(page.id, page);
  if (listing.next_start !== null && (
    !Number.isSafeInteger(listing.next_start) || listing.next_start <= start || pages.size === previous
  )) throw new Error('Space pagination stopped making progress');
  start = listing.next_start;
  console.log(JSON.stringify({ phase: 'inventory', count: pages.size, next: start }));
}
const missing = [...priority].filter(id => !pages.has(id));
if (missing.length) console.log(JSON.stringify({ phase: 'curation_notice', missing_current_kbr_pages: missing }));
const ordered = [...pages.values()].sort((a, b) => Number(priority.has(b.id)) - Number(priority.has(a.id)));
const totals = { requested: 0, queued: 0, skipped: 0, failed: 0 };
for (let offset = 0; offset < ordered.length; offset += 10) {
  const batch = ordered.slice(offset, offset + 10);
  const result = await operation(workspace.id, 'imports', {
    page_ids: batch.map(page => page.id),
    page_spaces: Object.fromEntries(batch.map(page => [page.id, page.space_key])),
    expected_versions: Object.fromEntries(batch.filter(page => page.version !== 'unknown').map(page => [page.id, page.version])),
    force: false,
  });
  for (const key of Object.keys(totals)) totals[key] += result[key] || 0;
  console.log(JSON.stringify({ phase: 'import', transferred: Math.min(offset + batch.length, ordered.length), total: ordered.length, ...totals }));
  if (result.failed || result.results.some(item => ['failed', 'busy'].includes(item.status))) {
    throw new Error('Some pages failed or were busy; rerun safely without force');
  }
}
const deadline = Date.now() + 900000;
let documents;
while (Date.now() < deadline) {
  documents = await request(`/workspaces/${workspace.id}/documents`);
  const selected = documents.filter(item => item.source_type === 'CONFLUENCE' && pages.has(item.external_id));
  const counts = selected.reduce((result, item) => ({ ...result, [item.status]: (result[item.status] || 0) + 1 }), {});
  console.log(JSON.stringify({ phase: 'indexing', ...counts, expected: pages.size }));
  if (counts.FAILED) throw new Error('Some imported documents failed indexing');
  if (counts.READY === pages.size && selected.every(item => item.search_enabled)) break;
  await sleep(4000);
}
if (!documents || documents.filter(item => item.status === 'READY' && pages.has(item.external_id)).length !== pages.size) {
  throw new Error('Indexing did not finish in the allotted time');
}
const manifest = {
  generated_at: new Date().toISOString(), base_url: base, workspace_id: workspace.id,
  workspace_name: name, visible_spaces: spaces.spaces.length, page_count: pages.size,
  source_space: 'KBR', import_result: totals,
  missing_current_kbr_pages: missing,
  curated_pages: documents.filter(item => priority.has(item.external_id)).map(item => ({
    document_id: item.id, page_id: item.external_id, version: item.external_version,
    title: item.title, source_url: item.source_url, synced_at: item.synced_at,
  })),
};
await mkdir('.storage/presentation', { recursive: true });
await writeFile(path.resolve('.storage/presentation/corpus-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
console.log(JSON.stringify({ phase: 'ready', workspace_id: workspace.id, documents: pages.size, report: '.storage/presentation/corpus-manifest.json' }));
