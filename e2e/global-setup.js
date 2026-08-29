import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const composeFile = resolve(repositoryRoot, "docker-compose.yaml");
const composeProject = process.env.E2E_COMPOSE_PROJECT || "interview-share-canvas-e2e";
const appPort = "18091";
const composeEnvironment = { ...process.env, APP_PORT: appPort };
const composeArguments = ["compose", "-p", composeProject, "-f", composeFile];

function compose(...arguments_) {
  execFileSync("docker", [...composeArguments, ...arguments_], {
    cwd: repositoryRoot,
    env: composeEnvironment,
    stdio: "inherit",
  });
}

function cleanUp() {
  try {
    compose("down", "--volumes", "--remove-orphans");
  } catch (error) {
    console.error("Failed to clean up the Playwright Compose stack.", error);
  }
}

async function waitForApplication() {
  const healthUrl = `http://127.0.0.1:${appPort}/health`;
  const deadline = Date.now() + 60_000;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(healthUrl);
      if (response.ok) return;
    } catch {
      // The container is still starting.
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 500));
  }

  throw new Error(`Application did not become healthy at ${healthUrl}.`);
}

export default async function globalSetup() {
  if (process.env.E2E_REUSE_COMPOSE === "1") {
    await waitForApplication();
    return;
  }

  cleanUp();
  try {
    compose("up", "--build", "--detach", "--wait");
    await waitForApplication();
  } catch (error) {
    cleanUp();
    throw error;
  }

  return cleanUp;
}
