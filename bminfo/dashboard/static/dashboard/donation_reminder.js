(() => {
  'use strict';

  const dialog = document.getElementById('donationReminder');
  if (!dialog || typeof dialog.showModal !== 'function') return;

  const stateKey = 'bminfo.donationReminder';
  const cookieName = 'bminfo_donation_until';
  const minute = 60 * 1000;
  let memoryState = null;
  let pending = false;

  function suppressed() {
    const entry = document.cookie.split(';').map(item => item.trim())
      .find(item => item.startsWith(`${cookieName}=`));
    return entry && Number(entry.slice(cookieName.length + 1)) > Date.now();
  }

  function readState() {
    try {
      const state = JSON.parse(localStorage.getItem(stateKey));
      if (state && Number.isFinite(state.nextAt) && Number.isFinite(state.lastSeen)) return state;
    } catch (_) {
      // Private browsing or disabled storage: keep a timer for this page.
    }
    return memoryState;
  }

  function writeState(state) {
    memoryState = state;
    try { localStorage.setItem(stateKey, JSON.stringify(state)); } catch (_) { /* Use memory. */ }
  }

  function schedule(first = false) {
    const now = Date.now();
    writeState({nextAt: now + (first ? 1 + Math.random() * 2 : 10 + Math.random() * 10) * minute, lastSeen: now});
  }

  function check() {
    if (suppressed()) {
      if (dialog.open) dialog.close();
      return;
    }
    if (document.visibilityState !== 'visible' || dialog.open) return;

    const now = Date.now();
    let state = readState();
    // A visit after 30 minutes away starts with the short initial delay.
    if (!state || now - state.lastSeen > 30 * minute) {
      schedule(true);
      state = readState();
    }
    if (now - state.lastSeen >= minute) writeState({...state, lastSeen: now});
    if (state.nextAt > now) return;

    // Advance the shared deadline before opening, so other tabs wait their
    // turn. The Web Lock serializes simultaneous checks in supported browsers.
    schedule();
    dialog.showModal();
    // Keep the cross-tab lock until this dialog closes or its page unloads.
    return new Promise(resolve => dialog.addEventListener('close', resolve, {once: true}));
  }

  function tick() {
    if (suppressed()) {
      if (dialog.open) dialog.close();
      return;
    }
    if (pending) return;
    if (navigator.locks) {
      pending = true;
      navigator.locks.request(stateKey, {ifAvailable: true}, lock => {
        if (lock) return check();
      }).catch(() => check()).finally(() => { pending = false; });
    } else {
      check();
    }
  }

  dialog.querySelector('[data-donation-dismiss]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    if (!suppressed()) schedule();
  });
  dialog.querySelectorAll('[data-donation-days]').forEach(button => {
    button.addEventListener('click', () => {
      const days = Number(button.dataset.donationDays);
      const seconds = days * 86400;
      const until = Date.now() + seconds * 1000;
      document.cookie = `${cookieName}=${until}; Max-Age=${seconds}; Path=/; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
      // Notify other tabs through storage; cookies alone have no change event.
      writeState({nextAt: until, lastSeen: Date.now()});
      dialog.close();
    });
  });

  window.addEventListener('storage', tick);
  document.addEventListener('visibilitychange', tick);
  tick();
  window.setInterval(tick, 1000);
})();
