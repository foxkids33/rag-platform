import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import https from 'node:https';
import { X509Certificate } from 'node:crypto';

const directory = '.storage/reviewer-internal-access';
const ca = await readFile(`${directory}/tls.crt`);
const credentials = JSON.parse(await readFile(`${directory}/credentials.json`, 'utf8'));
assert.equal(credentials.username, 'reviewer-demo');
const fingerprint = new X509Certificate(ca).fingerprint256;
function request(path, authorization) {
  return new Promise((resolve, reject) => {
    const req = https.get(`https://192.0.2.122:15174${path}`, {
      ca, headers: authorization ? {Authorization:authorization} : {}, timeout:10000,
    }, response => {
      assert.equal(response.socket.getPeerCertificate().fingerprint256, fingerprint);
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        body += chunk;
        if (body.length > 2_000_000) req.destroy(new Error('Response limit exceeded'));
      });
      response.on('end', () => resolve({status:response.statusCode,body,location:response.headers.location,challenge:response.headers['www-authenticate']}));
    });
    req.on('timeout', () => req.destroy(new Error('Request timed out')));
    req.on('error', reject);
  });
}
const authorization = 'Basic ' + Buffer.from(`${credentials.username}:${credentials.password}`).toString('base64');
const anonymous = await request('/confluence/login.action');
const wrong = await request('/confluence/login.action', 'Basic ' + Buffer.from('reviewer-demo:wrong-password').toString('base64'));
const direct = await request('/confluence/pages/viewpage.action?pageId=5636125');
const login = await request('/confluence/login.action', authorization);
const page = await request('/confluence/pages/viewpage.action?pageId=5636125', authorization);
const synchronyAnonymous = await request('/synchrony/heartbeat');
const synchronyAuthenticated = await request('/synchrony/heartbeat',authorization);
assert.equal(anonymous.status,401);
assert.match(anonymous.challenge,/Basic.*Reviewer internal demo/);
assert.equal(wrong.status,401);
assert.equal(direct.status,401);
assert.equal(login.status,200);
assert.match(login.body,/Confluence/);
assert.equal(page.status,302);
assert.match(page.location,/login\.action/);
assert.equal(synchronyAnonymous.status,401);
assert.equal(synchronyAuthenticated.status,200);
assert.equal(synchronyAuthenticated.body.trim(),'OK');
let previous = {};
try { previous = JSON.parse(await readFile('.storage/presentation/reviewer-relay-status.json','utf8')); }
catch (error) { if(error.code !== 'ENOENT') throw error; }
const status = {
  checked_at:new Date().toISOString(),remote_url:'https://192.0.2.122:15174/confluence',
  remote_editor_url:'https://192.0.2.122:15174/confluence/pages/editpage.action?pageId=5636125',
  gateway_verified:true,anonymous_status:anonymous.status,wrong_password_status:wrong.status,
  direct_page_anonymous_status:direct.status,gateway_authenticated_login_status:login.status,
  confluence_login_still_required:true,tls_pinned_fingerprint:fingerprint,tls_validation_disabled:false,
  external_tunnel_provider:false,remote_editor_verified:false,
  synchrony_anonymous_status:synchronyAnonymous.status,synchrony_authenticated_heartbeat_status:synchronyAuthenticated.status,
};
// Gateway liveness is a separate check, not a reason to erase a dated UI rehearsal.
if(previous.tls_pinned_fingerprint === fingerprint && previous.remote_url === status.remote_url) {
  for(const field of ['remote_editor_verified','remote_review_verified','remote_apply_minimal_verified',
    'remote_undo_full_html_verified','remote_review_request_ms','published_version_unchanged',
    'saved_result','remote_ui_checked_at','remote_ui_certificate_spki_pinned','remote_ui_arbitrary_certificate_bypass']) {
    if(field in previous) status[field] = previous[field];
  }
}
await writeFile('.storage/presentation/reviewer-relay-status.json',JSON.stringify(status,null,2)+'\n',{mode:0o600});
console.log(JSON.stringify(status));
