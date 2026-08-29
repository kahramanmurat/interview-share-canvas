/** Browser client for the FastAPI REST and WebSocket APIs. */

const API_BASE = (window.INTERVIEW_API_BASE || window.location.origin).replace(/\/$/, '');

let sessionToken = sessionStorage.getItem('interview_session_token');
let collaborationToken = null;
let joinedParticipant = null;

export const TEMPLATES = [
  { id: 'blank', name: 'Blank canvas', note: 'Start from nothing' },
  { id: 'shortener', name: 'URL shortener starter', note: 'Client, gateway, cache, store' },
];

export function setLatencyScale() {
  // Compatibility no-op for the prototype's existing tweak control.
}

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

export function guestTokenFromLocation() {
  const match = window.location.pathname.match(/^\/join\/([A-Fa-f0-9]{32})\/?$/);
  return match ? match[1] : null;
}

function activeToken() {
  return collaborationToken || sessionToken;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = activeToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    credentials: 'include',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);

  if (!response.ok) {
    throw new ApiError(
      payload && payload.code ? payload.code : 'request_failed',
      payload && payload.message ? payload.message : `Request failed (${response.status}).`,
      response.status,
    );
  }

  const issuedToken = response.headers.get('X-Session-Token');
  if (issuedToken) {
    sessionToken = issuedToken;
    collaborationToken = null;
    sessionStorage.setItem('interview_session_token', issuedToken);
  }
  return payload;
}

function requireGuestToken(token) {
  const resolved = token || guestTokenFromLocation();
  if (!resolved) {
    throw new ApiError('token_required', 'Open the guest link sent by your interviewer.', 400);
  }
  return resolved;
}

export const api = {
  signIn: (email) => request('/v1/auth/magic-link', { method: 'POST', body: { email } }),
  listSessions: () => request('/v1/sessions'),
  getSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}`),
  createSession: (body) => request('/v1/sessions', { method: 'POST', body }),
  patchSession: (id, body) => request(`/v1/sessions/${encodeURIComponent(id)}`, { method: 'PATCH', body }),
  startSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/start`, { method: 'POST' }),
  endSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/end`, { method: 'POST' }),
  duplicateSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/duplicate`, { method: 'POST' }),
  deleteSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  archiveSession: (id) => request(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH', body: { state: 'archived' },
  }),
  createGuestLink: async (id, options = {}) => {
    const result = await request(`/v1/sessions/${encodeURIComponent(id)}/guest-links`, {
      method: 'POST', body: options,
    });
    return { ...result, url: `${window.location.origin}/join/${result.token}` };
  },
  revokeGuestLink: (id, linkId) => request(
    `/v1/sessions/${encodeURIComponent(id)}/guest-links/${encodeURIComponent(linkId)}`,
    { method: 'DELETE' },
  ),
  previewJoin: (token) => request(`/v1/join/${requireGuestToken(token)}`),
  join: async (token, displayName) => {
    const result = await request(`/v1/join/${requireGuestToken(token)}`, {
      method: 'POST', body: { display_name: displayName, role: 'candidate' },
    });
    collaborationToken = result.collab_token;
    joinedParticipant = result.participant;
    return result;
  },
  removeParticipant: (id, participantId) => request(
    `/v1/sessions/${encodeURIComponent(id)}/participants/${encodeURIComponent(participantId)}`,
    { method: 'DELETE' },
  ),
  getCanvas: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/canvas`),
  saveCanvas: (id, doc, actor, clientOperationId = `op_${crypto.randomUUID()}`) => request(`/v1/sessions/${encodeURIComponent(id)}/canvas`, {
    method: 'POST',
    body: { doc, actor, client_operation_id: clientOperationId },
  }),
  exportJson: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/export`),
  auditTrail: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/audit`),
};

export function openSocket({ sessionId }) {
  const listeners = {};
  let socket = null;
  let status = 'connecting';
  let closed = false;
  let reconnectTimer = null;
  let reconnectDelay = 600;
  let forcedReconnectDelay = null;

  const emit = (type, payload) => (listeners[type] || []).slice().forEach((fn) => fn(payload));
  const setStatus = (next) => {
    status = next;
    emit('status', next);
  };

  const connect = () => {
    if (closed) return;
    setStatus(status === 'connecting' ? 'connecting' : 'reconnecting');
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = activeToken();
    const query = token ? `?access_token=${encodeURIComponent(token)}` : '';
    socket = new WebSocket(`${scheme}//${window.location.host}/v1/rooms/${encodeURIComponent(sessionId)}${query}`);

    socket.addEventListener('open', () => {
      reconnectDelay = 600;
      socket.send(JSON.stringify({
        type: 'join_room',
        payload: {
          session_id: sessionId,
          participant_id: joinedParticipant ? joinedParticipant.id : undefined,
        },
      }));
    });
    socket.addEventListener('message', (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type === 'status') setStatus(message.status);
      else if (message.type === 'presence_snapshot') emit(message.type, message.participants || []);
      else emit(message.type, message);
    });
    socket.addEventListener('close', () => {
      if (closed) return;
      setStatus('reconnecting');
      const delay = forcedReconnectDelay === null ? reconnectDelay : forcedReconnectDelay;
      forcedReconnectDelay = null;
      reconnectTimer = setTimeout(connect, delay);
      if (delay === reconnectDelay) reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    });
    socket.addEventListener('error', () => {
      if (!closed) setStatus('reconnecting');
    });
  };

  connect();
  return {
    get status() { return status; },
    on(type, fn) {
      (listeners[type] ||= []).push(fn);
      return () => { listeners[type] = listeners[type].filter((candidate) => candidate !== fn); };
    },
    send(type, payload = {}) {
      if (!socket || socket.readyState !== WebSocket.OPEN || status !== 'connected') return { queued: true };
      socket.send(JSON.stringify({ type, payload }));
      return { queued: false };
    },
    dropConnection(ms = 4200) {
      if (!socket) return;
      forcedReconnectDelay = ms;
      socket.close();
    },
    permissionChanged(enabled) {
      emit('permission_changed', { candidate_editing_enabled: enabled });
    },
    endSession() {
      emit('session_ended', { at: new Date().toISOString() });
    },
    close() {
      closed = true;
      clearTimeout(reconnectTimer);
      if (socket) socket.close();
      setStatus('offline');
    },
  };
}
