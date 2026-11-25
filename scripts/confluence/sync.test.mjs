import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseEnv, safeURL, inventory, fingerprint, indexSummary, run } from './sync.mjs';

test('dotenv is data, not shell; quoted values and comments', () => {
  const config = parseEnv('TOKEN="literal$(never-execute)"\nSPACE=KBR # comment\nexport NAME=bot\n');
  assert.equal(config.TOKEN, 'literal$(never-execute)');
  assert.equal(config.SPACE, 'KBR');
  assert.equal(config.NAME, 'bot');
});
test('credentials never follow pagination off-origin or outside REST API', () => {
  const base = 'https://wiki.example.test/confluence';
  assert.equal(safeURL('/confluence/rest/api/content?start=100', base, '/confluence/rest/api/').hostname, 'wiki.example.test');
  for (const value of ['https://evil.test/confluence/rest/api/', '/login', 'https://user:pass@wiki.example.test/confluence/rest/api/', '/confluence/rest/api/../../login']) {
    assert.throws(() => safeURL(value, base, '/confluence/rest/api/'));
  }
});
test('inventory follows all pages even when the server returns a short batch', async () => {
  const get = async url => url === '/one'
    ? { results: [{ id: '1', version: { number: 1 } }], _links: { next: '/two' } }
    : { results: [{ id: '2', version: { number: 1 } }] };
  assert.equal((await inventory(get, '/one')).length, 2);
});
test('partial or unstable catalogs cannot become successful syncs', async () => {
  await assert.rejects(inventory(async () => ({ results: [], _links: { next: '/one' } }), '/one'));
  await assert.rejects(inventory(async () => ({ results: [{ id: '1', version: { number: 1 } }], _links: { next: '/one' } }), '/one'));
  await assert.rejects(inventory(async () => ({ results: [{ id: '../secret', version: { number: 1 } }] }), '/one'));
});
test('fingerprint changes on title, version, labels or ancestor changes', () => {
  const source = { id: '1', title: 'A', version: { number: 1 } };
  for (const changes of [{ title: 'B' }, { version: { number: 2 } }, { ancestors: [{ id: '2', title: 'Moved' }] }, { metadata: { labels: { results: [{ name: 'archived' }] } } }]) {
    assert.notEqual(fingerprint(source), fingerprint({ ...source, ...changes }));
  }
});

test('READY old versions, legacy formats and duplicates cannot pass index reconciliation', () => {
  const page = { id: '1', version: { number: 2 } };
  const doc = { source_type: 'CONFLUENCE', external_id: '1', external_version: '2', filename: 'page.confluence.json', status: 'READY', search_enabled: true };
  assert.equal(indexSummary([doc], [page]).ready, 1);
  assert.equal(indexSummary([{ ...doc, external_version: '1' }], [page]).ready, 0);
  assert.equal(indexSummary([{ ...doc, filename: 'page.html' }], [page]).ready, 0);
  assert.equal(indexSummary([{ ...doc, search_enabled: false }], [page]).ready, 0);
  assert.deepEqual(indexSummary([doc, doc], [page]).duplicates, ['1']);
  assert.deepEqual(indexSummary([], [page]).missing, ['1']);
});

test('one-shot adapter uses real API routes, reuses workspace and does not reimport unchanged pages', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'kbr-sync-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const env = path.join(root, 'test.env');
  await fs.writeFile(env, 'CONFLUENCE_BASE_URL=https://wiki.test\nCONFLUENCE_DEPLOYMENT=data_center\nCONFLUENCE_AUTH_MODE=basic\nCONFLUENCE_EMAIL=bot\nCONFLUENCE_API_TOKEN=test-only\n');
  let created = 0, imported = 0;
  let documents = [];
  const page = { id: '1', title: 'T', version: { number: 1 }, space: { key: 'KBR' }, body: { storage: { value: '<p>Text</p>' } } };
  t.mock.method(globalThis, 'fetch', async (target, options) => {
    const url = new URL(target);
    let result;
    if (url.hostname === 'wiki.test') {
      if (url.pathname === '/rest/api/content') result = { results: [page] };
      else if (url.pathname === '/rest/api/content/1') result = page;
      else if (url.pathname.endsWith('/child/attachment')) result = { results: [] };
      else assert.fail('Unexpected Confluence route');
    } else if (url.pathname.endsWith('/integrations/confluence/status')) {
      result = { enabled: true, can_import: true, host: 'wiki.test', parser_format: 'confluence_storage_v1' };
    } else if (url.pathname === '/api/v1/workspaces' && options.method === 'POST') {
      created++; result = { id: 'workspace' };
    } else if (url.pathname === '/api/v1/workspaces/workspace/documents') result = documents;
    else if (url.pathname.endsWith('/imports/confluence')) {
      imported++;
      assert.deepEqual(JSON.parse(options.body).expected_versions, { '1': '1' });
      documents = [{ id: 'doc', external_id: '1', external_version: '1', filename: 'T-1.confluence.json', source_type: 'CONFLUENCE', status: 'READY', search_enabled: true }];
      result = { results: [{ page_id: '1', status: 'queued' }], queued: 1, failed: 0 };
    } else assert.fail(`Unexpected API route ${url.pathname}`);
    return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  const options = { env, space: 'KBR', output: path.join(root, 'snapshot'), 'api-url': 'http://127.0.0.1:8000', 'create-workspace': 'KBR pilot', 'watch-seconds': '0', 'wait-seconds': '0' };
  await run(options);
  await run(options);
  const report = JSON.parse(await fs.readFile(path.join(options.output, 'latest.json'), 'utf8'));
  assert.equal(report.index_complete, true);
  assert.equal(created, 1);
  assert.equal(imported, 1);
});
