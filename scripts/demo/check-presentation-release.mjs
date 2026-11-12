import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';

const root = 'apps/web/public/';
const base = 'http://192.0.2.122:15173/';
const paths = ['presentation/index.html','presentation/demo.html','presentation/speaker-script.html',
  'presentation/demo-runbook.html','presentation/slide-content.html','presentation/acceptance.html',
  'presentation/demo-manifest.json','presentation/acceptance-summary.json',
  'presentation/slides.pdf','presentation/speaker-script.pdf','presentation/demo-runbook.pdf',
  'presentation/assets/reviewer-remote-result.png','presentation/assets/rag-code-audit.png',
  'presentation/assets/rag-search.png','presentation-offline.zip'];
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
for(const path of paths) {
  const response = await fetch(base+path,{signal:AbortSignal.timeout(10000)});
  assert.equal(response.status,200,'Artifact unavailable: '+path);
  const remote = Buffer.from(await response.arrayBuffer());
  assert.equal(digest(remote),digest(await readFile(root+path)),'Artifact mismatch: '+path);
  if(path.endsWith('/demo-manifest.json')) {
    const manifest = JSON.parse(remote);
    assert.equal(manifest.visible_spaces,146);
    assert.equal(manifest.live_pages,309);
    assert.equal(manifest.topics.length,3);
    assert.equal(manifest.chats.length,9);
    assert(manifest.chats.every(chat=>chat.verified));
    assert.equal(manifest.reviewer.remote_review_verified,true);
    assert.equal(manifest.reviewer.remote_apply_minimal_verified,true);
    assert.equal(manifest.reviewer.remote_undo_full_html_verified,true);
  }
}
console.log(JSON.stringify({artifacts_checked:paths.length,http_status:200,all_sha256_match:true,
  spaces:146,pages:309,knowledge_bases:3,saved_chats:9,remote_reviewer_proof:true}));
