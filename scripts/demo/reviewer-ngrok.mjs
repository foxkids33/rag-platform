#!/usr/bin/env node
// Temporary, fail-closed gateway for the already-running own Reviewer clone only.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { chmod, lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(import.meta.url);
const root = resolve(dirname(script), '../..');
const privateDir = resolve(root, '.storage/reviewer-ngrok');
const files = Object.fromEntries(['credentials', 'traffic-policy', 'agent-override', 'status'].map(name =>
  [name, resolve(privateDir, name + '.json')]));
const ngrok = '/snap/ngrok/current/ngrok';
const existingConfig = process.env.NGROK_CONFIG || resolve(homedir(), 'snap/ngrok/current/.config/ngrok/ngrok.yml');
const unit = 'reviewer-demo-ngrok-20261009.service';
const upstream = 'http://127.0.0.1:23990';
const agentApi = 'http://127.0.0.1:44040';
const lifetimeHours = 30;
const mode = process.argv[2] || 'status';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function policyFor(credentials) {
  return { on_http_request: [{ actions: [
    { type: 'basic-auth', config: { realm: 'Reviewer-demo', enforce: true,
      credentials: [`${credentials.username}:${credentials.password}`] } },
    { type: 'remove-headers', config: { headers: ['authorization'] } }
  ] }] };
}

function validateCredentials(value) {
  assert.equal(value.username, 'reviewer-demo');
  assert.match(value.password, /^[A-Za-z0-9_-]{32}$/);
}

async function safeFile(path) {
  const stat = await lstat(path);
  assert(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1, 'Expected an owned regular private file');
  assert.equal(stat.uid, process.getuid(), 'Private file owner mismatch');
  assert.equal(stat.mode & 0o777, 0o600, 'Private file must be mode0600');
  return stat;
}

async function writePrivate(path, value, fresh = false) {
  if (!fresh) await safeFile(path);
  await writeFile(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: fresh ? 'wx' : 'w' });
  await chmod(path, 0o600);
}

async function readPrivate(path) {
  await safeFile(path);
  return JSON.parse(await readFile(path, 'utf8'));
}

async function prepare() {
  const storage = await lstat(resolve(root, '.storage'));
  assert(storage.isDirectory() && !storage.isSymbolicLink(), 'Repository storage must be a real directory');
  await mkdir(privateDir, { recursive: true, mode: 0o700 });
  const directory = await lstat(privateDir);
  assert(directory.isDirectory() && !directory.isSymbolicLink(), 'Private directory must not be a symlink');
  assert.equal(directory.uid, process.getuid());
  assert.equal(directory.mode & 0o777, 0o700, 'Private directory must be mode0700');
  let credentials;
  try { credentials = await readPrivate(files.credentials); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    credentials = { username: 'reviewer-demo', password: randomBytes(24).toString('base64url') };
    await writePrivate(files.credentials, credentials, true);
  }
  validateCredentials(credentials);
  const version = (await readFile(existingConfig, 'utf8')).match(/^version:\s*["']?([23])["']?\s*$/m)?.[1];
  assert(version, 'Unsupported existing ngrok config version; no token is copied or config rewritten');
  const override = version === '3'
    ? { version: '3', agent: { web_addr: '127.0.0.1:44040' } }
    : { version: '2', web_addr: '127.0.0.1:44040' };
  for (const [path, value] of [[files['traffic-policy'], policyFor(credentials)], [files['agent-override'], override]]) {
    let exists = true;
    try { await safeFile(path); } catch (error) { if (error.code !== 'ENOENT') throw error; exists = false; }
    await writePrivate(path, value, !exists);
  }
  return credentials;
}

function ownUnit() {
  const output = execFileSync('systemctl', ['--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState',
    '-p', 'MainPID', '-p', 'ExecStart'], { encoding: 'utf8' });
  const values = Object.fromEntries(output.trim().split('\n').map(line => [line.split('=')[0], line.slice(line.indexOf('=') + 1)]));
  if (values.LoadState !== 'not-found') assert(values.ExecStart.includes(script), 'Unit is not owned by this script');
  return { active: values.ActiveState === 'active', mainPid: Number(values.MainPID || 0), loadState: values.LoadState };
}

async function assertApiPortFree() {
  const server = createServer();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(44040, '127.0.0.1', resolve); });
  await new Promise(resolve => server.close(resolve));
}

