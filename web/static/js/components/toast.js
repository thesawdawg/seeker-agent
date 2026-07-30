import { $, el } from '../core/dom.js?v=24';

export function toast(message, kind = '') {
  const className = `toast ${kind ? `is-${kind}` : ''}`;
  const node = el('div', { class: className, text: message });
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 7000 : 3500);
}

