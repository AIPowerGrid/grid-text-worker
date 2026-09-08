// SPDX-FileCopyrightText: 2026 AI Power Grid
// SPDX-License-Identifier: AGPL-3.0-or-later

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const templates = new URL('../inference_worker/web/templates/', import.meta.url);
const setup = readFileSync(new URL('setup.html', templates), 'utf8');
const base = readFileSync(new URL('base.html', templates), 'utf8');
const settings = readFileSync(new URL('settings.html', templates), 'utf8');
const dashboard = readFileSync(new URL('dashboard.html', templates), 'utf8');

function instantiate(html, factory, fetch) {
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(source => source.includes(`function ${factory}()`));
  assert.ok(script);
  const context = vm.createContext({
    fetch, AbortSignal, setTimeout: callback => queueMicrotask(callback),
    setInterval: () => {}, window: { dispatchEvent() {}, open() {} }, CustomEvent: class {},
  });
  vm.runInContext(script, context);
  return vm.runInContext(`${factory}()`, context);
}

const response = data => ({ ok: true, json: async () => data });

test('setup links to the console and does not promise local wallet payouts', () => {
  assert.ok(setup.includes('https://console.aipowergrid.io/dashboard/api-key'));
  for (const file of ['setup.html', 'settings.html', 'dashboard.html']) {
    const source = readFileSync(new URL(file, templates), 'utf8');
    assert.ok(!source.includes('api.aipowergrid.io/register'));
    assert.ok(!source.includes('Dev Fund'));
    assert.ok(!source.includes('eth_requestAccounts'));
  }
  // Payout management lives in setup and settings; the dashboard stays about
  // operating, not wallets.
  for (const file of ['setup.html', 'settings.html']) {
    const source = readFileSync(new URL(file, templates), 'utf8');
    assert.ok(source.includes('https://console.aipowergrid.io/dashboard/settings'));
  }
  assert.ok(!dashboard.includes('https://console.aipowergrid.io/dashboard/settings'));
});

test('backend credentials remain editable and labels are backend-neutral', () => {
  assert.ok(!setup.includes('x-show="config.backend_api_key ||'));
  assert.ok(setup.includes('Backend endpoint'));
  assert.ok(!setup.includes('OpenAI Endpoint'));
});

test('setup and settings expose actual capacity controls', () => {
  assert.ok(setup.includes('Simultaneous jobs'));
  // The schedule is built from day/time/concurrency controls, not raw JSON.
  assert.ok(setup.includes('class="sched-days"'));
  assert.ok(setup.includes('toggleDay(day)'));
  assert.ok(setup.includes('scheduleJson()'));
  assert.ok(setup.includes('payload.GRID_SCHEDULE'));
  // Settings edits the same schedule through the same builder as setup.
  assert.ok(settings.includes('Operating schedule'));
  assert.ok(settings.includes('class="sched-days"'));
  assert.ok(settings.includes('toggleDay(day)'));
  assert.ok(settings.includes('scheduleJson()'));
  assert.ok(settings.includes('GRID_SCHEDULE: {{ settings.GRID_SCHEDULE'));
});

test('settings hydrates the schedule builder from the stored window', () => {
  // A stored day range must come back as selected days, not a blank builder.
  assert.match(settings, /hydrateSchedule\(\)/);
  assert.match(settings, /JSON\.parse\(raw\)/);
  assert.ok(settings.includes("GRID_SCHEDULE: this.scheduleJson()"));
});

test('settings names controls the way setup does', () => {
  assert.ok(settings.includes('Simultaneous jobs'));
  assert.ok(settings.includes('Jobs in window'));
  assert.ok(settings.includes('Backend endpoint'));
  assert.ok(!settings.includes('Max Threads'));
  assert.ok(!settings.includes('Ollama URL'));
});

test('schedule defaults to always-on and emits nothing in that state', () => {
  // 24x7 at full capacity is what "no schedule" already means to the grid.
  assert.match(setup, /days: \['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'\]/);
  assert.match(setup, /start: '00:00'/);
  assert.match(setup, /end: '23:59'/);
  assert.match(setup, /scheduleIsDefault\(\)\) return '';/);
});

test('grid model names default to the bare model name', () => {
  assert.ok(!setup.includes("'grid/' +"));
  assert.ok(setup.includes('grid_model: m,'));
});

