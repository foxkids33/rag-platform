import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

const wrapper = resolve(import.meta.dirname, 'compose-bge.sh');

function invoke({ release = 'custom-release', candidate = 'rag-test-bge', database = 'postgresql+asyncpg://test:secret@postgres:5432/rag', image = 'pytorch/pytorch:test', args = ['config', '--services'] } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'rag-compose-test-'));
  try {
    writeFileSync(join(dir, 'base.env'), `COMPOSE_PROJECT_NAME=${release}\n`);
    writeFileSync(join(dir, 'bge.env'), `COMPOSE_PROJECT_NAME=${candidate}\nBGE_DATABASE_URL=${database}\nBGE_PYTORCH_IMAGE=${image}\nBGE_GPU_DEVICE_ID=GPU-test\n`);
    writeFileSync(join(dir, 'docker'), '#!/bin/sh\nif [ "$2" = version ]; then exit 0; fi\nprintf "%s\\n" "$@"\nprintf "profiles=%s\\n" "$COMPOSE_PROFILES"\n', { mode: 0o755 });
    const env = { ...process.env, PATH: `${dir}:${process.env.PATH}`, RAG_ENV_FILE: join(dir, 'base.env'), RAG_BGE_ENV_FILE: join(dir, 'bge.env'), COMPOSE_PROFILES: 'rerank,bge-ui' };
    for (const key of ['COMPOSE_PROJECT_NAME', 'BGE_DATABASE_URL', 'BGE_PYTORCH_IMAGE', 'BGE_GPU_DEVICE_ID']) delete env[key];
    return spawnSync('bash', [wrapper, ...args], { env, encoding: 'utf8' });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('candidate cannot reuse a custom release project', () => {
  const result = invoke({ candidate: 'custom-release' });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /reuse/);
});

test('external database is rejected without printing credentials', () => {
  const result = invoke({ database: 'postgresql+asyncpg://test:secret@production:5432/rag' });
  assert.equal(result.status, 2);
  assert.doesNotMatch(result.stderr, /secret/);
});

test('GPU image placeholder is rejected before deployment', () => {
  assert.equal(invoke({ image: 'REPLACE_AFTER_GPU_INVENTORY' }).status, 2);
});

test('valid isolated project resets inherited profiles', () => {
  const result = invoke();
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /-p\nrag-test-bge\n/);
  assert.match(result.stdout, /profiles=\n/);
});

test('caller cannot override project isolation', () => {
  assert.equal(invoke({ args: ['--project-name', 'custom-release', 'up'] }).status, 2);
});

test('release overlay is explicit and keeps the existing BGE project', () => {
  const result = invoke({ args: ['--release', 'config', '--services'] });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /-p\nrag-test-bge\n/);
  assert.match(result.stdout, /docker-compose\.bge-m3\.yml\n-f\n.*docker-compose\.bge-release\.yml\nconfig/);
  assert.doesNotMatch(invoke().stdout, /bge-release/);
});

test('release mode still forbids arbitrary compose and project overrides', () => {
  for (const flag of ['--project-name', '--file', '--env-file', '--profile']) {
    assert.equal(invoke({ args: ['--release', flag, 'unsafe', 'up'] }).status, 2);
  }
});

test('real compose merge publishes only release web and preserves BGE storage', (t) => {
  const version = spawnSync('docker', ['compose', 'version'], { encoding: 'utf8' });
  if (version.status !== 0) return t.skip('Docker Compose is not installed');
  const dir = mkdtempSync(join(tmpdir(), 'rag-compose-merge-'));
  try {
    const base = join(dir, 'base.env');
    writeFileSync(base, 'AUTH_ENABLED=false\n');
    const root = resolve(import.meta.dirname, '../..');
    const result = spawnSync('docker', ['compose', '--env-file', base, '-p', 'rag-test-bge',
      '-f', join(root, 'deploy/docker-compose.server.yml'),
      '-f', join(root, 'deploy/docker-compose.bge-m3.yml'),
      '-f', join(root, 'deploy/docker-compose.bge-release.yml'),
      'config', '--format', 'json'], {
      encoding: 'utf8', env: { ...process.env, COMPOSE_PROFILES: '',
        RAG_SERVICE_ENV_FILE: base, POSTGRES_PASSWORD: 'fixture',
        DATABASE_URL: 'postgresql+asyncpg://rag:fixture@postgres:5432/rag',
        BGE_DATABASE_URL: 'postgresql+asyncpg://rag:fixture@postgres:5432/rag',
        MINIO_ACCESS_KEY: 'fixture', MINIO_SECRET_KEY: 'fixture-secret',
        BGE_PYTORCH_IMAGE: 'pytorch/pytorch:test', BGE_GPU_DEVICE_ID: '0',
        RAG_WEB_HOST_PORT: '15173', RAG_BIND_HOST: '127.0.0.1',
      },
    });
    assert.equal(result.status, 0, result.stderr);
    const config = JSON.parse(result.stdout);
    assert.equal(config.name, 'rag-test-bge');
    assert.deepEqual(config.services.web.profiles ?? [], []);
    assert.equal(config.services.web.ports.length, 1);
    assert.equal(String(config.services.web.ports[0].published), '15173');
    for (const [name, service] of Object.entries(config.services)) {
      if (name !== 'web') assert.equal((service.ports ?? []).length, 0, name);
    }
    assert.equal(config.services.api.environment.EMBEDDING_MODEL, 'BAAI/bge-m3');
    assert.equal(config.services.api.environment.EMBEDDING_DIM, '1024');
    assert.equal(config.services.worker.environment.EMBEDDING_DIM, '1024');
    assert.equal(config.services.api.environment.EMBEDDING_SCHEMA_INITIALIZATION_ALLOWED, 'false');
    assert.doesNotMatch(config.services.api.command.join(' '), /initialize_embedding_schema/);
    assert.equal(config.volumes.postgres_data.name, 'rag-test-bge_postgres_data');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
