#!/usr/bin/env node
// Generate local private runtime artifacts only; no SSH, services, model calls or Reviewer edits.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmod, lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const privateDir = resolve(root, '.storage/reviewer-internal-access');
const names = ['credentials.json', 'htpasswd', 'tls.key', 'tls.crt', 'nginx.conf', 'status.json'];
const files = Object.fromEntries(names.map(name => [name, resolve(privateDir, name)]));
const server = '192.0.2.122', port = 15174, reversePort = 23991, synchronyReversePort = 23992;
const openssl = '/usr/bin/openssl';
const mode = process.argv[2] || 'status';

function nginxConfig() {
  return `# Own temporary Reviewer only; no RAG, recorder or model forwarding.
user nginx;
worker_processes 1;
pid /var/run/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 256; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log off;
    server_tokens off;
    map $http_upgrade $reviewer_connection_upgrade { default upgrade; '' close; }
    server {
        listen ${server}:${port} ssl;
        server_name ${server};
        ssl_certificate /etc/nginx/reviewer-access/tls.crt;
        ssl_certificate_key /etc/nginx/reviewer-access/tls.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_cache shared:REVIEWER_TLS:1m;
        ssl_session_timeout 10m;
        auth_basic "Reviewer internal demo";
        auth_basic_user_file /etc/nginx/reviewer-access/htpasswd;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy same-origin always;
        add_header Cache-Control no-store always;
        client_max_body_size 2m;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        location /synchrony {
            proxy_pass http://127.0.0.1:${synchronyReversePort};
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Host $http_host;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header Authorization "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $reviewer_connection_upgrade;
            proxy_buffering off;
        }
        location / {
            proxy_pass http://127.0.0.1:${reversePort};
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Host $http_host;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-Port ${port};
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header Authorization "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $reviewer_connection_upgrade;
            proxy_buffering off;
        }
    }
}
`;
}

