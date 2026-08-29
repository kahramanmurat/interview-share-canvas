/**
 * Mock backend for the System Design Interview Platform (spec v1.0).
 *
 * Shapes match spec §11 (data model), §12 (API surface) and §13 (tokens are
 * opaque, never the raw record). Everything lives in memory.
 *
 * To go live, replace exactly two things:
 *   1. `request()`  -> fetch(`${BASE}/v1/...`) with credentials
 *   2. `openSocket()` -> new WebSocket(collabUrl) and forward the same
 *      message names (join_room / document_update / presence_update / ping).
 * Every exported call already returns a Promise and every payload is JSON-safe.
 */

const NOW = () => new Date().toISOString();
const uid = (p) => p + '_' + Math.random().toString(36).slice(2, 10);

/* ---------------------------------------------------------------- latency */

let latencyScale = 1;
export function setLatencyScale(x) { latencyScale = Math.max(0, x); }
const wait = (a, b) => new Promise((r) => setTimeout(r, (a + Math.random() * (b - a)) * latencyScale));

export class ApiError extends Error {
  constructor(code, message, status) { super(message); this.code = code; this.status = status || 400; }
}

/** The single choke point every REST call goes through. Swap for fetch(). */
async function request(kind, fn) {
  await (kind === 'read' ? wait(70, 190) : wait(130, 320));
  return fn();
}

/* ------------------------------------------------------------------ store */

const db = {
  user: { id: 'usr_owner', email: 'dana@northwind.dev', display_name: 'Dana Reyes', organization_id: 'org_1' },
  sessions: [],
  participants: [],
  guestLinks: [],
  canvases: {},
  audit: [],
};

const audit = (session_id, action) => db.audit.push({ id: uid('aud'), session_id, action, at: NOW() });

const PALETTE_SEED = {
  blank: { nodes: [], edges: [], strokes: [] },
  shortener: {
    nodes: [
      { id: 'n1', type: 'browser', label: 'Web client', x: 80, y: 200, w: 168, h: 92 },
      { id: 'n2', type: 'gateway', label: 'API gateway', x: 340, y: 200, w: 168, h: 92 },
      { id: 'n3', type: 'service', label: 'Redirect service', x: 600, y: 120, w: 176, h: 92 },
      { id: 'n4', type: 'cache', label: 'Redis — hot slugs', x: 880, y: 120, w: 176, h: 92 },
      { id: 'n5', type: 'sql', label: 'Postgres — links', x: 880, y: 280, w: 176, h: 92 },
    ],
    edges: [
      { id: 'e1', from: 'n1', to: 'n2', label: 'HTTPS', style: 'elbow', arrowEnd: true },
      { id: 'e2', from: 'n2', to: 'n3', label: '', style: 'elbow', arrowEnd: true },
      { id: 'e3', from: 'n3', to: 'n4', label: 'read', style: 'elbow', arrowEnd: true },
      { id: 'e4', from: 'n3', to: 'n5', label: 'read/write', style: 'elbow', arrowEnd: true },
    ],
    strokes: [],
  },
};

export const TEMPLATES = [
  { id: 'blank', name: 'Blank canvas', note: 'Start from nothing' },
  { id: 'shortener', name: 'URL shortener starter', note: 'Client, gateway, cache, store' },
];

function seedSession(over) {
  const s = {
    id: uid('ses'), owner_user_id: db.user.id, title: 'Untitled interview', prompt: '',
    state: 'draft', candidate_editing_enabled: true, cursors_visible: true,
    duration_minutes: 45, scheduled_at: null, started_at: null, ended_at: null,
    created_at: NOW(), updated_at: NOW(), ...over,
  };
  db.sessions.push(s);
  db.canvases[s.id] = {
    id: uid('cvs'), session_id: s.id, schema_version: 3, latest_operation_cursor: 0,
    updated_at: NOW(), doc: structuredClone(PALETTE_SEED[over._seed || 'blank']),
  };
  delete s._seed;
  return s;
}

const daysAgo = (n) => new Date(Date.now() - n * 864e5).toISOString();

