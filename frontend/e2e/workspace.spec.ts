import { expect, test, type Page } from "@playwright/test";

async function unlock(page: Page): Promise<void> {
  const authResponse = await page.request.get("/auth/status");
  expect(authResponse.ok()).toBe(true);
  const auth = await authResponse.json() as { protected: boolean; authenticated: boolean };
  if (auth.protected && !auth.authenticated) {
    const password = process.env.NANOCAT_WEB_PASSWORD;
    test.skip(!password, "NANOCAT_WEB_PASSWORD is required for a protected Web instance");
    await page.goto("/login");
    await page.locator("#password").fill(password ?? "");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page).toHaveURL(/\/$/);
  } else {
    await page.goto("/");
  }
  await expect(page.getByRole("heading", { name: "Home" })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".navigator-footer")).toContainText("online", { timeout: 15_000 });
}

async function deleteSessionIfPresent(page: Page, sessionId: string): Promise<void> {
  const result = await page.evaluate(async (id) => {
    const detail = await fetch(`/api/v1/sessions/${encodeURIComponent(id)}`);
    if (detail.status === 404) return { ok: true, status: 404 };
    if (!detail.ok) return { ok: false, status: detail.status };
    const session = await detail.json() as { revision?: number };
    const csrf = document.cookie.split("; ").find((item) => item.startsWith("nanocat_web_csrf="))?.split("=", 2)[1];
    const headers = new Headers();
    if (session.revision !== undefined) headers.set("If-Match", String(session.revision));
    if (csrf) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
    const response = await fetch(`/api/v1/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers,
    });
    return { ok: response.ok, status: response.status };
  }, sessionId);
  expect(result.ok, `session cleanup failed with HTTP ${result.status}`).toBe(true);
}

test("session actions work against the real API", async ({ page }, testInfo) => {
  await unlock(page);
  await page.locator(".command-center").getByRole("button", { name: "New session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+\/conversation/);
  const match = page.url().match(/\/sessions\/([^/]+)/);
  expect(match).not.toBeNull();
  const sessionId = match![1];
  await expect(page.getByRole("tab", { name: /Conversation/ })).toHaveAttribute("aria-selected", "true");
  try {
    await page.getByRole("button", { name: "Session actions" }).click();
    const actions = page.getByRole("dialog", { name: "Session actions" });
    await expect(actions).toBeVisible();
    const renamedTitle = `Renamed regression session ${testInfo.project.name}`;
    await actions.locator("input").fill(renamedTitle);
    await actions.getByRole("button", { name: /Rename/ }).click();
    await expect(page.getByRole("heading", { name: renamedTitle })).toBeVisible();
    await expect(page.locator(".header-connection")).toContainText("online");
    if (testInfo.project.name === "mobile") {
      const navToggle = await page.getByRole("button", { name: "Open sessions" }).boundingBox();
      expect(navToggle?.x).toBeGreaterThanOrEqual(8);
      expect(navToggle?.width).toBeGreaterThanOrEqual(44);
    }
    await expect(page).toHaveScreenshot("session-empty.png", { animations: "disabled" });
    await page.locator("input[type=file]").setInputFiles({ name: "context.txt", mimeType: "text/plain", buffer: Buffer.from("bounded fixture") });
    await expect(page.locator(".attachment-tray")).toContainText("context.txt");
    await page.reload();
    await expect(page.locator(".attachment-tray")).toContainText("context.txt");

    await page.getByRole("button", { name: "Session actions" }).click();
    await actions.locator("input").press("Escape");
    await expect(actions).toBeHidden();
    await expect(page.getByRole("button", { name: "Session actions" })).toBeFocused();
    await page.getByRole("button", { name: "Session actions" }).click();
    await actions.getByRole("button", { name: "Delete session" }).click();
    await actions.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
  } finally {
    await deleteSessionIfPresent(page, sessionId);
  }
});

test("command palette discovers and executes real commands", async ({ page }) => {
  await unlock(page);
  const before = await (await page.request.get("/api/v1/sessions?limit=1000")).json() as { sessions: Array<{ id: string }> };
  await page.keyboard.press("Control+K");
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette).toBeVisible();
  const search = palette.getByRole("combobox");
  await search.press("Tab");
  await expect(search).toBeFocused();
  await search.fill("/help");
  await search.press("Enter");
  await expect(palette.locator(".palette-result")).toContainText("Available commands");
  const after = await (await page.request.get("/api/v1/sessions?limit=1000")).json() as { sessions: Array<{ id: string }> };
  expect(after.sessions.map((session) => session.id)).toEqual(before.sessions.map((session) => session.id));
  await search.press("Escape");
  await expect(palette).toBeHidden();
});

test("command center and responsive navigation match visual baselines", async ({ page }) => {
  await unlock(page);
  await expect(page).toHaveScreenshot("command-center.png", { animations: "disabled" });
  const viewport = page.viewportSize();
  if (viewport && viewport.width < 1280) {
    const opener = viewport.width < 768
      ? page.getByRole("button", { name: "Sessions", exact: true })
      : page.locator(".global-rail").getByRole("button", { name: /sessions/ });
    if (viewport.width < 768) {
      await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toBeVisible();
    }
    await opener.click();
    const drawer = page.getByRole("dialog", { name: "Sessions" });
    await expect(drawer).toBeVisible();
    await expect(page).toHaveScreenshot("drawer-open.png", { animations: "disabled" });
    await page.keyboard.press("Tab");
    expect(await drawer.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
    await expect(page.locator("aside.navigator")).toHaveAttribute("aria-hidden", "true");
    await expect(opener).toBeFocused();
  }
});

test("trajectory inspector supports keyboard navigation and redacted tool detail", async ({ page }) => {
  await unlock(page);
  await page.locator(".command-center").getByRole("button", { name: "New session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+\/conversation/);
  const match = page.url().match(/\/sessions\/([^/]+)/);
  expect(match).not.toBeNull();
  const sessionId = match![1];
  try {
    await page.route(`**/api/v1/sessions/${sessionId}/trajectory*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            { id: "user-1", type: "user.message", source: "web", sequence: 1, timestamp: "2026-08-23T02:42:40Z", status: "queued", turnId: "turn-a", summary: "User message (42 chars)", metadata: { input: { contentChars: 42 } } },
            { id: "thinking-1", type: "assistant.progress", source: "runtime", sequence: 2, timestamp: "2026-08-23T02:42:41Z", status: "running", turnId: "turn-a", summary: "Assistant Progress", metadata: { output: { contentChars: 84 } } },
            { id: "tool-start-1", type: "tool.event", source: "tool", sequence: 3, timestamp: "2026-08-23T02:42:42Z", status: "running", turnId: "turn-a", summary: "Tools: read_file (running)", metadata: { phase: "start", input: { path: "workspace/example.txt", offset: 0, limit: 120 }, output: { toolEvent: { phase: "start", calls: [{ id: "call-1", name: "read_file", status: "running" }] } } } },
            { id: "tool-end-1", type: "tool.event", source: "tool", sequence: 4, timestamp: "2026-08-23T02:42:43Z", status: "running", turnId: "turn-a", summary: "Tools: read_file (ok)", metadata: { phase: "end", output: { content: "Loaded 42 lines from workspace/example.txt", toolEvent: { phase: "end", calls: [{ id: "call-1", name: "read_file", status: "ok" }] } } } },
            { id: "assistant-1", type: "assistant.final", source: "runtime", sequence: 5, timestamp: "2026-08-23T02:42:46Z", status: "completed", turnId: "turn-a", summary: "Assistant response (180 chars)", metadata: { output: { contentChars: 180 }, model: "gpt-4o", usage: { inputTokens: 1200, outputTokens: 180, totalTokens: 1380 } } },
            { id: "user-2", type: "user.message", source: "web", sequence: 6, timestamp: "2026-08-23T02:44:10Z", status: "queued", turnId: "turn-b", summary: "User message (31 chars)", metadata: { input: { contentChars: 31 } } },
            { id: "thinking-2", type: "assistant.progress", source: "runtime", sequence: 7, timestamp: "2026-08-23T02:44:11Z", status: "running", turnId: "turn-b", summary: "Assistant Progress", metadata: { output: { contentChars: 96 } } },
            { id: "tool-start-2", type: "tool.event", source: "tool", sequence: 8, timestamp: "2026-08-23T02:44:12Z", status: "running", turnId: "turn-b", summary: "Tools: grep_file (running)", metadata: { phase: "start", input: { path: "workspace", pattern: "Trajectory" }, output: { toolEvent: { phase: "start", calls: [{ id: "call-2", name: "grep_file", status: "running" }] } } } },
            { id: "tool-end-2", type: "tool.event", source: "tool", sequence: 9, timestamp: "2026-08-23T02:44:14Z", status: "running", turnId: "turn-b", summary: "Tools: grep_file (ok)", metadata: { phase: "end", output: { content: "12 matching lines", toolEvent: { phase: "end", calls: [{ id: "call-2", name: "grep_file", status: "ok" }] } } } },
            { id: "assistant-2", type: "assistant.final", source: "runtime", sequence: 10, timestamp: "2026-08-23T02:44:18Z", status: "completed", turnId: "turn-b", summary: "Assistant response (240 chars)", metadata: { output: { contentChars: 240 }, model: "gpt-4o", usage: { inputTokens: 1500, outputTokens: 240, totalTokens: 1740 } } },
          ],
          historicalItems: [],
          oldestSequence: 1,
          latestSequence: 10,
          historicalTotalCount: 0,
          hasMore: false,
        }),
      });
    });
    await page.reload();
    const conversationTab = page.getByRole("tab", { name: /Conversation/ });
    await conversationTab.focus();
    await conversationTab.press("ArrowRight");
    const trajectoryTab = page.getByRole("tab", { name: /Trajectory/ });
    await expect(trajectoryTab).toHaveAttribute("aria-selected", "true");
    await expect(trajectoryTab).toBeFocused();
    await expect(page.locator(".global-rail").getByRole("link", { name: "Trajectory" })).toHaveCount(0);
    await expect(page.locator(".mobile-tabbar").getByRole("link", { name: "Run" })).toHaveCount(0);
    await expect(page.getByText("Running", { exact: true })).toHaveCount(0);
    await expect(page.locator(".trajectory-table tbody tr")).toHaveCount(8);
    await expect(page).toHaveScreenshot("trajectory-ledger.png", {
      animations: "disabled",
      maxDiffPixels: 40,
    });
    await page.getByRole("button", { name: "Inspect read_file" }).first().click();
    const inspector = page.getByRole("dialog", { name: "Activity inspector" });
    await expect(inspector).toBeVisible();
    const summaryTab = inspector.getByRole("tab", { name: "Summary" });
    await summaryTab.focus();
    await summaryTab.press("ArrowRight");
    await expect(inspector.getByRole("tab", { name: "Input" })).toBeFocused();
    await expect(page).toHaveScreenshot("trajectory-inspector.png", {
      animations: "disabled",
      maxDiffPixels: 40,
    });
    await inspector.getByRole("tab", { name: "Output" }).click();
    await expect(inspector.getByText("Loaded 42 lines from workspace/example.txt")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(inspector).toBeHidden();
    await expect(page).not.toHaveURL(/[?&]node=/);
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}/trajectory`));
    await expect(page).not.toHaveURL(/[?&]node=/);
    await expect(inspector).toBeHidden();
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}/conversation`));
    await expect(inspector).toBeHidden();
  } finally {
    await deleteSessionIfPresent(page, sessionId);
  }
});

