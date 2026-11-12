import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const browser = new EventTarget();
Object.assign(browser, {
  location: { href: 'https://rag.example.test/', origin: 'https://rag.example.test' },
  fetch: async () => new Response('{}', { status: 200 }),
});
globalThis.window = browser;

const { apiFetch, setApiAccessToken, setApiCsrfToken } = await import('../../apps/web/src/api.ts');
let calls = [];

beforeEach(() => {
  setApiAccessToken(null);
  setApiCsrfToken(null);
  calls = [];
  browser.fetch = async (input, init) => {
    calls.push({ input, init });
    return new Response('{}', { status: 200 });
  };
});

test('cookie session CSRF is sent on API mutations, not reads', async () => {
  setApiCsrfToken('memory-only-csrf');
  for (const method of ['POST', 'PATCH', 'PUT', 'DELETE']) {
    await apiFetch('/api/v1/workspaces', { method });
    assert.equal(calls.at(-1).init.headers.get('X-CSRF-Token'), 'memory-only-csrf');
    assert.equal(calls.at(-1).init.credentials, 'same-origin');
    assert.equal(calls.at(-1).init.redirect, 'error');
  }
  for (const method of ['GET', 'HEAD', 'OPTIONS']) {
    await apiFetch('/api/v1/workspaces', { method });
    assert.equal(calls.at(-1).init.headers.has('X-CSRF-Token'), false);
  }
});

test('auth headers never leak to third-party or non-API paths', async () => {
  setApiAccessToken('oidc-secret');
  setApiCsrfToken('csrf-secret');
  for (const url of [
    'https://external.example.test/api/v1/upload',
    '//external.example.test/api/v1/upload',
    'https://rag.example.test/api.evil/upload',
    '/presentation/example',
  ]) {
    await apiFetch(url, { method: 'POST' });
    const headers = calls.at(-1).init.headers;
    assert.equal(headers.has('Authorization'), false, url);
    assert.equal(headers.has('X-CSRF-Token'), false, url);
  }
});

test('OIDC bearer support remains available on API requests', async () => {
  setApiAccessToken('oidc-access-token');
  await apiFetch('/api/v1/auth/me');
  assert.equal(calls.at(-1).init.headers.get('Authorization'), 'Bearer oidc-access-token');
});

test('Request method and headers are preserved, including multipart boundary', async () => {
  setApiCsrfToken('csrf');
  const request = new Request('https://rag.example.test/api/v1/workspaces', {
    method: 'POST',
    headers: { 'X-Example': 'keep-me', 'Content-Type': 'application/json' },
    body: '{}',
  });
  await apiFetch(request);
  assert.equal(calls[0].init.headers.get('X-Example'), 'keep-me');
  assert.equal(calls[0].init.headers.get('Content-Type'), 'application/json');
  assert.equal(calls[0].init.headers.get('X-CSRF-Token'), 'csrf');

  const body = new FormData();
  body.append('file', new Blob(['content']), 'example.txt');
  await apiFetch('/api/v1/workspaces/upload', { method: 'POST', body });
  // The browser must generate the actual multipart boundary itself.
  assert.equal(calls[1].init.headers.has('Content-Type'), false);
});

test('401 from current cookie session requests signs out the UI', async () => {
  setApiCsrfToken('csrf');
  let events = 0;
  const onRequired = () => { events += 1; };
  browser.addEventListener('rag:authentication-required', onRequired);
  browser.fetch = async () => new Response('{}', { status: 401 });
  try {
    await apiFetch('/api/v1/workspaces');
    assert.equal(events, 1);
  } finally {
    browser.removeEventListener('rag:authentication-required', onRequired);
  }
});

test('401 from an old request cannot sign out a newly authenticated account', async () => {
  setApiCsrfToken('old-session');
  let resolve;
  browser.fetch = () => new Promise((done) => { resolve = done; });
  let events = 0;
  const onRequired = () => { events += 1; };
  browser.addEventListener('rag:authentication-required', onRequired);
  try {
    const pending = apiFetch('/api/v1/workspaces');
    setApiCsrfToken('new-session');
    resolve(new Response('{}', { status: 401 }));
    await pending;
    assert.equal(events, 0);
  } finally {
    browser.removeEventListener('rag:authentication-required', onRequired);
  }
});

test('bad login or external 401 is not mistaken for an expired active session', async () => {
  let events = 0;
  const onRequired = () => { events += 1; };
  browser.addEventListener('rag:authentication-required', onRequired);
  browser.fetch = async () => new Response('{}', { status: 401 });
  try {
    await apiFetch('/api/v1/auth/login', { method: 'POST' });
    assert.equal(events, 0);
    setApiCsrfToken('active-session');
    await apiFetch('https://external.example.test/api/v1/auth/me');
    assert.equal(events, 0);
  } finally {
    browser.removeEventListener('rag:authentication-required', onRequired);
  }
});