seedSession({
  title: 'Senior BE — Priya Raghavan', state: 'ended', _seed: 'shortener',
  prompt: 'Design a URL shortener that serves 50k redirects/second with custom slugs and click analytics.',
  created_at: daysAgo(6), updated_at: daysAgo(6), started_at: daysAgo(6),
  ended_at: new Date(Date.now() - 6 * 864e5 + 41.5 * 60000).toISOString(),
});
seedSession({
  title: 'Staff Infra — Marcus Oyelaran', state: 'live', _seed: 'shortener',
  prompt: 'Design a multi-region rate limiter used by every internal service. Discuss consistency trade-offs.',
  created_at: daysAgo(0), updated_at: NOW(), started_at: new Date(Date.now() - 14 * 60000).toISOString(),
});
seedSession({
  title: 'Senior FE — Ana Sørensen', state: 'draft',
  prompt: 'Design the collaborative canvas you are drawing on right now. Focus on the sync layer.',
  scheduled_at: new Date(Date.now() + 36e5 * 26).toISOString(), created_at: daysAgo(1), updated_at: daysAgo(1),
});
seedSession({
  title: 'Platform — Tomás Lindqvist', state: 'archived', _seed: 'shortener',
  prompt: 'Design an event pipeline for product analytics at 1M events/minute.',
  created_at: daysAgo(21), updated_at: daysAgo(20), started_at: daysAgo(21),
  ended_at: new Date(Date.now() - 21 * 864e5 + 38 * 60000).toISOString(),
});

db.participants.push(
  { id: uid('par'), session_id: db.sessions[0].id, user_id: db.user.id, display_name: 'Dana Reyes', role: 'owner', joined_at: daysAgo(6), left_at: daysAgo(6) },
  { id: uid('par'), session_id: db.sessions[0].id, user_id: null, display_name: 'Priya Raghavan', role: 'candidate', joined_at: daysAgo(6), left_at: daysAgo(6) },
  { id: uid('par'), session_id: db.sessions[1].id, user_id: db.user.id, display_name: 'Dana Reyes', role: 'owner', joined_at: NOW(), left_at: null },
);

['session.created', 'link.rotated', 'session.started', 'permission.changed', 'session.ended'].forEach((action, i) => {
  db.audit.push({ id: uid('aud'), session_id: db.sessions[0].id, action, at: new Date(Date.now() - 6 * 864e5 + i * 42e4).toISOString() });
});

const find = (id) => {
  const s = db.sessions.find((x) => x.id === id);
  if (!s) throw new ApiError('session_not_found', 'That interview no longer exists.', 404);
  return s;
};
const publicSession = (s) => ({
  ...s,
  participants: db.participants.filter((p) => p.session_id === s.id).map((p) => p.display_name),
  active_participants: db.participants.filter((p) => p.session_id === s.id && !p.left_at).map((p) => p.display_name),
});

/* -------------------------------------------------------------------- api */

