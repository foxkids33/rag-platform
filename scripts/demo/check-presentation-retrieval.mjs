// Read-only search check for the tiny, rehearsed presentation corpus.
// No LLM calls, chat writes, indexing, publishing, or Confluence requests.
import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const MODES = ['dense', 'lexical', 'hybrid'];
const LIMIT = 5;
const PRIVATE_REPORT = '.storage/presentation/retrieval-check.json';
const PUBLIC_REPORT = 'apps/web/public/presentation/acceptance-summary.json';
const POSITIVE_KEYS = [
  'env', 'top-stat', 'commands', 'tls-termination', 'tls-chain',
  'round-robin', 'compression', 'node-id',
];
const UNKNOWN_IDENTIFIER = 'BLUE-KB-942';

export function percentile(values, proportion) {
  if (!values.length) return null;
  const sorted = values.slice().sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * proportion) - 1)];
}

function milliseconds(value) {
  return value === null ? null : Math.round(value * 100) / 100;
}

export function inspectSearch(payload, { topic, item, expectedPages }) {
  if (payload.workspace_source_mode !== 'KNOWLEDGE_BASE'
      || payload.knowledge_base_id !== topic.kb_id
      || payload.knowledge_base_version_id !== topic.version_id
      || payload.rerank_applied !== false
      || !Array.isArray(payload.results)
      || payload.results.length > LIMIT) {
    throw new Error('Unexpected retrieval scope, version, limit, or reranker state');
  }
  const results = payload.results;
  if (results.some(result => result.source_scope !== 'knowledge_base'
      || !topic.pages.includes(result.source_page_id)
      || String(result.source_version) !== String(topic.page_versions[result.source_page_id]))) {
    throw new Error('Retrieval returned a page outside the verified four-document version');
  }
  const pageIds = [...new Set(results.map(result => result.source_page_id))];
  const hit = expectedPages.length ? pageIds.some(id => expectedPages.includes(id)) : null;
  const anchorMatches = item.key === 'unknown'
    ? results.filter(result => [result.text, result.parent_text]
      .some(value => typeof value === 'string' && value.toUpperCase().includes(UNKNOWN_IDENTIFIER))).length
    : null;
  return {
    returned_chunk_count: results.length,
    returned_page_ids: pageIds,
    expected_page_hit_at_5: hit,
    unknown_identifier_matching_candidates: anchorMatches,
    scope_valid: true,
  };
}

export function aggregateResults(records) {
  return Object.fromEntries(MODES.map(mode => {
    const samples = records.filter(record => record.mode === mode);
    const positive = samples.filter(record => record.positive);
    const successful = samples.filter(record => record.error === null);
    const latencies = successful.map(record => record.latency_ms);
    const hits = positive.filter(record => record.expected_page_hit_at_5 === true).length;
    const unknown = samples.find(record => !record.positive);
    return [mode, {
      positive_case_count: positive.length,
      expected_page_hits_at_5: hits,
      expected_page_hit_rate_at_5: positive.length ? hits / positive.length : null,
      request_count: samples.length,
      request_error_count: samples.length - successful.length,
      successful_request_latency_ms: {
        sample_count: latencies.length,
        p50: milliseconds(percentile(latencies, 0.5)),
        min: latencies.length ? milliseconds(Math.min(...latencies)) : null,
        max: latencies.length ? milliseconds(Math.max(...latencies)) : null,
        percentile_method: 'nearest_rank',
      },
      unknown_identifier_check: {
        sample_count: unknown ? 1 : 0,
        completed: unknown?.error === null,
        returned_chunk_count: unknown?.returned_chunk_count ?? null,
        matching_candidates: unknown?.unknown_identifier_matching_candidates ?? null,
        identifier_absent: unknown?.error === null
          && unknown.unknown_identifier_matching_candidates === 0,
      },
    }];
  }));
}

async function jsonRequest(base, endpoint, payload) {
  const response = await fetch(base + '/api/v1' + endpoint, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
    signal: AbortSignal.timeout(60000),
  });
  if (!response.ok) throw new Error('HTTP_' + response.status);
  return response.json();
}

async function verifyScopes(manifest) {
  const workspaces = await jsonRequest(manifest.base_url, '/workspaces');
  const scopes = [];
  for (const topic of manifest.topics) {
    const workspace = workspaces.find(item => item.id === topic.workspace_id);
    if (!workspace || workspace.source_mode !== 'KNOWLEDGE_BASE'
        || workspace.base_knowledge_base_id !== topic.kb_id
        || workspace.active_knowledge_base_version_id !== topic.version_id) {
      throw new Error('Presentation workspace does not use its declared published KB version');
    }
    const documents = await jsonRequest(manifest.base_url,
      '/knowledge-bases/' + topic.kb_id + '/versions/' + topic.version_id + '/documents');
    if (documents.length !== 4 || topic.pages.length !== 4
        || new Set(documents.map(document => document.external_id)).size !== 4
        || documents.some(document => !topic.pages.includes(document.external_id)
          || document.status !== 'READY' || !document.search_enabled
          || document.source_type !== 'CONFLUENCE'
          || String(document.external_version) !== String(topic.page_versions[document.external_id]))) {
      throw new Error('Presentation corpus is not its expected four ready Confluence pages');
    }
    scopes.push({
      topic_key: topic.key,
      workspace_id: topic.workspace_id,
      knowledge_base_id: topic.kb_id,
      version_id: topic.version_id,
      document_count: documents.length,
    });
  }
  return scopes;
}

