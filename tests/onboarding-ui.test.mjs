// SPDX-FileCopyrightText: 2026 AI Power Grid
// SPDX-License-Identifier: AGPL-3.0-or-later

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const templates = new URL('../inference_worker/web/templates/', import.meta.url);
const setup = readFileSync(new URL('setup.html', templates), 'utf8');
const base = readFileSync(new URL('base.html', templates), 'utf8');

function instantiate(html, factory, fetch) {
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(source => source.includes(`function ${factory}()`));
  assert.ok(script);
  const context = vm.createContext({
    fetch, AbortSignal, setTimeout: callback => queueMicrotask(callback),
    setInterval: () => {}, window: { dispatchEvent() {} }, CustomEvent: class {},
  });
  vm.runInContext(script, context);
  return vm.runInContext(`${factory}()`, context);
}

const response = data => ({ ok: true, json: async () => data });

test('setup links to the console and does not promise local wallet payouts', () => {
  assert.ok(setup.includes('https://console.aipowergrid.io/dashboard/api-key'));
  assert.ok(!setup.includes('api.aipowergrid.io/register'));
  for (const file of ['setup.html', 'settings.html', 'dashboard.html']) {
    const source = readFileSync(new URL(file, templates), 'utf8');
    assert.ok(!source.includes('Dev Fund'));
    assert.ok(!source.includes('eth_requestAccounts'));
    assert.ok(source.includes('https://console.aipowergrid.io/dashboard/settings'));
  }
});

test('saving configuration and a running process do not imply Grid acceptance', async () => {
  let polls = 0;
  const wizard = instantiate(setup, 'setupWizard', async url => {
    if (url === '/api/setup/complete') return response({ ok: true });
    polls++;
    return response({ worker_running: true, grid_connected: false });
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, false);
  assert.match(wizard.deploy.error, /not confirmed/);
  assert.equal(polls, 90);
});

test('only an accepted connection completes the wizard', async () => {
  let polls = 0;
  const wizard = instantiate(setup, 'setupWizard', async url => {
    if (url === '/api/setup/complete') return response({ ok: true });
    return response({ worker_running: true, grid_connected: ++polls === 3 });
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, true);
  assert.equal(polls, 3);
});

test('a rejected key is not a successful setup', async () => {
  const wizard = instantiate(setup, 'setupWizard', async url => response(
    url === '/api/setup/complete' ? { ok: true } : { connection_error: 'Grid rejected registration' },
  ));
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, false);
  assert.equal(wizard.deploy.error, 'Grid rejected registration');
});

test('failed status request never completes setup', async () => {
  const wizard = instantiate(setup, 'setupWizard', async url => (
    url === '/api/setup/complete' ? response({ ok: true }) : { ok: false, status: 401 }
  ));
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, false);
  assert.match(wizard.deploy.error, /status unavailable/);
});

test('blank worker name is not replaced with a shared hardcoded identity', async () => {
  let payload;
  const wizard = instantiate(setup, 'setupWizard', async (url, options) => {
    if (url === '/api/setup/complete') {
      payload = JSON.parse(options.body);
      return response({ ok: true });
    }
    return response({ grid_connected: true });
  });
  await wizard.deployWorker();
  assert.equal(payload.GRID_WORKER_NAME, '');
});

test('shell distinguishes process running, partial registration, and online', () => {
  const shell = instantiate(base, 'shellStatus', async () => response({}));
  shell.s = { worker_running: true, grid_connected: false, connected_workers: 0 };
  assert.equal(shell.statusLabel(), 'Connecting');
  assert.notEqual(shell.statusClass(), 'online');
  shell.s.connected_workers = 1;
  assert.equal(shell.statusLabel(), 'Partially connected');
  shell.s.grid_connected = true;
  assert.equal(shell.statusLabel(), 'Online');
});

test('unreachable dashboard invalidates a previously online status', async () => {
  const shell = instantiate(base, 'shellStatus', async () => { throw new Error('offline'); });
  shell.s = { worker_running: true, grid_connected: true };
  await shell.poll();
  assert.equal(shell.statusLabel(), 'Status unavailable');
  assert.notEqual(shell.statusClass(), 'online');
});
