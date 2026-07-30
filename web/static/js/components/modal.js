import { $, clear } from '../core/dom.js?v=24';
import { toast } from './toast.js?v=24';

let confirmHandler = null;

export function openModal(title, bodyNode, onConfirm) {
  $('#modal-title').textContent = title;
  const body = $('#modal-body');
  clear(body);
  body.append(bodyNode);
  $('#modal').hidden = false;
  confirmHandler = onConfirm;
}

export function closeModal() {
  $('#modal').hidden = true;
  confirmHandler = null;
}

export function wireModal() {
  $('#modal-cancel').addEventListener('click', closeModal);
  $('#modal-confirm').addEventListener('click', async () => {
    if (!confirmHandler) return;
    const handler = confirmHandler;
    const confirm = $('#modal-confirm');
    confirm.disabled = true;
    try {
      await handler();
      closeModal();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      confirm.disabled = false;
    }
  });
  $('#modal').addEventListener('click', event => {
    if (event.target.id === 'modal') closeModal();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeModal();
  });
}
