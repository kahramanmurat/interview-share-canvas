import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("frontend build contains the application entry points", async () => {
  const assets = [
    "backend-client.js",
    "interview-platform.dc.html",
    "modernist.css",
    "support.js",
  ];

  await Promise.all(assets.map((asset) => access(new URL(`../dist/${asset}`, import.meta.url))));
  const html = await readFile(
    new URL("../dist/interview-platform.dc.html", import.meta.url),
    "utf8",
  );
  assert.match(html, /backend-client\.js/);
  assert.match(html, /data-screen-label/);
});

test("backend client recognizes guest links and retains issued session tokens", async () => {
  const sessionValues = new Map();
  globalThis.window = {
    INTERVIEW_API_BASE: "https://canvas.example.test/",
    location: {
      origin: "https://canvas.example.test",
      pathname: "/join/0123456789abcdef0123456789ABCDEF",
      protocol: "https:",
      host: "canvas.example.test",
    },
  };
  globalThis.sessionStorage = {
    getItem: (key) => sessionValues.get(key) ?? null,
    setItem: (key, value) => sessionValues.set(key, value),
  };

  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/v1/auth/magic-link")) {
      return new Response(JSON.stringify({ user: { id: "user_1" }, expires_in: 900 }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Session-Token": "session-token",
        },
      });
    }
    return Response.json([]);
  };

  const client = await import(`../backend-client.js?test=${Date.now()}`);
  assert.equal(client.guestTokenFromLocation(), "0123456789abcdef0123456789ABCDEF");

  await client.api.signIn("dana@northwind.dev");
  await client.api.listSessions();

  assert.equal(requests[0].url, "https://canvas.example.test/v1/auth/magic-link");
  assert.equal(requests[0].options.credentials, "include");
  assert.equal(requests[1].options.headers.get("Authorization"), "Bearer session-token");
  assert.equal(sessionValues.get("interview_session_token"), "session-token");
});
