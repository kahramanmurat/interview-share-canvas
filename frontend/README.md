# Interview canvas — front-end prototype

A working front end for the system-design interview platform backed by the FastAPI service.

## Run it

Run the frontend and API together from the repository root:

```bash
make run
# then open http://127.0.0.1:8091/interview-platform.dc.html
```

Build the production static files with Node:

```bash
cd frontend
npm ci
npm run build
```

The build is written to `frontend/dist`. The repository `Dockerfile` copies that
directory into the Python runtime image, where FastAPI serves it alongside the API.

## Files

| File | What it is |
| --- | --- |
| `interview-platform.dc.html` | The whole front end: six screens, one component. Template + logic class in one file. |
| `backend-client.js` | REST and WebSocket client for the FastAPI backend. |
| `modernist.css` | Design tokens and component classes (colors, type, spacing, buttons, table, dialog). |
| `support.js` | Rendering runtime for the component file. Not application code. |

## Screens

Dashboard → create session → live canvas → end → review, plus a candidate lobby and sign-in.
The black **Demo** strip in the bottom-right switches role (interviewer / candidate) and jumps between screens; it is prototype scaffolding, not product UI.

The canvas is real: 28-component palette, drag, resize, marquee multi-select, attached connectors
(elbow / straight / curved, labels, dashed), pen, highlighter, eraser, pan, zoom, zoom-to-fit,
undo/redo, and keyboard shortcuts (`V H C P M E T N`, `⌘Z`, `⇧⌘Z`, `⌘D`, `Delete`).

## Backend integration

`backend-client.js` sends authenticated requests to the same origin and connects to the
collaboration WebSocket. Interviewer tokens are retained in session storage; candidate
collaboration tokens are held in memory for the lifetime of the page.

### Endpoint map (spec §12)

| Client method | HTTP endpoint | Spec |
| --- | --- | --- |
| `api.signIn(email)` | `POST /v1/auth/magic-link` | §6.1 |
| `api.listSessions()` | `GET /v1/sessions` | §6.10 |
| `api.createSession(body)` | `POST /v1/sessions` | §5.1 |
| `api.getSession(id)` | `GET /v1/sessions/{id}` | §11 |
| `api.patchSession(id, patch)` | `PATCH /v1/sessions/{id}` | §6.9 |
| `api.startSession(id)` | `POST /v1/sessions/{id}/start` | §6.1 |
| `api.endSession(id)` | `POST /v1/sessions/{id}/end` | §5.4 |
| `api.duplicateSession(id)` | `POST /v1/sessions/{id}/duplicate` | §6.10 |
| `api.deleteSession(id)` | `DELETE /v1/sessions/{id}` | App extension |
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

Server → client, handled by the client: `room_joined`, `presence_snapshot`, `presence_update`,
`permission_changed`, `session_ended`, `ack`, `resynced`, `status`.
Client → server, accepted by `send()`: `document_update`, `presence_update`, `ping`.

## Runtime behavior

- **Autosave** — debounced 1.2 s writes with saving / saved / not-saved status (§6.8 requires ≤2 s).
- **Server-side authorization** — `saveCanvas` rejects candidate writes when
  `candidate_editing_enabled` is false and after a session ends (§6.9, acceptance criterion 7).
- **Reconnect** — the WebSocket client reconnects automatically; `socket.dropConnection(ms)` exercises it
  (acceptance criterion 5). Triggered from the canvas right panel.
- **Guest links** — 128-bit tokens, only a hash retained, rotate and revoke (§13).
- **Limits** — 10 participants per room, 2,000 elements per document (§14).

## Known gaps

- Collaboration uses last-write-wins snapshots rather than a CRDT, so simultaneous edits can overwrite each other.
- Undo/redo is snapshot-based and global to the tab, not per-participant (§6.3 allows this for MVP).
- Groups, layer ordering, copy/paste and full keyboard operation of canvas objects (§6.3, §8) are not built.
- No PNG/PDF export; JSON export only, as the spec recommends for MVP.