test('dashboard presents den as work accounting, not money', () => {
  assert.ok(dashboard.includes('>Den/hr</span>'));
  assert.ok(dashboard.includes('operational rate, not a token amount or payout forecast'));
  assert.ok(dashboard.includes('>Den recorded</span>'));
  assert.ok(!dashboard.includes('Points/hr'));
  assert.match(dashboard, /formatRate\(sessionRates\.den_per_hour\)/);
  assert.match(dashboard, /formatRate\(sessionRates\.jobs_per_hour\)/);
  assert.match(dashboard, /maximumFractionDigits: 1/);
  assert.match(dashboard, /!this\.sessionRatesCaptured/);
  assert.ok(!dashboard.toLowerCase().includes(['ku', 'dos'].join('')));
});

test('backend detection supplies a machine-specific worker-name suggestion', async () => {
  const wizard = instantiate(setup, 'setupWizard', async url => {
    assert.equal(url, '/api/setup/detect');
    return response({
      found: false,
      worker_name: 'Text-Inference-Worker-host-7f91',
      backends: [],
    });
  });
  await wizard.runDetect();
  assert.equal(wizard.config.worker_name, 'Text-Inference-Worker-host-7f91');
});

test('settings never hydrate stored API keys into browser state', () => {
  assert.ok(!settings.includes('settings.GRID_API_KEY'));
  assert.ok(!settings.includes('settings.OPENAI_API_KEY'));
  assert.ok(settings.includes("GRID_API_KEY: ''"));
  assert.ok(settings.includes("OPENAI_API_KEY: ''"));
  assert.ok(settings.includes('Reconnect through Console'));
});

test('secure Console enrollment is the default and keeps the key out of browser state', async () => {
  const calls = [];
  const wizard = instantiate(setup, 'setupWizard', async (url, options) => {
    calls.push([url, options?.body ? JSON.parse(options.body) : null]);
    if (url === '/api/setup/enrollment/start') {
      return response({
        ok: true,
        status: 'pending',
        authorize_url: 'https://console.aipowergrid.io/dashboard/connect-worker/enrollment_abcdefghijklmnopqrstuvwxyz',
        worker_name: 'Text-Inference-Worker-test',
      });
    }
    if (url === '/api/setup/enrollment/poll') {
      return response({ ok: true, status: 'activated', worker_name: 'Text-Inference-Worker-test' });
    }
    throw new Error(`unexpected URL ${url}`);
  });
  wizard.config.worker_name = 'Text-Inference-Worker-test';
  await wizard.connectGridAccount();
  assert.equal(wizard.credential_mode, 'console');
  assert.equal(wizard.enrollment.status, 'activated');
  assert.equal(wizard.config.api_key, '');
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/setup/enrollment/start',
    '/api/setup/enrollment/poll',
  ]);
  assert.deepEqual(calls[0][1], {
    worker_name: 'Text-Inference-Worker-test',
    restart: false,
  });
});

test('retrying a failed Console enrollment replaces stale pending state', async () => {
  const calls = [];
  const wizard = instantiate(setup, 'setupWizard', async (url, options) => {
    calls.push([url, options?.body ? JSON.parse(options.body) : null]);
    if (url === '/api/setup/enrollment/start') {
      return response({
        ok: true,
        status: 'pending',
        authorize_url: 'https://console.aipowergrid.io/dashboard/connect-worker/enrollment_abcdefghijklmnopqrstuvwxyz',
        worker_name: 'Text-Inference-Worker-test',
      });
    }
    return response({ ok: true, status: 'activated', worker_name: 'Text-Inference-Worker-test' });
  });
  wizard.config.worker_name = 'Text-Inference-Worker-test';
  wizard.enrollment.status = 'error';
  await wizard.connectGridAccount(true);
  assert.equal(wizard.enrollment.status, 'activated');
  assert.equal(calls[0][1].restart, true);
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
  assert.match(wizard.deploy.error, /no model reached the Grid/);
  assert.equal(polls, 150);
});

