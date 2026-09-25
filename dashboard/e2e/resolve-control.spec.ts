import { expect, test, type Page } from "@playwright/test";

/**
 * Resolve operator controls (dashboard/src/components/resolve/*), driven
 * against the real FastAPI app + built SPA on :4174 (see
 * dashboard/e2e/server.py's ResolveFixture), not against a mocked API.
 *
 * Each test uses its own issue number so the fixture's per-issue in-memory
 * state machine (dashboard/e2e/server.py) never collides across tests.
 */

const BASE = "http://127.0.0.1:4174";

async function gotoIssue(page: Page, issue: number) {
  await page.goto(`${BASE}/#/repo/self/issue/${issue}`);
  await expect(page.getByRole("heading", { name: new RegExp(`Resolve control #${issue}`) })).toBeVisible();
}

/** Opens the "Request re-arm" form and reviews it (keeping guidance); leaves the operator on the review screen. */
async function reviewRequestRearm(page: Page) {
  await page.getByRole("button", { name: "Request re-arm" }).click();
  await expect(page.getByLabel("Guidance for the next resolve attempt")).toHaveValue("keep");
  await page.getByRole("button", { name: "Review command" }).click();
  await expect(page.getByRole("heading", { name: "Review resolve command" })).toBeVisible();
}

function requestIdFromReview(page: Page) {
  return page.locator('.resolve-operation dt:has-text("Request ID") + dd');
}

