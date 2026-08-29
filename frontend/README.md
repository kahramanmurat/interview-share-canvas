# Interview canvas — front-end prototype

A working front end for the system-design interview platform described in `spec.md`.
Every backend call is mocked in `mock-backend.js`; no server is required to run it.

## Run it

Any static server (the page uses ES modules, so `file://` will not work):

```bash
python3 -m http.server 8080
# then open http://localhost:8080/interview-platform.dc.html
```

## Files

| File | What it is |
| --- | --- |
| `interview-platform.dc.html` | The whole front end: six screens, one component. Template + logic class in one file. |
| `mock-backend.js` | The fake backend. This is the file you replace. |
| `modernist.css` | Design tokens and component classes (colors, type, spacing, buttons, table, dialog). |
| `support.js` | Rendering runtime for the component file. Not application code. |

## Screens

Dashboard → create session → live canvas → end → review, plus a candidate lobby and sign-in.
The black **Demo** strip in the bottom-right switches role (interviewer / candidate) and jumps between screens; it is prototype scaffolding, not product UI.

The canvas is real: 28-component palette, drag, resize, marquee multi-select, attached connectors
(elbow / straight / curved, labels, dashed), pen, highlighter, eraser, pan, zoom, zoom-to-fit,
undo/redo, and keyboard shortcuts (`V H C P M E T N`, `⌘Z`, `⇧⌘Z`, `⌘D`, `Delete`).

## Replacing the mock backend

Two seams, both in `mock-backend.js`:

1. **`request(kind, fn)`** — every REST call goes through it. Replace the body with
   `fetch(\`${BASE}/v1/...\`, { credentials: 'include' })` and the whole `api` object is live.
2. **`openSocket({ sessionId, self })`** — returns `{ status, on, send, close }` and emits the
   message names from spec §12. Replace with a real `WebSocket` that forwards the same names.

Nothing else in the UI imports anything backend-shaped, so no screen changes.

### Endpoint map (spec §12)

| Mock method | HTTP endpoint | Spec |
| --- | --- | --- |
| `api.signIn(email)` | `POST /v1/auth/magic-link` | §6.1 |
| `api.listSessions()` | `GET /v1/sessions` | §6.10 |
| `api.createSession(body)` | `POST /v1/sessions` | §5.1 |
| `api.getSession(id)` | `GET /v1/sessions/{id}` | §11 |
| `api.patchSession(id, patch)` | `PATCH /v1/sessions/{id}` | §6.9 |
| `api.startSession(id)` | `POST /v1/sessions/{id}/start` | §6.1 |
| `api.endSession(id)` | `POST /v1/sessions/{id}/end` | §5.4 |
| `api.duplicateSession(id)` | `POST /v1/sessions/{id}/duplicate` | §6.10 |
| `api.archiveSession(id)` | `PATCH /v1/sessions/{id}` | §6.10 |
| `api.createGuestLink(id, opts)` | `POST /v1/sessions/{id}/guest-links` | §6.1, §13 |
| `api.revokeGuestLink(id, linkId)` | `DELETE /v1/sessions/{id}/guest-links/{linkId}` | §6.9 |
| `api.previewJoin(token)` | `GET /v1/join/{token}` | §6.2 |
| `api.join(token, name, role)` | `POST /v1/join/{token}` | §5.2 |
| `api.removeParticipant(id, pid)` | `DELETE /v1/sessions/{id}/participants/{pid}` | §6.9 |
| `api.getCanvas(id)` | `GET /v1/sessions/{id}/canvas` | §6.7 |
| `api.saveCanvas(id, doc, actor)` | operation stream / snapshot write | §6.8 |
| `api.exportJson(id)` | `GET /v1/sessions/{id}/export` | §5.4 |
| `api.auditTrail(id)` | `GET /v1/sessions/{id}/audit` | §13 |

### Socket messages (spec §12)

Server → client, emitted by the mock: `room_joined`, `presence_snapshot`, `presence_update`,
`permission_changed`, `session_ended`, `ack`, `resynced`, `status`.
Client → server, accepted by `send()`: `document_update`, `presence_update`, `ping`.

## What the mock deliberately simulates

- **Latency** — reads 70–190 ms, writes 130–320 ms, scaled by `setLatencyScale()` (exposed as a tweak).
- **A second participant** — scripted cursor movement and presence for the interviewer view.
- **Autosave** — debounced 1.2 s writes with saving / saved / not-saved status (§6.8 requires ≤2 s).
- **Server-side authorization** — `saveCanvas` rejects candidate writes when
  `candidate_editing_enabled` is false and after a session ends (§6.9, acceptance criterion 7).
- **Reconnect** — `socket.dropConnection(ms)` drops and converges without a reload
  (acceptance criterion 5). Triggered from the canvas right panel.
- **Guest links** — 128-bit tokens, only a hash retained, rotate and revoke (§13).
- **Limits** — 10 participants per room, 2,000 elements per document (§14).

## Known gaps

- Single browser tab: convergence between two real clients is not exercised, and there is no CRDT.
- Undo/redo is snapshot-based and global to the tab, not per-participant (§6.3 allows this for MVP).
- Groups, layer ordering, copy/paste and full keyboard operation of canvas objects (§6.3, §8) are not built.
- No PNG/PDF export; JSON export only, as the spec recommends for MVP.
