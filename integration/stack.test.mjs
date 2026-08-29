import assert from "node:assert/strict";
import test from "node:test";

const baseUrl = (process.env.APPLICATION_URL || "http://127.0.0.1:18091").replace(/\/$/, "");

async function waitUntilHealthy() {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`);
      if (response.ok) return;
    } catch {
      // Compose may still be starting the application container.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Application did not become healthy at ${baseUrl}/health.`);
}

test("Compose stack serves the API and persists an authenticated workflow", async () => {
  await waitUntilHealthy();

  const health = await fetch(`${baseUrl}/health`);
  assert.deepEqual(await health.json(), { status: "ok" });

  const signIn = await fetch(`${baseUrl}/v1/auth/magic-link`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "ci-integration@example.com" }),
  });
  assert.equal(signIn.status, 200);
  const token = signIn.headers.get("x-session-token");
  assert.ok(token);

  const createSession = await fetch(`${baseUrl}/v1/sessions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      title: `CI integration ${Date.now()}`,
      prompt: "Validate the Docker Compose application and PostgreSQL persistence boundary.",
      duration_minutes: 45,
      template_id: "blank",
    }),
  });
  assert.equal(createSession.status, 201);
  const session = await createSession.json();
  assert.ok(session.id);

  const canvas = await fetch(`${baseUrl}/v1/sessions/${session.id}/canvas`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(canvas.status, 200);
  const canvasPayload = await canvas.json();
  assert.ok(canvasPayload.canvas_document_id);
  assert.deepEqual(canvasPayload.doc, { nodes: [], edges: [], strokes: [] });
});