export async function main() {
  const manifest = JSON.parse(await readFile('.storage/presentation/demo-manifest.json', 'utf8'));
  const destination = new URL(manifest.base_url);
  // Rehearsed questions must not accidentally be sent to an external destination.
  if (destination.protocol !== 'http:' || destination.hostname !== '192.0.2.122'
      || destination.port !== '15173' || destination.username || destination.password
      || destination.search || destination.hash) {
    throw new Error('This check is scoped to the authorized internal presentation server');
  }
  const positives = POSITIVE_KEYS.map(key => {
    const items = manifest.chats.filter(item => item.key === key);
    if (items.length !== 1 || !items[0].cited_pages?.length) {
      throw new Error('A fixed positive case or its previously audited source pages is missing');
    }
    return items[0];
  });
  const unknowns = manifest.chats.filter(item => item.key === 'unknown');
  if (unknowns.length !== 1 || !unknowns[0].question.includes(UNKNOWN_IDENTIFIER)
      || manifest.topics.length !== 3) {
    throw new Error('The fixed presentation corpus or absent-identifier case is missing');
  }
  const scopes = await verifyScopes(manifest);
  const startedAt = new Date().toISOString();
  const records = [];
  for (const mode of MODES) {
    for (const item of [...positives, unknowns[0]]) {
      const topic = manifest.topics.find(candidate => candidate.key === item.topic);
      if (!topic || item.workspace_id !== topic.workspace_id) {
        throw new Error('Case workspace does not match its verified topic scope');
      }
      const expectedPages = [...new Set(item.cited_pages.map(page => page.id))];
      const record = {
        case_key: item.key,
        positive: item.key !== 'unknown',
        mode,
        workspace_id: topic.workspace_id,
        expected_page_ids: expectedPages,
        latency_ms: null,
        error: null,
        expected_page_hit_at_5: null,
      };
      const start = performance.now();
      try {
        const result = await jsonRequest(manifest.base_url,
          '/workspaces/' + topic.workspace_id + '/search',
          { query: item.question, mode, limit: LIMIT, rerank: false });
        record.latency_ms = milliseconds(performance.now() - start);
        if (result.mode !== mode) throw new Error('Unexpected retrieval mode');
        Object.assign(record, inspectSearch(result, { topic, item, expectedPages }));
      } catch (error) {
        record.latency_ms = milliseconds(performance.now() - start);
        // Preserve only safe diagnostic categories, never HTTP body or query text.
        record.error = /^HTTP_\d+$/.test(error.message) ? error.message : error.name;
      }
      records.push(record);
      console.log(JSON.stringify({
        case_key: record.case_key, mode, latency_ms: record.latency_ms,
        expected_page_hit_at_5: record.expected_page_hit_at_5,
        unknown_identifier_matching_candidates: record.unknown_identifier_matching_candidates,
        error: record.error,
      }));
    }
  }
  const summary = {
    schema_version: 1,
    report_kind: 'rehearsed_demo_retrieval_check',
    generated_at: new Date().toISOString(),
    started_at: startedAt,
    positive_case_count: positives.length,
    absent_identifier_case_count: 1,
    scoped_workspace_count: scopes.length,
    documents_per_workspace: 4,
    scoped_document_count: scopes.reduce((count, scope) => count + scope.document_count, 0),
    retrieval_limit: LIMIT,
    rerank: false,
    llm_calls: 0,
    answer_history_writes: 0,
    metric_name: 'expected_page_hit_at_5',
    metric_definition: 'At least one previously audited expected page appears among the first 5 returned chunk candidates; this is not Recall@5 over distinct pages or factual coverage.',
    expected_page_basis: 'Sources of eight prepared answers, independently inspected for source fidelity before this check.',
    independent_holdout: false,
    release_benchmark: false,
    sla_measurement: false,
    request_order: 'Sequential: dense, lexical, hybrid; fixed case order; no randomized or load-controlled experiment.',
    limitations: [
      'Tiny rehearsed sample: 8 positive questions, each searching only 4 curated documents.',
      'Results do not measure answer accuracy, generation latency, ACL enforcement, or whole-corpus quality.',
      'The absent identifier check measures missing text in returned candidates, not an LLM refusal.',
      'Measured wall times are single-run observations under current load, not an SLA or causal speed comparison.',
      'No JustAI measurement or comparison was performed.',
    ],
    modes: aggregateResults(records),
  };
  await mkdir('.storage/presentation', { recursive: true });
  await mkdir('apps/web/public/presentation', { recursive: true });
  await writeFile(PRIVATE_REPORT, JSON.stringify({ ...summary, scopes, records }, null, 2) + '\n',
    { mode: 0o600 });
  await chmod(PRIVATE_REPORT, 0o600);
  // Public artifact contains only aggregates and methodological caveats.
  await writeFile(PUBLIC_REPORT, JSON.stringify(summary, null, 2) + '\n');
  console.log(JSON.stringify({ phase: 'complete', positive_cases: positives.length,
    request_errors: records.filter(record => record.error !== null).length,
    modes: summary.modes }));
  if (records.some(record => record.error !== null)) process.exitCode = 2;
  return summary;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    // Do not print credential-bearing URLs, article text, or raw server failures.
    console.error(JSON.stringify({ error: 'Presentation retrieval check failed', type: error.name }));
    process.exitCode = 1;
  });
}