export const api = {
  /** POST /v1/auth/magic-link */
  signIn: (email) => request('write', () => {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email || '')) throw new ApiError('invalid_email', 'Enter a work email address.');
    return { user: { ...db.user, email }, expires_in: 900 };
  }),

  /** GET /v1/sessions */
  listSessions: () => request('read', () => db.sessions.map(publicSession).sort((a, b) => b.updated_at.localeCompare(a.updated_at))),

  /** GET /v1/sessions/{id} */
  getSession: (id) => request('read', () => publicSession(find(id))),

  /** POST /v1/sessions */
  createSession: (body) => request('write', () => {
    if (!body.title || !body.title.trim()) throw new ApiError('title_required', 'Give the interview a title.');
    const s = seedSession({
      title: body.title.trim(), prompt: body.prompt || '', duration_minutes: body.duration_minutes || 45,
      scheduled_at: body.scheduled_at || null, candidate_editing_enabled: body.candidate_editing_enabled !== false,
      _seed: body.template_id || 'blank',
    });
    audit(s.id, 'session.created');
    return publicSession(s);
  }),

  /** PATCH /v1/sessions/{id} */
  patchSession: (id, patch) => request('write', () => {
    const s = find(id);
    Object.assign(s, patch, { updated_at: NOW() });
    if ('candidate_editing_enabled' in patch) audit(id, 'permission.changed');
    return publicSession(s);
  }),

  /** POST /v1/sessions/{id}/start */
  startSession: (id) => request('write', () => {
    const s = find(id);
    s.state = 'live'; s.started_at = s.started_at || NOW(); s.updated_at = NOW();
    audit(id, 'session.started');
    return publicSession(s);
  }),

  /** POST /v1/sessions/{id}/end */
  endSession: (id) => request('write', () => {
    const s = find(id);
    s.state = 'ended'; s.ended_at = NOW(); s.candidate_editing_enabled = false; s.updated_at = NOW();
    audit(id, 'session.ended');
    return publicSession(s);
  }),

  /** POST /v1/sessions/{id}/duplicate */
  duplicateSession: (id) => request('write', () => {
    const s = find(id);
    const copy = seedSession({ title: s.title + ' (copy)', prompt: s.prompt, duration_minutes: s.duration_minutes });
    db.canvases[copy.id].doc = structuredClone(db.canvases[id].doc);
    return publicSession(copy);
  }),

  archiveSession: (id) => request('write', () => {
    const s = find(id); s.state = 'archived'; s.updated_at = NOW();
    return publicSession(s);
  }),

  /** POST /v1/sessions/{id}/guest-links — only the hash is stored server-side */
  createGuestLink: (id, opts = {}) => request('write', () => {
    const s = find(id);
    db.guestLinks.filter((l) => l.session_id === id && !l.revoked_at).forEach((l) => { l.revoked_at = NOW(); });
    const token = Array.from(crypto.getRandomValues(new Uint8Array(16))).map((b) => b.toString(16).padStart(2, '0')).join('');
    const link = {
      id: uid('lnk'), session_id: id, token_hash: 'sha256:' + token.slice(0, 12) + '…',
      role_granted: opts.role || 'candidate', expires_at: opts.expires_at || null,
      max_uses: opts.max_uses || 10, revoked_at: null, created_at: NOW(),
    };
    db.guestLinks.push(link);
    audit(id, 'link.rotated');
    return { link, url: `https://interviews.northwind.dev/join/${token}`, token };
  }),

  /** DELETE /v1/sessions/{id}/guest-links/{linkId} */
  revokeGuestLink: (id, linkId) => request('write', () => {
    const l = db.guestLinks.find((x) => x.id === linkId);
    if (l) l.revoked_at = NOW();
    audit(id, 'link.revoked');
    return { ok: true };
  }),

  /** GET /v1/join/{token} — lobby preflight */
  previewJoin: (token) => request('read', () => {
    const l = db.guestLinks.find((x) => x.token_hash.includes(String(token).slice(0, 12)));
    if (token && !l) throw new ApiError('token_invalid', 'This link is not valid. Ask your interviewer for a new one.', 404);
    if (l && l.revoked_at) throw new ApiError('token_revoked', 'This link was revoked.', 403);
    const s = l ? find(l.session_id) : db.sessions.find((x) => x.state === 'live');
    if (s.state === 'ended' || s.state === 'archived') throw new ApiError('session_closed', 'This interview has ended.', 403);
    return { session_id: s.id, title: s.title, owner: db.user.display_name, duration_minutes: s.duration_minutes, capacity: 10 };
  }),

  /** POST /v1/join/{token} */
  join: (token, display_name, role = 'candidate') => request('write', () => {
    if (!display_name || display_name.trim().length < 2) throw new ApiError('name_required', 'Enter the name your interviewer will see.');
    const l = db.guestLinks.find((x) => x.token_hash.includes(String(token).slice(0, 12)));
    const s = l ? find(l.session_id) : db.sessions.find((x) => x.state === 'live');
    const active = db.participants.filter((p) => p.session_id === s.id && !p.left_at);
    if (active.length >= 10) throw new ApiError('at_capacity', 'This interview is full.', 409);
    const p = { id: uid('par'), session_id: s.id, user_id: null, display_name: display_name.trim(), role, joined_at: NOW(), left_at: null };
    db.participants.push(p);
    return { participant: p, session: publicSession(s), collab_token: uid('cbt'), collab_url: 'wss://collab.northwind.dev/v1/rooms/' + s.id, expires_in: 300 };
  }),

  removeParticipant: (id, participantId) => request('write', () => {
    const p = db.participants.find((x) => x.id === participantId);
    if (p) p.left_at = NOW();
    audit(id, 'participant.removed');
    return { ok: true };
  }),

  /** GET /v1/sessions/{id}/canvas — snapshot + short-lived collab credential */
  getCanvas: (id) => request('read', () => {
    const c = db.canvases[id];
    return { canvas_document_id: c.id, schema_version: c.schema_version, operation_cursor: c.latest_operation_cursor, doc: structuredClone(c.doc) };
  }),

  /** Autosave. Batched by the caller at ≤2s (spec §6.8). */
  saveCanvas: (id, doc, actor) => request('write', () => {
    const c = db.canvases[id];
    const s = find(id);
    if (actor === 'candidate' && !s.candidate_editing_enabled) throw new ApiError('editing_locked', 'The interviewer has locked editing.', 403);
    if (s.state === 'ended' && actor !== 'owner') throw new ApiError('session_ended', 'This interview has ended.', 403);
    const count = doc.nodes.length + doc.edges.length + doc.strokes.length;
    if (count > 2000) throw new ApiError('document_too_large', 'Canvas exceeds the supported element count.', 413);
    c.doc = structuredClone(doc);
    c.latest_operation_cursor += 1;
    c.updated_at = NOW();
    s.updated_at = NOW();
    return { operation_cursor: c.latest_operation_cursor, saved_at: c.updated_at };
  }),

  exportJson: (id) => request('read', () => ({
    schema_version: db.canvases[id].schema_version,
    session: { id, title: find(id).title, prompt: find(id).prompt, ended_at: find(id).ended_at },
    canvas: db.canvases[id].doc,
    exported_at: NOW(),
  })),

  auditTrail: (id) => request('read', () => db.audit.filter((a) => a.session_id === id).slice().reverse()),

  currentUser: () => db.user,
};

