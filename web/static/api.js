// Minimal API helper shared by the three consoles.
//
// The role token is kept in localStorage purely so the pilot consoles are usable
// in a browser. This is the development auth scheme described in medic/auth.py
// and is not how a real deployment should authenticate anybody.

export const TOKENS = {
  customer: 'dev-customer-token',
  chemist: 'dev-chemist-token',
  pharmacist: 'dev-pharmacist-token',
  admin: 'dev-admin-token',
  // No shared courier token on purpose. Couriers authenticate as individuals,
  // because the Schedule H1 register records which one took custody of a
  // package and a fleet-wide secret cannot answer that. The courier console
  // writes the signed-in rider's own token into localStorage, which is where
  // tokenFor() looks first.
  courier: null,
};

export function tokenFor(role) {
  try {
    return localStorage.getItem(`medic.token.${role}`) || TOKENS[role];
  } catch {
    return TOKENS[role];
  }
}

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.detail || body?.error || `Request failed (${status})`);
    this.status = status;
    this.kind = body?.error;
  }
}

export async function api(role, path, { method = 'GET', body, form } = {}) {
  const headers = { 'X-Medic-Token': tokenFor(role) };
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body: payload });
  if (response.status === 204) return null;
  const text = await response.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = { detail: text };
  }
  if (!response.ok) throw new ApiError(response.status, parsed);
  return parsed;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// replaceChildren() stringifies null into a literal "null" text node, unlike
// el(), which skips it. Any call site that builds a child list with
// conditionals has to go through here.
export function setChildren(host, ...children) {
  host.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

export function banner(host, kind, message) {
  host.replaceChildren(el('div', { class: `banner ${kind}`, text: message }));
}

export function clearBanner(host) {
  host.replaceChildren();
}

// Status -> pill class. Anything unknown reads as "waiting" rather than "fine".
const PILL = {
  DELIVERED: 'ok',
  READY_FOR_PICKUP: 'ok',
  OUT_FOR_DELIVERY: 'ok',
  CLEARED: 'ok',
  NOT_REQUIRED: 'ok',
  AWAITING_FULFILMENT: 'wait',
  CREATED: 'wait',
  PENDING: 'wait',
  VERIFIED: 'ok',
  FAILED: 'bad',
  REJECTED: 'bad',
  BLOCKED_NOT_DISPENSABLE: 'bad',
  CANCELLED_NO_CHEMIST: 'bad',
  CANCELLED_STOCK_OUT: 'bad',
  REJECTED_PRESCRIPTION: 'bad',
  CANCELLED_BY_CUSTOMER: 'bad',
};

export function statusPill(status) {
  return el('span', { class: `pill ${PILL[status] || 'wait'}`, text: status.replaceAll('_', ' ') });
}

export function schedulePill(schedule) {
  const cls = schedule === 'OTC' ? 'ok' : schedule === 'X' ? 'bad' : 'rx';
  return el('span', { class: `pill ${cls}`, text: schedule === 'OTC' ? 'OTC' : `Sch. ${schedule}` });
}

export function pageHeader(title, role) {
  return el(
    'header',
    {},
    el('h1', { text: title }),
    el('span', { class: 'tag', text: 'Jaipur pilot' }),
    el('span', { class: 'tag', text: `role: ${role}` }),
    el(
      'nav',
      {},
      el('a', { href: '/customer/', text: 'Customer' }),
      el('a', { href: '/chemist/', text: 'Chemist' }),
      el('a', { href: '/courier/', text: 'Courier' }),
      el('a', { href: '/admin/', text: 'Admin' }),
      el('a', { href: '/docs', text: 'API' }),
    ),
  );
}
