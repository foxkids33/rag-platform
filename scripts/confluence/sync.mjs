#!/usr/bin/env node
// Operational adapter for the existing API + durable PostgreSQL ingestion queue.
// Node >= 22; no npm dependencies. Source content is never executed.
import { createHash, randomUUID } from 'node:crypto';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match) continue;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    else value = value.replace(/\s+#.*$/, '').trim();
    values[match[1]] = value;
  }
  return values;
}

export function safeURL(target, base, prefix) {
  const url = new URL(target, base);
  const expected = new URL(base);
  if (url.origin !== expected.origin || url.username || url.password || !url.pathname.startsWith(prefix)) {
    throw new Error('Request outside configured origin/API path rejected');
  }
  return url;
}

export function fingerprint(page) {
  return createHash('sha256').update(JSON.stringify({
    id: page.id, version: page.version?.number, title: page.title,
    ancestors: (page.ancestors || []).map(p => [p.id, p.title]),
    labels: (page.metadata?.labels?.results || []).map(l => l.name).sort(),
  })).digest('hex');
}

export async function inventory(get, initial) {
  const pages = new Map(), visited = new Set();
  let next = initial;
  while (next) {
    if (visited.has(next)) throw new Error('Confluence pagination cycle');
    visited.add(next);
    const response = await get(next);
    if (!Array.isArray(response.results)) throw new Error('Invalid Confluence inventory');
    if (!response.results.length && response._links?.next) throw new Error('Empty intermediate inventory page');
    for (const page of response.results) {
      if (!/^\d+$/.test(String(page.id)) || !page.version?.number) throw new Error('Inventory lacks page identity/version');
      if (pages.has(String(page.id))) throw new Error('Unstable inventory: duplicate page; retry snapshot');
      pages.set(String(page.id), page);
    }
    if (pages.size > 50000) throw new Error('Inventory safety ceiling exceeded; refusing partial sync');
    next = response._links?.next;
  }
  return [...pages.values()];
}

