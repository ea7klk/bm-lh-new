const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../bminfo/dashboard/static/dashboard/donation_reminder.js'), 'utf8');
const minute = 60000;

function browser(shared = {storage: new Map(), cookies: new Map()}, random = 0) {
  let now = 1800000000000;
  let tick;
  const handlers = {};
  const buttons = Object.fromEntries([30, 365].map(days => [days, {dataset: {donationDays: String(days)}, addEventListener: (_, fn) => { buttons[days].click = fn; }}]));
  const dialog = {
    open: false, shown: 0,
    showModal() { this.open = true; this.shown++; },
    close() { this.open = false; for (const fn of handlers.close || []) fn(); },
    addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
    querySelector() { return {addEventListener: (_, fn) => { dialog.dismiss = fn; }}; },
    querySelectorAll() { return Object.values(buttons); },
  };
  const document = {
    visibilityState: 'visible',
    getElementById: () => dialog,
    addEventListener: (name, fn) => { handlers[name] = [fn]; },
    get cookie() { return [...shared.cookies].filter(([, c]) => c.expires > now).map(([name, c]) => `${name}=${c.value}`).join('; '); },
    set cookie(value) {
      const [pair, ...attributes] = value.split(';');
      const [name, content] = pair.split('=');
      const age = Number(attributes.find(s => s.trim().startsWith('Max-Age=')).trim().split('=')[1]);
      shared.cookies.set(name, {value: content, expires: now + age * 1000, attributes: value});
    },
  };
  const localStorage = {
    getItem: key => shared.storage.get(key) || null,
    setItem: (key, value) => shared.storage.set(key, value),
  };
  const navigator = {};
  const context = {
    document, localStorage, navigator, location: {protocol: 'https:'},
    Date: {now: () => now}, Math: {random: () => random},
    window: {
      setInterval: fn => { tick = fn; },
      addEventListener: (name, fn) => { handlers[name] = [fn]; },
    },
  };
  return {
    shared, dialog, document, buttons, navigator, context,
    start() { vm.runInNewContext(source, context); },
    advance(ms) { now += ms; tick(); },
    event(name) { for (const fn of handlers[name] || []) fn(); },
    get state() { return JSON.parse(shared.storage.get('bminfo.donationReminder')); },
    get now() { return now; },
  };
}

test('first reminder is randomized between one and three minutes', () => {
  for (const random of [0, 0.5, 0.999999]) {
    const b = browser(undefined, random);
    b.start();
    const delay = b.state.nextAt - b.now;
    assert.ok(delay >= minute && delay <= 3 * minute);
    b.advance(delay - 1);
    assert.equal(b.dialog.open, false);
    b.advance(1);
    assert.equal(b.dialog.open, true);
  }
});

test('navigation preserves the deadline and dismissal randomizes a 10–20 minute wait', () => {
  const first = browser();
  first.start();
  const nextPage = browser(first.shared, 0.999999);
  nextPage.start();
  assert.equal(nextPage.state.nextAt, first.state.nextAt);
  nextPage.advance(minute);
  nextPage.dialog.dismiss();
  const delay = nextPage.state.nextAt - nextPage.now;
  assert.ok(delay >= 10 * minute && delay <= 20 * minute);
  nextPage.advance(delay - 1);
  assert.equal(nextPage.dialog.open, false);
  nextPage.advance(1);
  assert.equal(nextPage.dialog.open, true);
});

for (const days of [30, 365]) {
  test(`self-declared donation suppresses reminders for ${days} days without refreshing expiry`, () => {
    const b = browser();
    b.start();
    b.advance(minute);
    b.buttons[days].click();
    assert.equal(b.dialog.open, false);
    const cookie = b.shared.cookies.get('bminfo_donation_until');
    assert.equal(cookie.expires - b.now, days * 86400000);
    assert.match(cookie.attributes, /SameSite=Lax; Secure$/);
    const nextPage = browser(b.shared);
    nextPage.start();
    nextPage.advance(minute);
    nextPage.advance(days * 86400000 - 1);
    assert.equal(nextPage.dialog.shown, 0);
    assert.equal(b.shared.cookies.get('bminfo_donation_until').expires, cookie.expires);
    nextPage.advance(1);
    nextPage.advance(minute);
    assert.equal(nextPage.dialog.shown, 1);
  });
}

test('background pages defer the dialog until visible', () => {
  const b = browser();
  b.start();
  b.document.visibilityState = 'hidden';
  b.advance(3 * minute);
  assert.equal(b.dialog.open, false);
  b.document.visibilityState = 'visible';
  b.event('visibilitychange');
  assert.equal(b.dialog.open, true);
});

test('another tab advances the shared deadline and donation suppression closes an open dialog', () => {
  const a = browser();
  const b = browser(a.shared);
  a.start();
  b.start();
  a.advance(minute);
  b.advance(minute);
  assert.equal(a.dialog.open, true);
  assert.equal(b.dialog.open, false);
  b.buttons[30].click();
  a.event('storage');
  assert.equal(a.dialog.open, false);
});

test('the cross-tab Web Lock is held until the dialog closes', async () => {
  const b = browser();
  let held = false;
  b.navigator.locks = {async request(_, options, callback) {
    assert.equal(options.ifAvailable, true);
    held = true;
    try { await callback({}); } finally { held = false; }
  }};
  b.start();
  await new Promise(resolve => setImmediate(resolve));
  b.advance(minute);
  assert.equal(held, true);
  assert.equal(b.dialog.open, true);
  b.dialog.dismiss();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(held, false);
});

test('blocked storage falls back to a functioning page timer', () => {
  const b = browser();
  b.context.localStorage = {
    getItem() { throw new Error('storage disabled'); },
    setItem() { throw new Error('storage disabled'); },
  };
  b.start();
  b.advance(minute);
  assert.equal(b.dialog.open, true);
});
