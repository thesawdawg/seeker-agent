import assert from 'node:assert/strict';
import test from 'node:test';

import { emit, on } from '../core/events.js?v=22';

test('event subscriptions receive details and can unsubscribe', () => {
  const received = [];
  const unsubscribe = on('test:event', detail => received.push(detail));

  emit('test:event', { value: 1 });
  unsubscribe();
  emit('test:event', { value: 2 });

  assert.deepEqual(received, [{ value: 1 }]);
});