async function http(url, headers = {}) {
  return fetch(url, { headers, redirect: 'manual', signal: AbortSignal.timeout(10000) });
}

async function verify(publicUrl, credentials) {
  const url = new URL(publicUrl);
  assert.equal(url.protocol, 'https:');
  assert(!url.username && !url.password && !url.search && !url.hash && url.pathname === '/');
  const base = url.origin;
  const skipWarning = { 'ngrok-skip-browser-warning': 'true' };
  const anonymous = await http(base + '/confluence/login.action', skipWarning);
  const bad = await http(base + '/confluence/login.action', { ...skipWarning,
    Authorization: 'Basic ' + Buffer.from('reviewer-demo:deliberately-invalid').toString('base64') });
  const authorization = 'Basic ' + Buffer.from(`${credentials.username}:${credentials.password}`).toString('base64');
  const authenticated = await http(base + '/confluence/login.action', { ...skipWarning, Authorization: authorization });
  const loginBody = await authenticated.text();
  const ownPage = await http(base + '/confluence/pages/viewpage.action?pageId=5636125', skipWarning);
  assert.equal(anonymous.status, 401, 'Gateway must reject every unauthenticated request');
  assert.match(anonymous.headers.get('www-authenticate') || '', /Basic.*Reviewer-demo/i);
  assert.equal(bad.status, 401, 'Wrong gateway credentials must fail closed');
  assert.equal(ownPage.status, 401, 'Gateway must also guard direct page URLs');
  assert.equal(authenticated.status, 200, 'Valid gateway credentials must reach Confluence login');
  assert(/confluence/i.test(loginBody) && /login|authenticate/i.test(loginBody), 'Expected Confluence login HTML, not a ngrok error/interstitial');
  return { checked_at: new Date().toISOString(), verified: true, anonymous_status: anonymous.status,
    invalid_credentials_status: bad.status, direct_page_anonymous_status: ownPage.status,
    gateway_authenticated_login_status: authenticated.status, confluence_login_still_required: true,
    editor_remote_verified: false, frozen_plugin_modified: false };
}

async function run() {
  const credentials = await readPrivate(files.credentials);
  validateCredentials(credentials);
  assert.deepEqual(await readPrivate(files['traffic-policy']), policyFor(credentials));
  await safeFile(files['agent-override']);
  await assertApiPortFree();
  let statusExists = true;
  try { await safeFile(files.status); } catch (error) { if (error.code !== 'ENOENT') throw error; statusExists = false; }
  const status = { unit, upstream, agent_api: agentApi, expires_at: new Date(Date.now() + lifetimeHours * 3600000).toISOString(),
    active: false, public_url: null, basic_auth_enforced: true, inspection_enabled: false,
    token_config_modified: false, token_copied: false, plugin_modified: false, confluence_base_url_modified: false };
  await writePrivate(files.status, status, !statusExists);
  const configs = existingConfig + ',' + files['agent-override'];
  // Private paths are validated before spawning. No storage error can leave an unowned child behind.
  // Both --scheme and the published URL are checked: no downgrade to an unprotected tunnel.
  const child = spawn(ngrok, ['http', upstream, '--scheme=https', '--inspect=false', '--log=stdout',
    '--log-format=json', '--log-level=info', '--name=reviewer-demo', '--config=' + configs,
    '--traffic-policy-file=' + files['traffic-policy']], { stdio: ['ignore', 'pipe', 'pipe'] });
  let exited = false;
  let errorCode = null;
  const captureSafeError = chunk => {
    const code = chunk.toString().match(/ERR_NGROK_[0-9]+/g);
    if (code?.length) errorCode = code[0];
  };
  child.stdout.on('data', captureSafeError);
  child.stderr.on('data', captureSafeError);
  child.on('error', () => { exited = true; });
  child.on('exit', () => { exited = true; });
  const terminate = () => { child.kill('SIGTERM'); };
  process.on('SIGTERM', terminate);
  process.on('SIGINT', terminate);
  try {
    let endpoint;
    for (let attempt = 0; attempt < 45; attempt++) {
      if (exited) throw new Error('Ngrok failed before a protected endpoint was ready' + (errorCode ? ' (' + errorCode + ')' : ''));
      try {
        const response = await http(agentApi + '/api/tunnels');
        const data = await response.json();
        if (data.tunnels?.length) {
          assert.equal(data.tunnels.length, 1, 'Expected exactly one own HTTPS endpoint');
          endpoint = data.tunnels[0];
          assert.equal(endpoint.proto, 'https');
          assert.equal(new URL(endpoint.config.addr).origin, upstream);
          assert.equal(endpoint.config.inspect, false);
          break;
        }
      } catch (error) {
        if (error.code === 'ERR_ASSERTION') throw error;
      }
      await pause(1000);
    }
    assert(endpoint, 'No protected HTTPS endpoint discovered; no fallback to a plain tunnel');
    status.public_url = endpoint.public_url;
    status.verification = await verify(endpoint.public_url, credentials);
    status.active = true;
    status.ready_at = new Date().toISOString();
    await writePrivate(files.status, status);
    console.log(JSON.stringify({ event: 'protected_reviewer_tunnel_ready', unit, public_url: status.public_url,
      verified: status.verification.verified, expires_at: status.expires_at }));
    await new Promise(resolve => { if (exited) resolve(); else child.once('exit', resolve); });
  } catch (error) {
    status.safe_error = error.code === 'ERR_ASSERTION' ? 'Gateway verification failed; tunnel stopped' : String(error.message);
    console.error(JSON.stringify({ event: 'protected_reviewer_tunnel_failed', unit, error: status.safe_error }));
    process.exitCode = 1;
  } finally {
    terminate();
    status.active = false;
    status.stopped_at = new Date().toISOString();
    await writePrivate(files.status, status);
  }
}

