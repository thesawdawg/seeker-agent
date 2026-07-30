export function markSelectedTab(tabs, isSelected) {
  tabs.forEach(tab => {
    const selected = isSelected(tab);
    tab.classList.toggle('is-active', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
}

export function wireTablistKeys(nav, onSelect, attribute) {
  nav.addEventListener('keydown', event => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!keys.includes(event.key)) return;

    const tabs = Array.from(nav.querySelectorAll('.tab')).filter(tab => !tab.hidden);
    if (!tabs.length) return;

    const current = tabs.indexOf(document.activeElement);
    let next;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else {
      const step = event.key === 'ArrowRight' ? 1 : -1;
      next = ((current < 0 ? 0 : current) + step + tabs.length) % tabs.length;
    }

    event.preventDefault();
    tabs[next].focus();
    onSelect(tabs[next].dataset[attribute]);
  });
}