test("working feed pairs historical tool input and output", async ({ page }) => {
  await unlock(page);
  await page.locator(".command-center").getByRole("button", { name: "New session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+\/conversation/);
  const match = page.url().match(/\/sessions\/([^/]+)/);
  expect(match).not.toBeNull();
  const sessionId = match![1];
  try {
    await page.route(`**/api/v1/sessions/${sessionId}/conversation*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            { id: "user-1", type: "user.message", role: "user", content: "Inspect the remote workspace", timestamp: "2026-08-23T00:00:00Z", turnId: "turn-1" },
            { id: "work-1", type: "assistant.work", role: "assistant", content: "I will inspect the workspace before continuing.", timestamp: "2026-08-23T00:00:01Z", turnId: "turn-1", metadata: { startedAt: "2026-08-23T00:00:00Z", endedAt: "2026-08-23T00:00:03Z", durationMs: 3000, toolCalls: [{ id: "call-1", name: "ssh_send", arguments: { command: "pwd" } }] } },
            { id: "result-1", type: "tool.result", role: "tool", content: { summary: "Tool execution completed.", status: "completed", redacted: true, preview: { content: "/srv/workspace" } }, timestamp: "2026-08-23T00:00:02Z", turnId: "turn-1", metadata: { toolCallId: "call-1", toolName: "ssh_send" } },
            { id: "assistant-1", type: "assistant.final", role: "assistant", content: "The workspace is ready.", timestamp: "2026-08-23T00:00:03Z", turnId: "turn-1", metadata: { startedAt: "2026-08-23T00:00:00Z", endedAt: "2026-08-23T00:00:03Z", durationMs: 3000 } },
          ],
          totalCount: 4,
          pageOffset: 0,
        }),
      });
    });
    await page.route(`**/api/v1/sessions/${sessionId}/trajectory*`, async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], historicalItems: [], source: "session", cursorKind: "offset", totalCount: 0, pageOffset: 0 }) });
    });
    await page.reload();
    const working = page.locator(".working-feed");
    await expect(working.getByText("Worked for 3s")).toBeVisible();
    await working.getByRole("button", { name: /Worked for/ }).click();
    await expect(working.getByText("ssh_send", { exact: true })).toHaveCount(1);
    await expect(working.getByText("I will inspect the workspace before continuing.")).toBeVisible();
    await working.getByText("Details").click();
    await expect(working.getByText(/"command": "pwd"/)).toBeVisible();
    await expect(working.getByText(/"content": "\/srv\/workspace"/)).toBeVisible();
    await expect(working).not.toContainText("retained in private session history");
    await expect(page).toHaveScreenshot("working-feed.png", { animations: "disabled" });
  } finally {
    await deleteSessionIfPresent(page, sessionId);
  }
});

test("conversation refresh opens at the latest message and short feeds stay bottom aligned", async ({ page }) => {
  await unlock(page);
  await page.locator(".command-center").getByRole("button", { name: "New session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+\/conversation/);
  const match = page.url().match(/\/sessions\/([^/]+)/);
  expect(match).not.toBeNull();
  const sessionId = match![1];
  let longHistory = true;
  try {
    await page.route(`**/api/v1/sessions/${sessionId}/conversation*`, async (route) => {
      const count = longHistory ? 60 : 2;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: Array.from({ length: count }, (_, index) => ({
            id: `message-${index}`,
            type: index % 2 ? "assistant.final" : "user.message",
            role: index % 2 ? "assistant" : "user",
            content: index === count - 1 ? "Latest visible message" : `Conversation message ${index + 1}`,
            timestamp: `2026-08-23T00:${String(index).padStart(2, "0")}:00Z`,
          })),
          totalCount: count,
          pageOffset: 0,
        }),
      });
    });
    await page.route(`**/api/v1/sessions/${sessionId}/trajectory*`, async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], historicalItems: [], source: "session", cursorKind: "offset", totalCount: 0, pageOffset: 0 }) });
    });

    await page.reload();
    await expect(page.getByText("Latest visible message")).toBeVisible();
    const longGap = await page.locator(".conversation-scroll").evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
    expect(longGap).toBeLessThanOrEqual(2);

    longHistory = false;
    await page.reload();
    await expect(page.getByText("Latest visible message")).toBeVisible();
    const shortGap = await page.locator(".conversation-scroll").evaluate((scroll) => {
      const turn = scroll.querySelector(".conversation-turn:last-child");
      if (!(turn instanceof HTMLElement)) return Number.POSITIVE_INFINITY;
      return scroll.getBoundingClientRect().bottom - turn.getBoundingClientRect().bottom;
    });
    expect(shortGap).toBeLessThan(80);
  } finally {
    await deleteSessionIfPresent(page, sessionId);
  }
});

test("unprotected Web settings omit inactive sign-out controls", async ({ page }) => {
  await unlock(page);
  await page.goto("/settings/web");
  await expect(page.getByRole("heading", { name: "Channels" })).toBeVisible();
  await expect(page.getByText("Browser login sessions are inactive.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  await expect(page).toHaveScreenshot("settings-web.png", {
    animations: "disabled",
    mask: [page.getByText(/Runtime override currently applies:/)],
    maskColor: "#ffffff",
  });
  const telegram = page.locator("details.settings-tree-group > summary").filter({ hasText: /^Telegram/ }).locator("..");
  const qq = page.locator("details.settings-tree-group > summary").filter({ hasText: /^Qq/ }).locator("..");
  await expect(telegram).toBeVisible();
  await expect(qq).toBeVisible();
  await telegram.locator(":scope > summary").click();
  const replyToMessage = telegram.getByRole("checkbox", { name: /Reply to message/i });
  await replyToMessage.setChecked(!(await replyToMessage.isChecked()));
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/1 reconnected/)).toBeVisible();
});

test("unprotected runtime redirects the login route to the workspace", async ({ page }) => {
  await page.goto("/login");
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
});

test("password errors respect server Retry-After", async ({ page }) => {
  await page.route("**/auth/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ protected: true, authenticated: false }),
    });
  });
  await page.route("**/auth/login", async (route) => {
    await route.fulfill({
      status: 429,
      headers: { "Content-Type": "application/problem+json", "Retry-After": "3" },
      body: JSON.stringify({ title: "Authentication failed" }),
    });
  });
  await page.goto("/login");
  await expect(page.locator("#password")).toBeVisible();
  await expect(page).toHaveScreenshot("login.png", { animations: "disabled" });
  await page.locator("#password").fill("invalid-password");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("button", { name: /Try again in/ })).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("Try again in");
});
