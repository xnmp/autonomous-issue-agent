import { expect, test, type Page } from "@playwright/test";

/**
 * PR-scoped review-rejection controls (dashboard/src/components/review/*),
 * driven through the real FastAPI app and built SPA on :4174 against
 * dashboard/e2e/server.py's ReviewFixture. Issue N links pull request N + 100;
 * each test owns its own pull request so the in-memory fixture never collides.
 * `/fixture/review-drain/{pr}` stands in for the review daemon's next drain.
 */

const BASE = "http://127.0.0.1:4174";
const HEADERS = { "X-Issue-Agent-Dashboard": "1" };

async function openReview(page: Page, issue: number) {
  await page.goto(`${BASE}/#/repo/self/issue/${issue}`);
  const panel = page.getByRole("region", { name: new RegExp(`Review control PR #${issue + 100}`) });
  await expect(panel).toBeVisible();
  return panel;
}

async function drain(page: Page, pr: number) {
  const response = await page.request.post(`${BASE}/fixture/review-drain/${pr}`, { headers: HEADERS });
  expect(response.ok()).toBe(true);
}

test.describe("review operator controls", () => {
  test("renewing the selected exhausted episode once completes projection and preserves its sibling hold", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const panel = await openReview(page, 94);
    await expect(panel.getByText(/Failed rounds 5\/5/)).toBeVisible();
    await panel.getByRole("button", { name: "Renew execution allowance" }).click();
    await panel.getByRole("button", { name: "Review command" }).click();
    const posted = page.waitForRequest("**/api/repos/self/pulls/194/review-control/commands");
    await panel.getByRole("button", { name: "Confirm command" }).click();
    expect((await posted).postDataJSON()).toMatchObject({ operation: "renew_execution",
      execution_epoch: 7, execution_episode: "execution-194", guidance: { kind: "keep" } });
    await expect(panel.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await panel.getByRole("button", { name: "Close result" }).click();
    const current = panel.getByRole("region", { name: "Current re-arm request" });
    await expect(current).toContainText("pending");
    await expect(current.getByRole("listitem")).toHaveCount(1);
    await expect(panel.getByText(/Failed rounds 0\/5/)).toBeVisible();
    await drain(page, 194);
    await panel.getByRole("button", { name: "Refresh status" }).click();
    await expect(current).toContainText("Every obligation is verified");
    await expect(panel.getByText(/independent sibling hold/)).toBeVisible();
    await expect(panel.getByRole("button", { name: "Renew execution allowance" })).toHaveCount(0);
    const response = await page.request.get(`${BASE}/api/repos/self/pulls/194/review-control`);
    expect((await response.json()).execution.execution_episode).toBe("renewed-194-1");
    for (const width of [320, 768]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    }
    expect(errors).toEqual([]);
    await page.screenshot({ path: test.info().outputPath("renewal-completed-sibling-hold.png"), fullPage: true });
  });

  test("lost renewal reply and reload replay the original captured request without another grant", async ({ page }) => {
    const bodies: (string | null)[] = [];
    await page.route("**/api/repos/self/pulls/195/review-control/commands", async (route) => {
      bodies.push(route.request().postData());
      if (bodies.length === 1) { await route.fetch(); await route.abort("failed"); }
      else await route.continue();
    });
    let panel = await openReview(page, 95);
    await panel.getByRole("button", { name: "Renew execution allowance" }).click();
    await panel.getByRole("button", { name: "Review command" }).click();
    await panel.getByRole("button", { name: "Confirm command" }).click();
    await expect(panel.getByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();
    await page.reload();
    panel = page.getByRole("region", { name: /Review control PR #195/ });
    await expect(panel.getByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();
    expect(bodies).toHaveLength(1);
    await panel.getByRole("button", { name: "Retry same command" }).click();
    await expect(panel.getByText(/Original command replayed/)).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    const control = await (await page.request.get(`${BASE}/api/repos/self/pulls/195/review-control`)).json();
    expect(control.execution.execution_episode).toBe("renewed-195-1");
    expect(control.current_intent.authorized_reviews).toEqual([]);
  });

  test("stale renewal confirmation reports conflict and grants no new episode", async ({ page }) => {
    const panel = await openReview(page, 96);
    await panel.getByRole("button", { name: "Renew execution allowance" }).click();
    await panel.getByRole("button", { name: "Review command" }).click();
    expect((await page.request.post(`${BASE}/fixture/review-revision/196`, { headers: HEADERS })).ok()).toBe(true);
    await panel.getByRole("button", { name: "Confirm command" }).click();
    await expect(panel.getByRole("heading", { name: "Command not accepted" })).toBeVisible();
    await expect(panel.getByText(/workflow revision changed/)).toBeVisible();
    const control = await (await page.request.get(`${BASE}/api/repos/self/pulls/196/review-control`)).json();
    expect(control.execution.execution_episode).toBe("execution-196");
    expect(control.execution.failures).toBe(5);
    expect(control.current_intent).toBeNull();
  });

  test("blocked history names the source, has no legacy reset and remains unchanged after polling", async ({ page }) => {
    await page.route("**/api/repos/self/workflow-families", (route) => route.fulfill({ status: 200,
      contentType: "application/json", body: JSON.stringify({ status: "available", repository: "o/r",
        active_families: ["review_rejections"] }) }));
    const before = await (await page.request.get(`${BASE}/api/repos/self/pulls/197/review-control`)).json();
    const panel = await openReview(page, 97);
    await expect(panel.getByRole("heading", { name: "Review execution migration blocked" })).toBeVisible();
    await expect(panel.getByText("unassigned-review-counter")).toBeVisible();
    await expect(panel.getByRole("button", { name: /Renew execution|Release selected/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Review legacy unblock" })).toBeDisabled();
    for (let count = 0; count < 3; count += 1) await panel.getByRole("button", { name: "Refresh status" }).click();
    const after = await (await page.request.get(`${BASE}/api/repos/self/pulls/197/review-control`)).json();
    expect(after).toEqual(before);
  });

  test("selected verdict release completes without renewing execution or clearing a sibling hold", async ({ page }) => {
    const panel = await openReview(page, 98);
    await panel.getByRole("button", { name: "Release selected verdict hold" }).click();
    await panel.getByRole("button", { name: "Review command" }).click();
    const posted = page.waitForRequest("**/api/repos/self/pulls/198/review-control/commands");
    await panel.getByRole("button", { name: "Confirm command" }).click();
    expect((await posted).postDataJSON()).toMatchObject({ operation: "release_verdict",
      execution_episode: "execution-198", submission_command_id: "confirmed-198" });
    await expect(panel.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await panel.getByRole("button", { name: "Close result" }).click();
    await drain(page, 198);
    await panel.getByRole("button", { name: "Refresh status" }).click();
    await expect(panel.getByRole("region", { name: "Current re-arm request" })).toContainText("Every obligation is verified");
    await expect(panel.getByText(/Failed rounds 5\/5/)).toBeVisible();
    await expect(panel.getByText(/independent sibling hold/)).toBeVisible();
    await expect(panel.getByRole("button", { name: "Release selected verdict hold" })).toHaveCount(0);
  });

  test("re-arming a ceiling records pending dismissals, then shows completion once the daemon drains", async ({ page }) => {
    const panel = await openReview(page, 81);
    await expect(panel.getByText("Review rejection ceiling")).toBeVisible();
    await expect(panel.getByText(/2 counted bot rejections \/ 2 allowed/)).toBeVisible();

    await panel.getByRole("button", { name: "Request re-arm" }).click();
    const form = panel.getByRole("group", { name: "Request re-arm" });
    await expect(form).toContainText("Authorizes dismissal of exactly the 2 counted bot reviews");
    await form.getByLabel("Guidance for the next paid review").selectOption("set");
    await form.getByLabel("New guidance").fill("Focus on the parser edge case.");
    await form.getByRole("button", { name: "Review command" }).click();
    await expect(panel.getByRole("heading", { name: "Review command" })).toBeFocused();

    const posted = page.waitForRequest("**/api/repos/self/pulls/181/review-control/commands");
    await panel.getByRole("button", { name: "Confirm command" }).click();
    expect((await posted).postDataJSON()).toMatchObject({
      operation: "request_rearm", gate_id: "review-ceiling-181",
      guidance: { kind: "set", text: "Focus on the parser edge case." },
    });
    await expect(panel.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await expect(panel.getByText(/not necessarily completed/)).toBeVisible();
    await panel.getByRole("button", { name: "Close result" }).click();

    const current = panel.getByRole("region", { name: "Current re-arm request" });
    await expect(current).toContainText("pending");
    const obligations = current.getByRole("list", { name: "Re-arm obligations" }).getByRole("listitem");
    await expect(obligations).toHaveCount(3);
    await expect(obligations.last()).toContainText("Relabel: remove needs-human-review, add ai-pr-open pending");
    // The daemon's recorded decision, as of its read: the open ceiling gate, not its projected label, holds dispatch.
    await expect(panel.getByText(/Review daemon, as of 2026-09-26T09:55:00\+00:00: Review dispatch is held by 1 gate/)).toBeVisible();

    await drain(page, 181);
    await panel.getByRole("button", { name: "Refresh status" }).click();
    await expect(current).toContainText("completed");
    await expect(current).toContainText("Every obligation is verified");
    await expect(panel.getByText("No recorded review holds")).toBeVisible();
    await expect(panel.getByText(/Review daemon, as of 2026-09-26T10:05:00\+00:00: A fresh review is owed and passed this tick’s dispatch admission/)).toBeVisible();
    await expect(panel.getByText("Review rejection ceiling")).toHaveCount(0);
  });

  test("authorizing one retry records a single grant that the drain consumes", async ({ page }) => {
    const panel = await openReview(page, 82);
    await expect(panel.getByText("Retry authorization required")).toBeVisible();
    await panel.getByRole("button", { name: "Authorize one retry" }).click();
    const form = panel.getByRole("group", { name: "Authorize one retry" });
    await expect(form).toContainText("exactly one more execution of command dismiss-182");
    await form.getByRole("button", { name: "Review command" }).click();
    const authorization = await panel.locator('.resolve-operation dt:has-text("Authorization ID") + dd').textContent();

    const posted = page.waitForRequest("**/api/repos/self/pulls/182/review-control/commands");
    await panel.getByRole("button", { name: "Confirm command" }).click();
    expect((await posted).postDataJSON()).toMatchObject({
      operation: "authorize_retry", authorization_id: authorization, gate_id: "review-retry-182",
      command_id: "dismiss-182", evidence_id: "absence-182",
    });
    await panel.getByRole("button", { name: "Close result" }).click();

    await expect(panel.getByText(/One retry authorized, waiting for the executor/)).toContainText(authorization!);
    const second = panel.getByRole("button", { name: "Authorize one retry" });
    await expect(second).toBeDisabled();
    await expect(second).toHaveAccessibleDescription(new RegExp(`${authorization} is already waiting for the executor`));

    await drain(page, 182);
    await panel.getByRole("button", { name: "Refresh status" }).click();
    await expect(panel.getByText("Retry authorization required")).toHaveCount(0);
    await expect(panel.getByText("No recorded review holds")).toBeVisible();
  });

  test("a refused re-arm shows why and can never be sent", async ({ page }) => {
    const posts: string[] = [];
    page.on("request", (request) => { if (request.method() === "POST") posts.push(request.url()); });
    const panel = await openReview(page, 83);
    await expect(panel.getByText(/70 counted bot rejections/)).toBeVisible();
    const request = panel.getByRole("button", { name: "Request re-arm" });
    await expect(request).toBeDisabled();
    await expect(request).toHaveAccessibleDescription(/70 counted reviews exceed the 63-dismissal bound of one re-arm/);
    await expect(panel.getByText(/dismiss some on GitHub so reconciliation can refresh the ceiling/)).toBeVisible();
    await request.click({ force: true });
    await expect(panel.getByRole("group", { name: "Request re-arm" })).toHaveCount(0);
    expect(posts).toEqual([]);
  });

  test("the label-based unblock is shown retired while the review family is active", async ({ page }) => {
    // The repository's family read, as the server's legacy refusal reads it (the fixture backend reports none).
    await page.route("**/api/repos/self/workflow-families", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ status: "available", repository: "o/r", active_families: ["review_rejections"] }),
    }));
    await openReview(page, 84);
    const legacy = page.getByRole("button", { name: "Review legacy unblock" });
    await expect(legacy).toBeDisabled();
    await expect(legacy).toHaveAccessibleDescription(/label-based unblock is retired\. Review holds on PR #184 are shown in its review control below\./);
  });

  test("the review panel fits a phone: no horizontal overflow, long review ids wrap, actions reachable", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 900 });
    const panel = await openReview(page, 85);
    await panel.getByText("Counted reviews (2)").click();
    await expect(panel.getByText("PRR_kwDOfixture185review000")).toBeVisible();
    await panel.getByRole("button", { name: "Request re-arm" }).click();
    for (const width of [320, 360, 768]) {
      await page.setViewportSize({ width, height: 900 });
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
      const box = await panel.getByRole("button", { name: "Review command" }).boundingBox();
      expect(box && box.x >= 0 && box.x + box.width <= width).toBe(true);
    }
  });
});

test("existing control panel shows selected recovery debt and owed effects without reset controls", async ({ page }) => {
  const panel = await openReview(page, 99);
  await expect(panel.getByRole("heading", { name: "Fix recovery", exact: true })).toBeVisible();
  await expect(panel).toContainText("PR repair · generation 3 · revision 9");
  const debt = panel.locator(".resolve-history").filter({ has: page.getByRole("heading", { name: "Fix recovery", exact: true }) });
  await expect(debt.locator("dd")).toHaveText(["4"]);
  await expect(debt).toContainText("PR continuity held.");
  await expect(debt).toContainText("Awaiting verified completion: ci rerun, branch refresh.");
  await expect(debt.getByRole("button")).toHaveCount(0);
  const issuePanel = page.getByRole("region", { name: /Resolve control #99/ });
  await expect(issuePanel).toContainText("Resolve authority is inactive");
  await expect(issuePanel.locator(".resolve-facts dd")).toHaveText(["4", "3", "2", "5", "6", "1 / 3"]);
  await expect(issuePanel).toContainText("Recovery holds: machine, budget.");
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  }
  await page.screenshot({ path: test.info().outputPath("selected-recovery-debt.png"), fullPage: true });
});

test("unreadable recovery authority is visible without invented zero debt", async ({ page }) => {
  const panel = await openReview(page, 98);
  await expect(panel).toContainText("Selected PR repair source is unreadable. Recovery debt cannot be shown.");
  const debt = panel.locator(".resolve-history").filter({ has: page.getByRole("heading", { name: "Fix recovery", exact: true }) });
  await expect(debt.locator("dd")).toHaveCount(0);
});
