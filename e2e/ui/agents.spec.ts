import { devices, expect, test, type Page } from "@playwright/test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// The orchestrator card in a real browser: real server, real tmux, real xterm.js, fake agent TUIs.
const VENV_PY = join(homedir(), ".local/share/llm-orchestration/venv/bin/python");
const PYTHON = process.env.ORCH_PYTHON || (existsSync(VENV_PY) ? VENV_PY : "python3");

type ClusterInfo = { desktop: string; agentsRepo: string; desktopState: string };
let proc: ChildProcessWithoutNullStreams;
let info: ClusterInfo;

const linesOf = (agent: string): string[] => {
  const f = join(info.desktopState, `${agent}.lines`);
  return existsSync(f) ? readFileSync(f, "utf8").split("\n").filter(Boolean) : [];
};

async function noHorizontalOverflow(page: Page): Promise<void> {
  const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  expect(sw).toBeLessThanOrEqual(cw + 1);
}

test.beforeAll(async () => {
  proc = spawn(PYTHON, [join(process.cwd(), "e2e", "ui_cluster.py")], { stdio: "pipe" });
  info = await new Promise<ClusterInfo>((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", (d) => {
      buf += d.toString();
      const line = buf.split("\n").find((l) => l.startsWith("{"));
      if (line) resolve(JSON.parse(line));
    });
    proc.on("exit", (code) => reject(new Error(`cluster exited ${code}`)));
    setTimeout(() => reject(new Error("cluster start timeout")), 60_000);
  });
});

test.afterAll(async () => {
  proc?.stdin.end();
  // wait for the cluster to shut down (it ends its tmux terminals first), up to 30 s
  await new Promise((r) => { proc?.once("exit", r); setTimeout(r, 30_000); });
});

test.describe.configure({ mode: "serial" });

test("wide: open a repo, type to Claude in the terminal, watch AGY, hand it the baton", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(info.desktop);
  await expect(page.getByRole("tab", { name: "Agent terminals" })).toHaveAttribute("aria-selected", "true", { timeout: 20_000 });
  await page.getByRole("button", { name: "Open agents for a repo" }).first().click();
  await page.getByRole("button", { name: "agentsrepo" }).click();

  await expect(page.getByRole("tab", { name: "agentsrepo" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("baton: Claude")).toBeVisible();
  const claude = page.getByTestId("terminal-claude");
  await expect(claude).toContainText("claude>", { timeout: 15_000 }); // (the start banner may have rewrapped above the visible area)
  await expect(page.getByText("You can type")).toBeVisible();

  // Real keystrokes through xterm.js into the tmux session
  await claude.click();
  await page.keyboard.type("ECHO typed-through-xterm");
  await page.keyboard.press("Enter");
  await expect(claude).toContainText("typed-through-xterm", { timeout: 10_000 });
  expect(linesOf("claude")).toContain("ECHO typed-through-xterm");

  // The other two are one-line strips, not squashed terminals
  const agyStrip = page.getByTestId("strip-agy");
  await expect(agyStrip).toContainText("AGY · not started");
  await expect(page.getByTestId("strip-local")).toBeVisible();
  await agyStrip.getByRole("button", { name: "Start", exact: true }).click();
  await expect(agyStrip).toContainText(/AGY · (idle|working)/, { timeout: 15_000 });

  // Watching AGY without the baton: typing goes nowhere
  await agyStrip.getByRole("button", { name: /Show AGY|AGY/ }).first().click();
  const agy = page.getByTestId("terminal-agy");
  await expect(agy).toContainText("agy>", { timeout: 15_000 });
  await expect(page.getByText("Watching · no baton")).toBeVisible();
  await agy.click();
  await page.keyboard.type("ECHO should-not-arrive");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1000);
  expect(linesOf("agy").some((l) => l.includes("should-not-arrive"))).toBe(false);

  // Hand the baton over: Claude updates the notes first, then AGY is told to take over
  await page.getByRole("button", { name: "Hand baton here" }).click();
  await expect(page.getByText("baton: AGY")).toBeVisible({ timeout: 30_000 });
  await expect.poll(() => linesOf("agy").some((l) => l.startsWith("Taking over from Claude")), { timeout: 10_000 }).toBe(true);
  await expect(page.getByText("You can type")).toBeVisible();

  await page.getByRole("button", { name: "Shared notes (.orch/context.md)" }).click();
  await expect(page.getByText(/Claude → AGY/)).toBeVisible();
  await page.keyboard.press("Escape");
  await noHorizontalOverflow(page);
});

test("phone: three tabs, the input box types to the baton holder, nothing scrolls sideways", async ({ browser }) => {
  const ctx = await browser.newContext({ ...devices["Pixel 7"] });
  const page = await ctx.newPage();
  await page.goto(info.desktop);
  const tabs = page.getByRole("tablist", { name: "Agents" });
  await expect(tabs).toBeVisible({ timeout: 20_000 });
  await expect(tabs.getByRole("tab")).toHaveCount(3);
  await expect(tabs.getByRole("tab", { name: /AGY ●/ })).toBeVisible();
  const box = page.getByRole("textbox", { name: "Send a line to AGY" });
  await box.fill("ECHO from-the-phone-box");
  await box.press("Enter");
  await expect.poll(() => linesOf("agy").includes("ECHO from-the-phone-box"), { timeout: 10_000 }).toBe(true);
  const send = page.getByRole("button", { name: "Send" });
  const size = await send.boundingBox();
  expect(size!.height).toBeGreaterThanOrEqual(40);
  await noHorizontalOverflow(page);
  await ctx.close();
});