export function indexSummary(documents, pages) {
  const expected = new Map(pages.map(p => [String(p.id), String(p.version?.number ?? p.version)]));
  const tracked = documents.filter(d => d.source_type === 'CONFLUENCE' && expected.has(d.external_id));
  const ids = tracked.map(d => d.external_id);
  const current = d => d.external_version === expected.get(d.external_id) && d.filename?.endsWith('.confluence.json');
  return { expected: expected.size, found: tracked.length,
    ready: tracked.filter(d => d.status === 'READY' && d.search_enabled && current(d)).length,
    stale: tracked.filter(d => !current(d)).map(d => d.external_id),
    missing: [...expected.keys()].filter(id => !ids.includes(id)),
    duplicates: ids.filter((id, index) => ids.indexOf(id) !== index),
    failed: tracked.filter(d => d.status === 'FAILED').map(d => d.external_id),
    pending: tracked.filter(d => ['QUEUED', 'PROCESSING'].includes(d.status)).length };
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
async function atomicJSON(filename, value) {
  const temporary = `${filename}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  await fs.rename(temporary, filename);
}
async function readJSON(filename, fallback) {
  try { return JSON.parse(await fs.readFile(filename, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return fallback; throw error; }
}

function client(base, prefix, headers) {
  return async (target, body, method = body === undefined ? 'GET' : 'POST') => {
    const url = safeURL(target, base, prefix);
    for (let attempt = 0; attempt < 4; attempt++) {
      let response;
      try {
        response = await fetch(url, {
          method, headers: { Accept: 'application/json', ...headers, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
          body: body === undefined ? undefined : JSON.stringify(body),
          redirect: 'manual', signal: AbortSignal.timeout(90000),
        });
      } catch {
        if (attempt < 3 && method === 'GET') { await delay(1000 * 2 ** attempt); continue; }
        throw new Error(`Network failure: ${method} ${url.pathname}`);
      }
      if ((response.status === 429 || response.status >= 500) && attempt < 3 && method === 'GET') {
        const retry = Number(response.headers.get('retry-after'));
        await response.body?.cancel();
        await delay(Math.min(30000, Number.isFinite(retry) && retry > 0 ? retry * 1000 : 1000 * 2 ** attempt));
        continue;
      }
      if (!response.ok) {
        await response.body?.cancel();
        const error = new Error(`HTTP ${response.status}: ${method} ${url.pathname}`);
        error.status = response.status;
        throw error;
      }
      // Error bodies, headers and credentials are deliberately never logged.
      try { return await response.json(); } catch { throw new Error(`Invalid JSON: ${url.pathname}`); }
    }
  };
}

export async function run(options) {
  const config = { ...parseEnv(await fs.readFile(options.env, 'utf8')), ...process.env };
  const base = config.CONFLUENCE_BASE_URL?.replace(/\/+$/, '');
  if (!base || !/^https:\/\//.test(base)) throw new Error('CONFLUENCE_BASE_URL must use HTTPS');
  if (config.CONFLUENCE_DEPLOYMENT !== 'data_center') throw new Error('This adapter currently supports Confluence Data Center only');
  if (!/^[A-Za-z0-9_-]+$/.test(options.space)) throw new Error('Invalid space key');
  const root = new URL(base);
  const cfPrefix = `${root.pathname.replace(/\/$/, '')}/rest/api/`;
  const token = config.CONFLUENCE_AUTH_MODE === 'basic'
    ? `Basic ${Buffer.from(`${config.CONFLUENCE_EMAIL}:${config.CONFLUENCE_API_TOKEN}`).toString('base64')}`
    : `Bearer ${config.CONFLUENCE_ACCESS_TOKEN}`;
  if (config.CONFLUENCE_AUTH_MODE === 'basic' && (!config.CONFLUENCE_EMAIL || !config.CONFLUENCE_API_TOKEN)) throw new Error('Missing Confluence Basic credentials');
  if (config.CONFLUENCE_AUTH_MODE !== 'basic' && !config.CONFLUENCE_ACCESS_TOKEN) throw new Error('Missing Confluence access token');
  const importCredentials = {
    email: config.CONFLUENCE_AUTH_MODE === 'basic' ? config.CONFLUENCE_EMAIL : undefined,
    token: config.CONFLUENCE_AUTH_MODE === 'basic'
      ? config.CONFLUENCE_API_TOKEN
      : config.CONFLUENCE_ACCESS_TOKEN,
  };
  const cf = client(base, cfPrefix, { Authorization: token });
  const ragBase = options['api-url'];
  const ragOrigin = new URL(ragBase);
  if (ragOrigin.protocol !== 'https:' && !['127.0.0.1', 'localhost', '[::1]'].includes(ragOrigin.hostname)) throw new Error('Remote RAG API must use HTTPS');
  const api = client(ragBase, '/api/v1/', config.RAG_API_TOKEN ? { Authorization: `Bearer ${config.RAG_API_TOKEN}` } : {});
  const output = path.resolve(options.output);
  await fs.mkdir(output, { recursive: true, mode: 0o700 });
  const lockPath = path.join(output, 'sync.lock');
  let lock;
  try { lock = await fs.open(lockPath, 'wx', 0o600); }
  catch (error) { if (error.code === 'EEXIST') throw new Error(`Sync lock exists at ${lockPath}; verify no sync is running before removing it`); throw error; }
  await lock.writeFile(JSON.stringify({ pid: process.pid, started_at: new Date().toISOString() }));
  try {
    const statePath = path.join(output, 'state.json');
    const state = await readJSON(statePath, { source: base, space: options.space, pages: {}, workspace_id: null });
    if (state.source !== base || state.space !== options.space) throw new Error('State belongs to a different source/space; choose another output directory');
    if (options['workspace-id'] && state.workspace_id && options['workspace-id'] !== state.workspace_id) throw new Error('State belongs to another workspace');
    let workspace = options['workspace-id'] || state.workspace_id;
    if (!options['snapshot-only']) {
      const status = await api('/api/v1/integrations/confluence/status');
      if (!status.can_import || !status.enabled || status.host !== root.hostname) throw new Error('RAG Confluence integration is unavailable, unauthorized, or points to another host');
      if (status.parser_format !== 'confluence_storage_v1') throw new Error('Deploy the structure-preserving Confluence API/worker before importing');
      if (!workspace && options['create-workspace']) {
        workspace = (await api('/api/v1/workspaces', { name: options['create-workspace'], source_mode: 'USER_DOCUMENTS' })).id;
        state.workspace_id = workspace;
        await atomicJSON(statePath, state);
      }
      if (!workspace) throw new Error('Specify --workspace-id or --create-workspace (or --snapshot-only)');
      // This API exposes workspace listing/PATCH, not GET /workspaces/{id}.
      // The documents route validates both existence and workspace access.
      await api(`/api/v1/workspaces/${workspace}/documents`);
      state.workspace_id = workspace;
    }
    const watch = Number(options['watch-seconds']);
    do {
      const started = Date.now();
      const runId = `${new Date().toISOString().replaceAll(':', '-')}-${randomUUID().slice(0, 8)}`;
      const directory = path.join(output, runId);
      await fs.mkdir(directory, { mode: 0o700 });
      const report = { run_id: runId, source: base, space: options.space, workspace_id: workspace,
        started_at: new Date().toISOString(), inventory_complete: false, snapshot_complete: false,
        policy: 'all_accessible_current_pages_including_copy_and_archived_labels',
        attachment_policy: 'metadata_only_no_ocr', pages: [], errors: [], imports: [], disabled: [] };
      try {
        const params = new URLSearchParams({ type: 'page', spaceKey: options.space, limit: '100', expand: 'version,ancestors,metadata.labels' });
        const pages = await inventory(cf, `${cfPrefix}content?${params}`);
        if (!pages.length && Object.keys(state.pages).length) throw new Error('Unexpected empty inventory: no automatic retirement');
        report.inventory_complete = true;
        console.log(JSON.stringify({ event: 'inventory', pages: pages.length, run_id: runId }));
        let documents = options['snapshot-only'] ? [] : await api(`/api/v1/workspaces/${workspace}/documents`);
        const byPage = new Map(documents.filter(d => d.source_type === 'CONFLUENCE').map(d => [d.external_id, d]));
        const changed = [];
        for (const listed of pages) {
          const id = String(listed.id), previous = state.pages[id];
          const key = fingerprint(listed);
          let record;
          try {
            if (previous?.fingerprint === key && previous.path) {
              const stored = path.resolve(output, previous.path);
              if (!stored.startsWith(output + path.sep)) throw new Error('Unsafe snapshot path');
              const bytes = await fs.readFile(stored);
              if (digest(bytes) !== previous.sha256) throw new Error('Snapshot integrity mismatch');
              record = { ...previous };
            } else {
              const params = new URLSearchParams({ expand: 'body.storage,version,space,ancestors,metadata.labels' });
              const page = await cf(`${cfPrefix}content/${id}?${params}`);
              if (String(page.id) !== id || page.space?.key !== options.space || page.version?.number !== listed.version.number) throw new Error('Page changed or moved during snapshot; retry');
              if (typeof page.body?.storage?.value !== 'string') throw new Error('Storage body missing');
              const attachments = [];
              let next = `${cfPrefix}content/${id}/child/attachment?limit=100`;
              const seen = new Set();
              while (next) {
                if (seen.has(next)) throw new Error('Attachment pagination cycle');
                seen.add(next);
                const data = await cf(next);
                if (!Array.isArray(data.results)) throw new Error('Invalid attachment listing');
                attachments.push(...data.results.map(a => ({ id: a.id, title: a.title, media_type: a.metadata?.mediaType,
                  version: a.version?.number, download: a._links?.download })));
                next = data._links?.next;
              }
              const snapshot = { ...page, snapshot_attachments: attachments };
              const bytes = Buffer.from(JSON.stringify(snapshot));
              const relative = path.join(runId, `${id}.json`);
              await fs.writeFile(path.join(output, relative), bytes, { mode: 0o600, flag: 'wx' });
              record = { id, title: page.title, version: String(page.version.number), fingerprint: key,
                path: relative, sha256: digest(bytes), attachments: attachments.length, downloaded_at: new Date().toISOString() };
            }
            state.pages[id] = record;
            report.pages.push(record);
            const document = byPage.get(id);
            if (!options['snapshot-only'] && record.retired_by_sync && document?.status === 'READY' && !document.search_enabled) {
              await api(`/api/v1/workspaces/${workspace}/documents/${document.id}`, { search_enabled: true }, 'PATCH');
              delete record.retired_by_sync;
            }
            if (!options['snapshot-only'] && (!document || document.external_version !== record.version || document.status === 'FAILED' || previous?.fingerprint !== key || !document.filename.endsWith('.confluence.json'))) changed.push(record);
          } catch (error) {
            report.errors.push({ page_id: id, error: error.message });
            // A confirmed loss of access must hide even a still-listed page.
            // Network/authentication/5xx failures do not imply deletion.
            const document = byPage.get(id);
            if (!options['snapshot-only'] && [403, 404].includes(error.status) && document?.search_enabled) {
              await api(`/api/v1/workspaces/${workspace}/documents/${document.id}`, { search_enabled: false }, 'PATCH');
              report.disabled.push({ page_id: id, document_id: document.id });
              if (state.pages[id]) state.pages[id].retired_by_sync = true;
            }
          }
          if (report.pages.length && report.pages.length % 25 === 0) console.log(JSON.stringify({ event: 'snapshot_progress', completed: report.pages.length, total: pages.length }));
        }
        report.snapshot_complete = report.errors.length === 0 && report.pages.length === pages.length;
        if (!options['snapshot-only']) {
          for (let index = 0; index < changed.length; index += 10) {
            const batch = changed.slice(index, index + 10);
            const response = await api(`/api/v1/workspaces/${workspace}/imports/confluence`, {
              page_ids: batch.map(p => p.id), expected_versions: Object.fromEntries(batch.map(p => [p.id, p.version])),
              credentials: importCredentials,
            });
            report.imports.push(...response.results);
            for (const item of response.results) if (item.status === 'failed') report.errors.push({ page_id: item.page_id, error: item.detail });
            console.log(JSON.stringify({ event: 'import_progress', completed: Math.min(index + 10, changed.length), total: changed.length, queued: response.queued, failed: response.failed }));
          }
          // Retire only connector-tracked pages after a complete non-empty listing
          // and a per-page confirmation. A source outage never implies deletion.
          const present = new Set(pages.map(p => String(p.id)));
          for (const [id] of Object.entries(state.pages)) {
            const document = byPage.get(id);
            if (present.has(id) || !document?.search_enabled) continue;
            let retire = false;
            try {
              const page = await cf(`${cfPrefix}content/${id}?expand=space`);
              retire = page.space?.key !== options.space || page.status !== 'current';
            } catch (error) {
              if ([403, 404].includes(error.status)) retire = true;
              else report.errors.push({ page_id: id, error: error.message });
            }
            if (retire) {
              await api(`/api/v1/workspaces/${workspace}/documents/${document.id}`, { search_enabled: false }, 'PATCH');
              report.disabled.push({ page_id: id, document_id: document.id });
              state.pages[id].retired_by_sync = true;
            }
          }
          const deadline = Date.now() + Number(options['wait-seconds']) * 1000;
          do {
            documents = await api(`/api/v1/workspaces/${workspace}/documents`);
            report.index = indexSummary(documents, pages);
            if (!report.index.pending || Date.now() >= deadline) break;
            await delay(5000);
          } while (true);
          report.index_complete = report.snapshot_complete && report.errors.length === 0
            && report.index.ready === pages.length && report.index.found === pages.length
            && report.index.stale.length === 0 && report.index.duplicates.length === 0;
        }
      } catch (error) { report.errors.push({ error: error.message }); }
      report.finished_at = new Date().toISOString();
      report.elapsed_ms = Date.now() - started;
      await atomicJSON(path.join(directory, 'manifest.json'), report);
      await atomicJSON(statePath, state);
      await atomicJSON(path.join(output, 'latest.json'), report);
      console.log(JSON.stringify({ event: 'complete', manifest: path.join(directory, 'manifest.json'), pages: report.pages.length,
        errors: report.errors.length, index: report.index, snapshot_complete: report.snapshot_complete, index_complete: report.index_complete }));
      if (!watch) { if (report.errors.length || (!options['snapshot-only'] && !report.index_complete)) process.exitCode = 1; break; }
      await delay(watch * 1000);
    } while (true);
  } finally {
    await lock.close();
    await fs.unlink(lockPath);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: {
    env: { type: 'string', default: '.env' }, space: { type: 'string', default: 'KBR' },
    output: { type: 'string', default: '.storage/confluence/KBR' },
    'snapshot-only': { type: 'boolean', default: false }, 'api-url': { type: 'string', default: 'http://127.0.0.1:8000' },
    'workspace-id': { type: 'string' }, 'create-workspace': { type: 'string' },
    'watch-seconds': { type: 'string', default: '0' }, 'wait-seconds': { type: 'string', default: '900' },
  } });
  const watch = Number(values['watch-seconds']), wait = Number(values['wait-seconds']);
  if (!Number.isFinite(watch) || (watch !== 0 && watch < 30) || !Number.isFinite(wait) || wait < 0) throw new Error('watch-seconds must be 0 or >=30; wait-seconds must be >=0');
  run(values).catch(error => { console.error(error.message); process.exitCode = 1; });
}