test('only an accepted connection completes the wizard', async () => {
  let polls = 0;
  const calls = [];
  const wizard = instantiate(setup, 'setupWizard', async url => {
    calls.push(url);
    if (url === '/api/setup/complete') return response({ ok: true });
    if (url === '/api/status') {
      return response({ worker_running: true, grid_connected: ++polls === 3 });
    }
    if (url === '/api/grid-canary') {
      return response({ ok: true, status: 'passed', economic_effect: 'none' });
    }
    throw new Error(`unexpected URL ${url}`);
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, true);
  assert.equal(polls, 3);
  assert.equal(calls.at(-1), '/api/grid-canary');
});

test('a connected worker with a failed canary does not complete setup', async () => {
  const wizard = instantiate(setup, 'setupWizard', async url => {
    if (url === '/api/setup/complete') return response({ ok: true });
    if (url === '/api/status') return response({ grid_connected: true });
    if (url === '/api/grid-canary') {
      return { ok: false, json: async () => ({ ok: false, error: 'Canary mismatch' }) };
    }
    throw new Error(`unexpected URL ${url}`);
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, false);
  assert.equal(wizard.deploy.error, 'Canary mismatch');
});

test('advanced account keys stay registration-only', async () => {
  const calls = [];
  const wizard = instantiate(setup, 'setupWizard', async url => {
    calls.push(url);
    if (url === '/api/setup/complete') return response({ ok: true });
    if (url === '/api/status') return response({ grid_connected: true });
    throw new Error(`unexpected URL ${url}`);
  });
  wizard.credential_mode = 'manual';
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, true);
  assert.ok(!calls.includes('/api/grid-canary'));
});

test('a transient per-model error does not fail a deploy that still connects', async () => {
  // Several cold models mean one backend can be retrying while another lands;
  // only what is connected when the window closes decides the outcome.
  let polls = 0;
  const wizard = instantiate(setup, 'setupWizard', async url => {
    if (url === '/api/setup/complete') return response({ ok: true });
    if (url === '/api/status') {
      polls++;
      return response({
        worker_running: true,
        grid_connected: polls >= 4,
        connection_error: polls < 4 ? 'Grid connection lost; reconnecting.' : null,
        backends: [
          { name: 'a', grid_model: 'a', connected: 1, expected: 1, connection_error: null },
          { name: 'b', grid_model: 'b', connected: polls >= 4 ? 1 : 0, expected: 1,
            connection_error: polls < 4 ? 'Grid connection lost; reconnecting.' : null },
        ],
      });
    }
    if (url === '/api/grid-canary') return response({ ok: true, status: 'passed', economic_effect: 'none' });
    throw new Error(`unexpected URL ${url}`);
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, true);
  assert.equal(wizard.deploy.error, '');
});

test('a partial connection completes as partial, not as failure', async () => {
  const wizard = instantiate(setup, 'setupWizard', async url => {
    if (url === '/api/setup/complete') return response({ ok: true });
    return response({
      worker_running: true,
      grid_connected: false,
      backends: [
        { name: 'a', grid_model: 'a', connected: 1, expected: 1, connection_error: null },
        { name: 'b', grid_model: 'b', connected: 0, expected: 1, connection_error: 'still trying' },
      ],
    });
  });
  await wizard.deployWorker();
  assert.equal(wizard.deploy.done, true);
  assert.equal(wizard.deploy.partial, true);
  assert.equal(wizard.deploy.error, '');
  assert.equal(wizard.deployConnectedCount(), 1);
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
  wizard.credential_mode = 'manual';
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

test('console credentials clamp both concurrency controls to one', () => {
  // Core rejects >1 for an enrolled credential, in the baseline and inside a
  // schedule window alike — the wizard must not be able to build that config.
  const wizard = instantiate(setup, 'setupWizard', async () => response({}));
  wizard.credential_mode = 'console';
  wizard.config.max_threads = '4';
  wizard.sched.concurrency = 6;
  wizard.clampConcurrencyForCredential();
  assert.equal(wizard.config.max_threads, '1');
  assert.equal(wizard.sched.concurrency, 1);
});

test('an api-key credential leaves concurrency to the operator', () => {
  const wizard = instantiate(setup, 'setupWizard', async () => response({}));
  wizard.credential_mode = 'manual';
  wizard.config.max_threads = '4';
  wizard.sched.concurrency = 6;
  wizard.clampConcurrencyForCredential();
  assert.equal(wizard.config.max_threads, '4');
  assert.equal(wizard.sched.concurrency, 6);
});
