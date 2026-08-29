import { expect, test } from "@playwright/test";

test("candidate canvas changes appear for the interviewer", async ({ browser, baseURL }) => {
  const interviewerContext = await browser.newContext();
  const candidateContext = await browser.newContext();
  const interviewer = await interviewerContext.newPage();
  const candidate = await candidateContext.newPage();
  const uniqueSuffix = Date.now();
  const interviewTitle = `Playwright collaboration ${uniqueSuffix}`;
  const candidateChange = `Candidate queue ${uniqueSuffix}`;

  try {
    // Session 1: sign in explicitly as the interviewer.
    await interviewer.goto("/");
    await expect(interviewer.locator('[data-screen-label="Dashboard"]')).toBeVisible();
    await interviewer.getByRole("button", { name: "Sign in", exact: true }).click();
    await interviewer.getByLabel("Work email").fill("dana@northwind.dev");
    await interviewer.getByRole("button", { name: "Send magic link" }).click();
    await expect(interviewer.locator('[data-screen-label="Dashboard"]')).toBeVisible();

    // Create and start an editable interview, then capture its candidate link.
    await interviewer.getByRole("button", { name: "New interview" }).click();
    await interviewer.getByLabel("Title").fill(interviewTitle);
    await interviewer
      .getByLabel("Problem statement")
      .fill("Verify real-time canvas collaboration with two isolated browser sessions.");
    await interviewer.getByRole("button", { name: "Create draft and link" }).click();
    await expect(interviewer.locator('[data-screen-label="Live canvas"]')).toBeVisible();
    await expect(interviewer.getByText("Candidate link", { exact: true })).toBeVisible();

    const joinLink = await interviewer.locator('.dialog input[readonly]').inputValue();
    expect(joinLink).toMatch(new RegExp(`^${baseURL}/join/[a-f0-9]{32}$`));
    await interviewer.getByRole("button", { name: "Done" }).click();
    await expect(interviewer.getByText("Connected", { exact: true })).toBeVisible();

    // Session 2: join through the shared link as a candidate.
    await candidate.goto(joinLink);
    await expect(candidate.locator('[data-screen-label="Candidate lobby"]')).toBeVisible();
    await candidate.getByLabel("Display name").fill("Playwright Candidate");
    await candidate.getByRole("button", { name: "Join interview" }).click();
    await expect(candidate.locator('[data-screen-label="Live canvas"]')).toBeVisible();
    await expect(candidate.getByText("Connected", { exact: true })).toBeVisible();

    // Place and rename a component from the candidate canvas.
    await candidate.getByTitle("Queue", { exact: true }).click();
    await candidate.getByTestId("canvas-surface").click({ position: { x: 480, y: 280 } });
    await candidate.getByLabel("Label").fill(candidateChange);
    await expect(candidate.getByTestId("canvas-node").filter({ hasText: candidateChange })).toBeVisible();

    // Session 1 receives the candidate's document update over the collaboration socket.
    await expect(
      interviewer.getByTestId("canvas-node").filter({ hasText: candidateChange }),
    ).toBeVisible({ timeout: 15_000 });
  } finally {
    await Promise.allSettled([candidateContext.close(), interviewerContext.close()]);
  }
});