/* ----------------------------------------------------------------- socket */

const REMOTE_COLORS = ['#ec3013', '#2d5fd0', '#0f8a54'];

/**
 * Stand-in for the collaboration gateway. Emits the same message names the
 * real WebSocket will (spec §12) and scripts one remote participant so
 * presence, cursors and remote edits are visible in the prototype.
 */
export function openSocket({ sessionId, self, withRemote = true }) {
  const listeners = {};
  const emit = (type, payload) => (listeners[type] || []).forEach((fn) => fn(payload));
  let status = 'connecting';
  let alive = true;
  const timers = [];

  const setStatus = (s) => { status = s; emit('status', s); };

  const remote = { id: 'par_remote', display_name: 'Marcus Oyelaran', role: 'candidate', color: REMOTE_COLORS[1], cursor: { x: 700, y: 420 }, selection: [] };
  const roster = () => [
    { id: self.id, display_name: self.display_name, role: self.role, color: REMOTE_COLORS[0], you: true },
    ...(withRemote ? [{ id: remote.id, display_name: remote.display_name, role: remote.role, color: remote.color }] : []),
  ];

  timers.push(setTimeout(() => {
    if (!alive) return;
    setStatus('connected');
    emit('room_joined', { session_id: sessionId, operation_cursor: 0, participants: roster() });
    emit('presence_snapshot', roster());
  }, 260 * latencyScale + 120));

  if (withRemote) {
    let t = 0;
    timers.push(setInterval(() => {
      if (!alive || status !== 'connected') return;
      t += 1;
      remote.cursor = {
        x: 690 + Math.cos(t / 6) * 190 + Math.sin(t / 11) * 60,
        y: 400 + Math.sin(t / 5) * 120,
      };
      emit('presence_update', { participant_id: remote.id, display_name: remote.display_name, color: remote.color, cursor: remote.cursor });
    }, 900));
  }

  return {
    get status() { return status; },
    on(type, fn) { (listeners[type] ||= []).push(fn); return () => { listeners[type] = listeners[type].filter((f) => f !== fn); }; },
    /** client -> server: document_update / presence_update / ping */
    send(type, payload) {
      if (status !== 'connected') return { queued: true };
      if (type === 'document_update') timers.push(setTimeout(() => alive && emit('ack', { client_operation_id: payload.client_operation_id }), 90 * latencyScale));
      return { queued: false };
    },
    /** Simulates a brief network loss (spec §6.8, acceptance criterion 5). */
    dropConnection(ms = 4200) {
      if (status !== 'connected') return;
      setStatus('reconnecting');
      timers.push(setTimeout(() => {
        if (!alive) return;
        setStatus('connected');
        emit('presence_snapshot', roster());
        emit('resynced', { operation_cursor: db.canvases[sessionId]?.latest_operation_cursor ?? 0 });
      }, ms));
    },
    permissionChanged(enabled) { emit('permission_changed', { candidate_editing_enabled: enabled }); },
    endSession() { emit('session_ended', { at: NOW() }); },
    close() { alive = false; timers.forEach((t) => { clearTimeout(t); clearInterval(t); }); setStatus('offline'); },
  };
}
