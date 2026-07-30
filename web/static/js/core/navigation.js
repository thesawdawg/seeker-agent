import { $, $$ } from './dom.js?v=22';
import { emit } from './events.js?v=22';

/* ── view switching ──────────────────────────────────────────────────── */

export function showView(name) {
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${name}`; });
  $('#topbar').hidden = (name === 'login');
  emit('navigation:changed', { view: name });
}
