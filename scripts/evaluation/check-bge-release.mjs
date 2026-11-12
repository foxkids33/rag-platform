// Offline gate for the frozen KBR v2 first-release run; no API calls or writes.
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

export function releaseErrors(report) {
  const failures = [];
  const check = (condition, label) => { if (!condition) failures.push(label); };
  const config = report.configuration ?? {};
  const cases = report.cases ?? [];
  check(config.dataset_sha256 === 'caae95822112223f2a0d32910c72a6175443b06711895274b020273357d05b3f', 'Dataset must be frozen KBR v2');
  check(config.workspace_id === 'b15f3afe-55c0-4fa5-9c6c-1fe17779cade', 'Wrong BGE workspace');
  check(config.mode === 'hybrid' && config.limit === 10 && config.rerank === false && config.answers === true && config.answer_temperature === 0 && config.answer_max_tokens === 4096, 'Unexpected evaluation configuration');
  check(report.successful_case_count === 36 && report.failed_case_count === 0 && cases.length === 36, 'All 36 cases must complete');
  check(Array.isArray(report.errors) && report.errors.length === 0 && Array.isArray(report.warnings) && report.warnings.length === 0, 'Errors/warnings must be empty');
  check(new Set(cases.map(c => c.case?.id)).size === 36 &&
    Array.from({ length: 36 }, (_, i) => `kbr-${String(i + 1).padStart(3, '0')}`).every(id => cases.some(c => c.case?.id === id)), 'Unexpected case identities');
  check(report.source_documents?.length === 298 && report.source_documents.every(d => d.status === 'READY' && d.search_enabled), 'Expected 298 ready searchable documents');
  for (const c of cases) {
    const id = c.case?.id ?? 'unknown';
    check(c.answer?.evaluated === true && c.answer.abstained === c.case?.expected_abstention, `${id}: answer/abstention mismatch`);
    check(['stop', 'insufficient_context'].includes(c.answer?.finish_reason), `${id}: unexpected finish_reason`);
    if (!c.case?.expected_abstention) check(c.answer?.citation_valid === true, `${id}: invalid citations`);
    if (c.case?.required_code_blocks?.length) {
      check(c.answer_metrics?.code_block_coverage === 1 && c.answer_metrics?.code_order_correct === true, `${id}: code grouping/order regression`);
    }
  }
  for (const id of ['kbr-016', 'kbr-026', 'kbr-027']) {
    const c = cases.find(c => c.case?.id === id);
    check(c?.answer_metrics?.required_fact_coverage === 1 && c?.answer_metrics?.gold_context_fact_coverage === 1, `${id}: required facts missing from answer or gold context`);
  }
  check(report.aggregate?.retrieval?.recall_at_5 === 1, 'Recall@5 must stay 1');
  check(report.aggregate?.answer?.exact_value_accuracy === 1, 'Exact-value regression');
  check(report.aggregate?.answer?.required_fact_coverage >= 0.9291666666666667 - 1e-9, 'Overall fact coverage below previous BGE run');
  return failures;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    if (process.argv.length !== 3) throw new Error('Usage: node scripts/evaluation/check-bge-release.mjs REPORT.json');
    const report = JSON.parse(await readFile(process.argv[2], 'utf8'));
    const failures = releaseErrors(report);
    console.log(JSON.stringify({ status: failures.length ? 'BLOCKED' : 'PASS', failures,
      note: 'Offline quality gate only. Still verify the running model, schema, UI and access restrictions.' }, null, 2));
    process.exitCode = failures.length ? 2 : 0;
  } catch (error) {
    console.error(error.message);
    process.exitCode = 2;
  }
}