test.describe("resolve operator controls", () => {
  test("request re-arm: review shows the request ID, confirm records durable intent (not completion)", async ({ page }) => {
    await gotoIssue(page, 63);

    await reviewRequestRearm(page);
    const requestId = await requestIdFromReview(page).textContent();
    expect(requestId).toBeTruthy();

    await page.getByRole("button", { name: "Confirm command" }).click();
    await expect(page.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await expect(page.getByText(/not necessarily completed/i)).toBeVisible();
    // No unhedged claim of completion or readiness anywhere on the resolved panel.
    const resolvedText = (await page.locator(".resolve-operation").textContent()) ?? "";
    expect(resolvedText).not.toMatch(/\bis (now )?(complete|ready)\b/i);

    await page.getByRole("button", { name: "Close result" }).click();
    const current = page.locator('section[aria-label="Current re-arm request"]');
    await expect(current).toBeVisible();
    await expect(current).toContainText("pending");
    await expect(current.locator("p.mono").first()).toHaveText(requestId!);
  });

  test("lost response is recoverable: survives reload and retries the identical command", async ({ page }) => {
    const issue = 64;
    let postCount = 0;
    const bodies: (string | null)[] = [];
    await page.route(`**/api/repos/self/issues/${issue}/resolve-control/commands`, async (route) => {
      postCount += 1;
      bodies.push(route.request().postData());
      if (postCount === 1) {
        // The server actually processes and commits this one; the browser
        // just never sees the reply.
        await route.fetch();
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });

    await gotoIssue(page, issue);
    await reviewRequestRearm(page);
    const requestId = await requestIdFromReview(page).textContent();

    await page.getByRole("button", { name: "Confirm command" }).click();
    await expect(page.getByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry same command" })).toBeVisible();
    expect(postCount).toBe(1);

    await page.reload();
    await expect(page.getByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();
    await expect(requestIdFromReview(page)).toHaveText(requestId!);
    // Reload restores the uncertain state from session storage; nothing new was sent.
    expect(postCount).toBe(1);

    await page.getByRole("button", { name: "Retry same command" }).click();
    await expect(page.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await expect(page.getByText(/Original command replayed/i)).toBeVisible();
    expect(postCount).toBe(2);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
  });

  test("cancel a pending request, continue it, and look it up by request ID", async ({ page }) => {
    const issue = 65;
    await gotoIssue(page, issue);

    await reviewRequestRearm(page);
    const originalId = await requestIdFromReview(page).textContent();
    await page.getByRole("button", { name: "Confirm command" }).click();
    await page.getByRole("button", { name: "Close result" }).click();

    // Cancel the pending request with a reason.
    await page.getByRole("button", { name: "Cancel pending re-arm" }).click();
    await page.getByLabel("Reason for cancellation").fill("operator asked to stop while investigating");
    await page.getByRole("button", { name: "Review command" }).click();
    await page.getByRole("button", { name: "Confirm command" }).click();
    await page.getByRole("button", { name: "Close result" }).click();
    const current = page.locator('section[aria-label="Current re-arm request"]');
    await expect(current).toContainText("cancelled");
    await expect(current.locator("p.mono").first()).toHaveText(originalId!);

    // Continue it: same original request ID, now pending again.
    await page.getByRole("button", { name: "Continue existing re-arm" }).click();
    await expect(page.locator("form .mono", { hasText: originalId! })).toBeVisible();
    await page.getByRole("button", { name: "Review command" }).click();
    await page.getByRole("button", { name: "Confirm command" }).click();
    await page.getByRole("button", { name: "Close result" }).click();
    await expect(current).toContainText("pending");
    await expect(current.locator("p.mono").first()).toHaveText(originalId!);

    // Historical lookup of the same request ID shows a separate section.
    await page.getByText("Request history and guidance").click();
    await page.getByLabel("Look up request ID").fill(originalId!);
    await page.getByRole("button", { name: "Look up request" }).click();
    const historical = page.locator('section[aria-label="Requested historical re-arm"]');
    await expect(historical).toBeVisible();
    await expect(historical.locator("p.mono").first()).toHaveText(originalId!);
    await expect(current).toBeVisible();
  });

  test("issue held with inactive resolve authority: legacy unblock is reviewable and posts /unblock", async ({ page }) => {
    const issue = 61;
    await gotoIssue(page, issue);
    await expect(page.getByText("Resolve authority is inactive for this deployment.")).toBeVisible();

    let unblockCalled = false;
    await page.route(`**/api/repos/self/issues/${issue}/unblock`, async (route) => {
      unblockCalled = true;
      await route.continue();
    });

    await page.getByRole("button", { name: "Review legacy unblock" }).click();
    const dialog = page.getByRole("dialog", { name: "Unblock issue" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: `Unblock #${issue}` }).click();
    await expect(dialog).toHaveCount(0);
    expect(unblockCalled).toBe(true);
  });

  test("issue with unavailable resolve control: no unblock affordance anywhere", async ({ page }) => {
    const issue = 62;
    await gotoIssue(page, issue);
    await expect(page.getByText(/Resolve control unavailable/)).toBeVisible();
    await expect(page.getByRole("button", { name: /unblock/i })).toHaveCount(0);
    await expect(page.getByText(/unblock/i)).toHaveCount(0);
  });

  test("the request-rearm flow is completable with keyboard only", async ({ page }) => {
    const issue = 66;
    await gotoIssue(page, issue);

    // Enter the panel via focus (equivalent to having tabbed there from the
    // rest of the page); everything from here on is Tab/Enter only.
    await page.getByRole("button", { name: "Request re-arm" }).focus();
    await page.keyboard.press("Enter");
    // The guidance select receives autofocus on open.
    await expect(page.getByLabel("Guidance for the next resolve attempt")).toBeFocused();
    await page.keyboard.press("Tab"); // -> "Review command"
    await expect(page.getByRole("button", { name: "Review command" })).toBeFocused();
    await page.keyboard.press("Enter");

    const heading = page.getByRole("heading", { name: "Review resolve command" });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();

    await page.keyboard.press("Tab"); // heading has tabIndex=-1, skipped by Tab -> "Confirm command"
    await expect(page.getByRole("button", { name: "Confirm command" })).toBeFocused();
    await page.keyboard.press("Enter");

    await expect(page.getByRole("heading", { name: "Command recorded" })).toBeVisible();
    await page.getByRole("button", { name: "Close result" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Command recorded" })).toHaveCount(0);
  });

  test("resolve panel has no horizontal overflow and long IDs wrap, across viewport widths", async ({ page }) => {
    const issue = 68;
    const widths = [320, 768, 1024, 1440];

    const assertNoOverflow = async () => {
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
    };

    await page.setViewportSize({ width: widths[0], height: 900 });
    await gotoIssue(page, issue);

    await reviewRequestRearm(page);
    await page.getByRole("button", { name: "Confirm command" }).click();
    await page.getByRole("button", { name: "Close result" }).click();
    await expect(page.locator('section[aria-label="Current re-arm request"]')).toContainText("pending");

    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await assertNoOverflow();
      await page.screenshot({ path: test.info().outputPath(`pending-${width}.png`), fullPage: true });
    }

    // Move into an "uncertain" state (lost response) and repeat across widths.
    let postCount = 0;
    await page.route(`**/api/repos/self/issues/${issue}/resolve-control/commands`, async (route) => {
      postCount += 1;
      if (postCount === 1) {
        await route.fetch();
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });
    await page.getByRole("button", { name: "Cancel pending re-arm" }).click();
    await page.getByLabel("Reason for cancellation").fill("checking layout under an unresolved command");
    await page.getByRole("button", { name: "Review command" }).click();
    await page.getByRole("button", { name: "Confirm command" }).click();
    await expect(page.getByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();

    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await assertNoOverflow();
      await page.screenshot({ path: test.info().outputPath(`uncertain-${width}.png`), fullPage: true });
    }
  });
});