async function main() {
  if (mode === 'self-test') {
    const credentials = { username: 'reviewer-demo', password: randomBytes(24).toString('base64url') };
    validateCredentials(credentials);
    const policy = policyFor(credentials);
    assert.equal(policy.on_http_request[0].actions[0].config.enforce, true);
    assert.equal(policy.on_http_request[0].actions[1].type, 'remove-headers');
    assert.deepEqual(policy.on_http_request[0].actions[1].config.headers, ['authorization']);
    assert.equal(policy.on_http_request[0].expressions, undefined);
    console.log('PASS: random32-character credential; mandatory auth on every route; auth stripped before upstream');
  } else if (mode === '__run') await run();
  else if (mode === 'start') {
    if (ownUnit().active) { console.log(JSON.stringify(await readPrivate(files.status), null, 2)); return; }
    const ready = await http(upstream + '/confluence/status');
    assert.equal((await ready.json()).state, 'RUNNING', 'The existing own Reviewer clone must already be running');
    await assertApiPortFree();
    await prepare();
    execFileSync(ngrok, ['config', 'check', '--config=' + existingConfig + ',' + files['agent-override']], { stdio: 'ignore' });
    execFileSync('systemd-run', ['--user', '--collect', '--unit=' + unit, '--property=WorkingDirectory=' + root,
      '--property=Restart=no', '--property=KillMode=control-group', '--property=TimeoutStopSec=10',
      '--property=RuntimeMaxSec=30h', process.execPath, script, '__run'], { stdio: 'ignore' });
    console.log(JSON.stringify({ event: 'started_own_temporary_unit', unit, credentials_file: files.credentials,
      status_file: files.status, max_runtime_hours: lifetimeHours }));
  } else if (mode === 'stop') {
    const state = ownUnit();
    if (state.loadState !== 'not-found') execFileSync('systemctl', ['--user', 'stop', unit], { stdio: 'ignore' });
    console.log(JSON.stringify({ stopped_only: unit, clone_and_recorder_unchanged: true }));
  } else if (mode === 'status' || mode === 'verify') {
    const state = ownUnit();
    const status = await readPrivate(files.status);
    if (mode === 'verify') {
      assert(state.active, 'Own tunnel unit is not active');
      status.verification = await verify(status.public_url, await readPrivate(files.credentials));
      await writePrivate(files.status, status);
    }
    console.log(JSON.stringify({ ...status, unit_active: state.active }, null, 2));
  } else throw new Error('Usage: reviewer-ngrok.mjs start|status|verify|stop|self-test');
}

await main().catch(() => {
  // Never print raw ngrok/config/auth exception payloads: those can contain secrets.
  console.error('Reviewer tunnel command failed. Inspect safe status JSON; do not remove gateway authentication.');
  process.exitCode = 1;
});