async function exists(path) {
  try { await lstat(path); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
async function check(path, permissions = 0o600) {
  const stat = await lstat(path);
  assert(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1, 'Expected a regular private artifact');
  assert.equal(stat.uid, process.getuid(), 'Artifact owner mismatch');
  assert.equal(stat.mode & 0o777, permissions, 'Artifact permission mismatch');
}
async function fresh(path, body, permissions = 0o600) {
  await writeFile(path, body, { flag: 'wx', mode: permissions });
  await check(path, permissions);
}
function ssl(args, input) {
  return execFileSync(openssl, args, { input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
}
async function credentials() {
  await check(files['credentials.json']);
  const value = JSON.parse(await readFile(files['credentials.json'], 'utf8'));
  assert.equal(value.username, 'reviewer-demo');
  assert.match(value.password, /^[A-Za-z0-9_-]{32}$/);
  return value;
}

async function inspect() {
  for (const name of ['htpasswd', 'tls.key', 'nginx.conf']) await check(files[name]);
  await check(files['tls.crt'], 0o644);
  const value = await credentials();
  const hashed = (await readFile(files.htpasswd, 'utf8')).trim();
  const salt = hashed.match(/^reviewer-demo:\$apr1\$([^$]+)\$/)?.[1];
  assert(salt, 'Expected salted apr1 password hash');
  assert.equal(hashed, 'reviewer-demo:' + ssl(['passwd', '-apr1', '-salt', salt, '-stdin'], value.password + '\n'));
  assert.equal(await readFile(files['nginx.conf'], 'utf8'), nginxConfig(), 'Config changed manually; preserve rather than overwrite');
  ssl(['pkey', '-in', files['tls.key'], '-check', '-noout']);
  ssl(['x509', '-in', files['tls.crt'], '-checkip', server, '-noout']);
  ssl(['x509', '-in', files['tls.crt'], '-checkend', String(30 * 3600), '-noout']);
  return { checked_at: new Date().toISOString(), artifacts_prepared: true, preparation_only: true, services_started: false,
    public_url: `https://${server}:${port}/confluence`, remote_directory: '/home/intern/ZMostovoy/reviewer-access',
    upstream_on_server: `http://127.0.0.1:${reversePort}`, local_reviewer: 'http://127.0.0.1:23990/confluence',
    synchrony_upstream_on_server: `http://127.0.0.1:${synchronyReversePort}/synchrony`,
    local_synchrony: 'http://127.0.0.1:8091/synchrony',
    gateway_username: 'reviewer-demo', credentials_file: files['credentials.json'],
    tls_self_signed: true, trusted_ca: false,
    tls_sha256_fingerprint: ssl(['x509', '-in', files['tls.crt'], '-noout', '-sha256', '-fingerprint']).split('=')[1],
    certificate_validity: ssl(['x509', '-in', files['tls.crt'], '-noout', '-dates']).split('\n'),
    remote_files: ['nginx.conf', 'htpasswd', 'tls.crt', 'tls.key'], raw_credentials_transfer_allowed: false,
    external_tunnel_provider: false, ssh_password_stored: false, ssh_keys_created: false,
    helper_modifies_plugin: false, helper_modifies_tomcat: false, helper_modifies_confluence_base_url: false,
    model_calls: 0, remote_editor_verified: false };
}

async function prepare() {
  const storage = await lstat(resolve(root, '.storage'));
  assert(storage.isDirectory() && !storage.isSymbolicLink());
  await mkdir(privateDir, { recursive: true, mode: 0o700 });
  const directory = await lstat(privateDir);
  assert(directory.isDirectory() && !directory.isSymbolicLink());
  assert.equal(directory.uid, process.getuid());
  assert.equal(directory.mode & 0o777, 0o700);
  if (!await exists(files['credentials.json'])) {
    await fresh(files['credentials.json'], JSON.stringify({ username: 'reviewer-demo',
      password: randomBytes(24).toString('base64url') }, null, 2) + '\n');
  }
  const value = await credentials();
  if (!await exists(files.htpasswd)) {
    await fresh(files.htpasswd, value.username + ':' + ssl(['passwd', '-apr1', '-stdin'], value.password + '\n') + '\n');
  }
  const keyExists = await exists(files['tls.key']), certExists = await exists(files['tls.crt']);
  assert.equal(keyExists, certExists, 'Incomplete certificate pair; preserve and resolve explicitly');
  if (!keyExists) {
    ssl(['req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', '2',
      '-keyout', files['tls.key'], '-out', files['tls.crt'], '-subj', `/CN=${server}`,
      '-addext', `subjectAltName=IP:${server}`, '-addext', 'basicConstraints=critical,CA:FALSE',
      '-addext', 'extendedKeyUsage=serverAuth']);
    await chmod(files['tls.crt'], 0o644);
  }
  if (!await exists(files['nginx.conf'])) await fresh(files['nginx.conf'], nginxConfig());
  const status = await inspect();
  if (await exists(files['status.json'])) {
    await check(files['status.json']);
    await writeFile(files['status.json'], JSON.stringify(status, null, 2) + '\n', { mode: 0o600 });
  } else await fresh(files['status.json'], JSON.stringify(status, null, 2) + '\n');
  return status;
}

process.umask(0o077);
try {
  if (mode === 'self-test') {
    const config = nginxConfig();
    assert(config.includes(`listen ${server}:${port} ssl;`));
    assert(config.includes(`proxy_pass http://127.0.0.1:${reversePort};`));
    assert(config.includes(`proxy_pass http://127.0.0.1:${synchronyReversePort};`));
    assert(config.includes('user nginx;') && config.includes('auth_basic_user_file'));
    assert(config.includes('proxy_set_header Authorization "";'));
    assert(config.includes('proxy_set_header Upgrade $http_upgrade;'));
    assert(!config.includes('28001') && !config.includes('15173'));
    assert.equal(randomBytes(24).toString('base64url').length, 32);
    console.log('PASS: exact internal TLS bind; only own reverseSSH; auth retained; gatewaypassword stripped; no recorder/RAG route');
  } else if (mode === 'prepare') console.log(JSON.stringify(await prepare(), null, 2));
  else if (mode === 'status') console.log(JSON.stringify(await inspect(), null, 2));
  else throw new Error('Usage: reviewer-internal-access.mjs prepare|status|self-test');
} catch {
  // OpenSSL/assertion payloads can contain password hashes: never log their raw errors.
  console.error('Internal Reviewer artifact preparation failed. Preserve private files and inspect permissions/configuration; no service was started.');
  process.exitCode = 1;
}
