import assert from 'node:assert/strict';
import test from 'node:test';
import { releaseErrors } from './check-bge-release.mjs';

function fixture() {
  return {
    configuration: { dataset_sha256: 'caae95822112223f2a0d32910c72a6175443b06711895274b020273357d05b3f', workspace_id: 'b15f3afe-55c0-4fa5-9c6c-1fe17779cade', mode: 'hybrid', limit: 10, rerank: false, answers: true, answer_temperature: 0, answer_max_tokens: 4096 },
    successful_case_count: 36, failed_case_count: 0, errors: [], warnings: [],
    source_documents: Array.from({ length: 298 }, () => ({ status: 'READY', search_enabled: true })),
    aggregate: { retrieval: { recall_at_5: 1 }, answer: { exact_value_accuracy: 1, required_fact_coverage: 0.97 } },
    cases: Array.from({ length: 36 }, (_, i) => ({
      case: { id: `kbr-${String(i + 1).padStart(3, '0')}`, expected_abstention: i >= 32, required_code_blocks: i === 2 ? ['command'] : [] },
      answer: { evaluated: true, abstained: i >= 32, finish_reason: i >= 32 ? 'insufficient_context' : 'stop', citation_valid: true },
      answer_metrics: { required_fact_coverage: 1, gold_context_fact_coverage: 1, code_block_coverage: 1, code_order_correct: true },
    })),
  };
}

test('accepts complete frozen release run', () => assert.deepEqual(releaseErrors(fixture()), []));
test('blocks observed instruction and code regressions', () => {
  const report = fixture();
  report.cases[15].answer_metrics.required_fact_coverage = 0;
  report.cases[2].answer_metrics.code_block_coverage = 0;
  const errors = releaseErrors(report);
  assert.ok(errors.some(e => e.startsWith('kbr-016:')));
  assert.ok(errors.some(e => e.startsWith('kbr-003:')));
});
test('rejects missing metadata, duplicate cases and v3 substitution', () => {
  assert.ok(releaseErrors({}).length > 0);
  const report = fixture();
  report.cases[1].case.id = report.cases[0].case.id;
  report.configuration.dataset_sha256 = 'v3';
  assert.ok(releaseErrors(report).includes('Unexpected case identities'));
  assert.ok(releaseErrors(report).includes('Dataset must be frozen KBR v2'));
});
