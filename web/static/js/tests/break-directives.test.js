import assert from 'node:assert/strict';
import test from 'node:test';

import { buildDirectives } from '../features/break-directives.js?v=24';

function draft(overrides = {}) {
  return {
    removedThemes: new Set(),
    addedThemes: new Set(),
    removedGaps: new Set(),
    correctedGaps: new Map(),
    newGaps: [],
    seminalOverrides: new Map(),
    verdictOverrides: new Map(),
    outputs: [],
    freeText: '',
    ...overrides,
  };
}

test('an empty break draft confirms the current result', () => {
  assert.deepEqual(buildDirectives(draft()), ['CONFIRMED']);
});

test('break directives preserve the backend grammar and ordering', () => {
  const result = buildDirectives(draft({
    removedThemes: new Set(['history']),
    addedThemes: new Set(['sociology']),
    removedGaps: new Set(['GAP-1']),
    correctedGaps: new Map([['GAP-2', 'Use the longitudinal study']]),
    newGaps: ['No replication data'],
    seminalOverrides: new Map([['SRC-1', 'Foundational source']]),
    verdictOverrides: new Map([['EVAL-1', 'A new instrument exists']]),
    outputs: [{ type: 'blog_post', audience: 'general public' }],
  }));

  assert.deepEqual(result, [
    'REMOVE THEME: history',
    'ADD THEME: sociology',
    'REMOVE GAP GAP-1',
    'CORRECT GAP GAP-2: Use the longitudinal study',
    'ADD GAP: No replication data',
    'OVERRIDE SEMINAL SRC-1: Foundational source',
    'OVERRIDE VERDICT EVAL-1: A new instrument exists',
    'SCRIBE OUTPUT: blog_post | audience: general public',
  ]);
});
