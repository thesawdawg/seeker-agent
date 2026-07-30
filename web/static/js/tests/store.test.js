import assert from 'node:assert/strict';
import test from 'node:test';

import { getState, subscribe, updateState } from '../core/store.js?v=22';

test('updateState applies a named patch and notifies subscribers', () => {
  const changes = [];
  const unsubscribe = subscribe(change => changes.push(change));

  updateState({ runId: 'RUN-TEST', tab: 'tree' }, 'test:run-selected');
  unsubscribe();

  assert.equal(getState().runId, 'RUN-TEST');
  assert.equal(getState().tab, 'tree');
  assert.equal(changes.length, 1);
  assert.equal(changes[0].action, 'test:run-selected');
  assert.deepEqual(changes[0].keys, ['runId', 'tab']);
  assert.equal(changes[0].previous.runId, null);

  updateState({ runId: null, tab: 'overview' }, 'test:cleanup');
});

test('state is sealed against undeclared top-level fields', () => {
  assert.equal(Object.isSealed(getState()), true);
  assert.throws(() => {
    getState().undeclared = true;
  }, TypeError);
});
