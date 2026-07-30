import { emit } from './events.js?v=22';
import { updateState } from './store.js?v=22';

/* ── API ─────────────────────────────────────────────────────────────── */

export async function api(path, options = {}) {
  const opts = { credentials: 'same-origin', headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }

  const resp = await fetch(path, opts);
  if (resp.status === 401 && !path.includes('/auth/login')) {
    updateState({ user: null }, 'session:expired');
    emit('session:expired');
    throw new Error('Your session expired — sign in again.');
  }

  let payload = null;
  try { payload = await resp.json(); } catch { /* empty body */ }

  if (!resp.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? detail
      : `Request failed (HTTP ${resp.status})`);
  }
  return payload;
}
