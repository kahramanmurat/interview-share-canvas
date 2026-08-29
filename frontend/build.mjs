import { copyFile, mkdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const outputDirectory = join(sourceDirectory, "dist");
const assets = [
  "backend-client.js",
  "interview-platform.dc.html",
  "modernist.css",
  "support.js",
];

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });
await Promise.all(
  assets.map((asset) =>
    copyFile(join(sourceDirectory, asset), join(outputDirectory, asset)),
  ),
);
