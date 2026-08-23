import { defineConfig, devices } from "@playwright/test";

const externalUrl = process.env.NANOCAT_WEB_URL;
const e2ePort = process.env.NANOCAT_E2E_PORT ?? String(20_000 + Math.floor(Math.random() * 20_000));
process.env.NANOCAT_E2E_PORT = e2ePort;
const baseURL = externalUrl ?? `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 960 } } },
    { name: "tablet", use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } } },
    { name: "mobile", use: { ...devices["iPhone 13"] } },
  ],
});
