const listeners = new Map();

export function on(eventName, listener) {
  const eventListeners = listeners.get(eventName) || new Set();
  eventListeners.add(listener);
  listeners.set(eventName, eventListeners);
  return () => {
    eventListeners.delete(listener);
    if (!eventListeners.size) listeners.delete(eventName);
  };
}

export function emit(eventName, detail) {
  const eventListeners = listeners.get(eventName);
  if (!eventListeners) return;
  [...eventListeners].forEach(listener => listener(detail));
}

