#!/usr/bin/env node
// Materialize gold code from the approved snapshot, never via LLM generation.
import * as fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';

const { values: options } = parseArgs({ options: {
  snapshot: { type: 'string', default: '.storage/confluence/KBR' },
  questions: { type: 'string', default: 'evaluation/datasets/kbr.v1.questions.json' },
  output: { type: 'string', default: 'evaluation/results/kbr.v1.prepared.jsonl' },
} });
const root = path.resolve(options.snapshot);
const manifest = JSON.parse(await fs.readFile(path.join(root, 'latest.json'), 'utf8'));
if (!manifest.snapshot_complete) throw new Error('A complete snapshot is required');
const definitions = JSON.parse(await fs.readFile(options.questions, 'utf8'));
const prepared = [];
for (const item of definitions.cases) {
  const entry = manifest.pages.find(p => p.id === item.source_page_id);
  if (item.expected_abstention) { prepared.push(item); continue; }
  if (!entry || entry.version !== item.source_version) throw new Error(`Review ${item.id}: source page version does not match`);
  const filename = path.resolve(root, entry.path);
  if (!filename.startsWith(root + path.sep)) throw new Error('Unsafe snapshot path');
  const bytes = await fs.readFile(filename);
  if (createHash('sha256').update(bytes).digest('hex') !== entry.sha256) throw new Error('Snapshot checksum mismatch');
  const page = JSON.parse(bytes);
  const blocks = [...page.body.storage.value.matchAll(/<ac:structured-macro\b[^>]*\bac:name="(?:code|noformat)"[^>]*>([\s\S]*?)<\/ac:structured-macro>/g)]
    .map(match => match[1].match(/<!\[CDATA\[([\s\S]*?)\]\]>/)?.[1]);
  const code = (item.required_code_block_indices || []).map(index => {
    if (typeof blocks[index] !== 'string') throw new Error(`Missing code block ${index} in ${item.id}`);
    return blocks[index];
  });
  // Same naming rule as the importer, including Unicode alphanumeric characters.
  let name = path.posix.basename(page.title).trim().replace(/[^\p{L}\p{N}_.()\- ]+/gu, '_');
  name = [...name].slice(0, 420).join('').trimEnd() || 'confluence-page';
  if (name.endsWith('.html')) name = name.slice(0, -5);
  prepared.push({
    id: item.id, question: item.question, question_type: item.question_type,
    source_page_id: entry.id, source_version: entry.version,
    expected_filenames: [`${name}-${entry.id}.confluence.json`],
    required_facts: item.required_facts || [], required_code_blocks: code,
    expected_abstention: false,
    notes: `Self-authored development case. Confluence ${entry.id} v${entry.version}; snapshot ${manifest.run_id}; sha256 ${entry.sha256}. Not independently reviewed.`,
  });
}
await fs.mkdir(path.dirname(options.output), { recursive: true, mode: 0o700 });
await fs.writeFile(options.output, prepared.map(item => JSON.stringify(item)).join('\n') + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify({ output: options.output, cases: prepared.length, code_cases: prepared.filter(c => c.required_code_blocks?.length).length }));
