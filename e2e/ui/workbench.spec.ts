import { devices, expect, test, type Page } from "@playwright/test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createHmac } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// The app runs in its own Python environment (it needs the `webauthn` package).
const VENV_PY = join(homedir(), ".local/share/llm-orchestration/venv/bin/python");
const PYTHON = process.env.ORCH_PYTHON || (existsSync(VENV_PY) ? VENV_PY : "python3");

type ClusterInfo = { desktop: string; tunnel: string; laptop: string; totpSecret: string; desktopRepo: string; laptopRepo: string; tmp: string };

let proc: ChildProcessWithoutNullStreams;
let info: ClusterInfo;

function totp(secretB32: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secretB32.replace(/=+$/, "")) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / 30)));
  const h = createHmac("sha1", key).update(counter).digest();
  const off = h[h.length - 1] & 0xf;
  return String((h.readUInt32BE(off) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

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

// The orchestrator pane opens on the agent terminals (e2e/ui/agents.spec.ts); these tests cover
// the old chat, which stays one tab away until the switch-over.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("orch.mode", "chat"));
});

test.afterAll(async () => {
  proc?.stdin.end();
  // wait for the cluster to shut down (it ends its tmux terminals first), up to 30 s
  await new Promise((r) => { proc?.once("exit", r); setTimeout(r, 30_000); });
});

test.describe("wide screen", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("status strip shows both machines; a model-chosen command waits for your click, and Deny really denies", async ({ page }) => {
    const marker = join(info.tmp, "ui-marker");
    await page.goto(info.desktop);
    const strip = page.getByRole("status", { name: "System status" });
    await expect(strip).toContainText("desktop", { timeout: 20_000 });
    await expect(strip).toContainText("laptop");
    await expect(strip).not.toContainText("offline");

    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill(`RUN: touch ${marker}`);
    await composer.press("Enter");
    const modal = page.getByTestId("approval-modal");
    await expect(modal).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("pill-approvals")).toBeVisible();
    await expect(modal).toContainText(`touch ${marker}`);
    await modal.getByRole("button", { name: "Deny" }).click();
    await expect(modal).toBeHidden();
    await expect(page.getByText(/Denied \(or not approved in time\)/).filter({ visible: true }).first()).toBeVisible({ timeout: 20_000 });
    expect(existsSync(marker)).toBe(false);
  });

  test("conversations: new, rename, pin and archive, and it all survives a reload", async ({ page }) => {
    await page.goto(info.desktop);
    const convos = page.getByRole("region", { name: "Conversations", exact: true });
    await convos.getByRole("button", { name: "New conversation" }).click();
    await page.getByRole("region", { name: "Conversation", exact: true }).getByRole("button", { name: /New conversation/ }).click();
    const title = page.getByRole("textbox", { name: "Conversation name" });
    await title.fill("Release planning");
    await title.press("Enter");
    const row = convos.locator("div.group").filter({ hasText: "Release planning" });
    await expect(row).toBeVisible();

    await row.hover();
    await row.getByRole("button", { name: "Pin" }).click();
    await page.reload();
    const first = page.getByRole("region", { name: "Conversations", exact: true }).locator("div.group").first();
    await expect(first).toContainText("Release planning", { timeout: 20_000 });
    await expect(first.getByRole("button", { name: "Unpin" })).toBeAttached();

    await first.hover();
    await first.getByRole("button", { name: "Archive" }).click();
    await expect(page.getByRole("region", { name: "Conversations", exact: true })).not.toContainText("Release planning");
    await page.reload();
    await expect(page.getByRole("region", { name: "Conversations", exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("region", { name: "Conversations", exact: true })).not.toContainText("Release planning");
  });

  test("repos: unfinished laptop work comes with a recommendation and moves to the desktop in one click", async ({ page }) => {
    writeFileSync(join(info.laptopRepo, "feature.txt"), "started on the laptop\n");
    await page.goto(info.desktop);
    await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "Repos" }).click();
    const board = page.getByTestId("sync-board");
    await board.getByRole("button", { name: "Rescan all machines" }).click();
    const card = board.locator('[data-repo="webapp"]');
    await expect(card).toContainText("Unfinished changes", { timeout: 20_000 });
    await expect(card).toContainText("laptop has newer work. Bring it to desktop?");
    await card.getByRole("button", { name: "Bring to desktop" }).click();
    await expect(page.getByText(/^webapp: /).first()).toBeVisible({ timeout: 30_000 });
    expect(existsSync(join(info.desktopRepo, "feature.txt"))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(board).toBeHidden();
  });

  test("sites shows each machine's services and who stands by", async ({ page }) => {
    await page.goto(info.desktop);
    await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "Sites" }).click();
    const svc = page.locator('[data-service="notes-app"]');
    await expect(svc).toBeVisible({ timeout: 20_000 });
    await expect(svc).toContainText("Notes App");
    await expect(svc).toContainText(/Healthy|Checking/, { timeout: 20_000 });
    await expect(page.getByTestId("services-tab")).toContainText("Backup machine");
  });

  test("text size changes the whole UI and is remembered", async ({ page }) => {
    await page.goto(info.desktop);
    await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "More" }).click();
    await page.getByRole("radio").nth(3).click();
    expect(await page.evaluate(() => document.documentElement.style.fontSize)).toBe("20.8px");
    await page.reload();
    await expect(page.getByRole("status", { name: "System status" })).toBeVisible({ timeout: 20_000 });
    expect(await page.evaluate(() => document.documentElement.style.fontSize)).toBe("20.8px");
    await page.evaluate(() => localStorage.removeItem("orch.textScale"));
  });
});

test.describe("phone in Chrome's 'Desktop site' mode (pretends to be 980px wide)", () => {
  test.use({ viewport: { width: 980, height: 2170 }, screen: { width: 412, height: 915 }, hasTouch: true, isMobile: true, deviceScaleFactor: 1 });

  test("everything scales back up: one column of cards, thumb-sized buttons", async ({ page }) => {
    await page.goto(info.desktop);
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav).toBeVisible({ timeout: 20_000 });
    const rootPx = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
    expect(rootPx).toBeGreaterThan(36); // ~16px x 1.1 x (980/412)
    await nav.getByRole("button", { name: "Repos" }).tap();
    const card = page.locator('[data-repo="webapp"]');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const cardBox = (await card.boundingBox())!;
    expect(cardBox.width).toBeGreaterThan(980 * 0.85); // one column, full width
    const btn = (await page.getByRole("button", { name: "Rescan all machines" }).boundingBox())!;
    expect(btn.height / (980 / 412)).toBeGreaterThanOrEqual(44); // >= 44 real pixels on the phone
    await noHorizontalOverflow(page);
  });
});

// iPhone-sized touch screen (kept on Chromium; the preset would switch browsers)
const { defaultBrowserType: _browser, ...iphone } = devices["iPhone 13"];

test.describe("phone", () => {
  test.use(iphone);

  test("bottom tabs, no sideways scrolling, chat and repos usable with a thumb", async ({ page }) => {
    await page.goto(info.desktop);
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav).toBeVisible({ timeout: 20_000 });
    for (const tab of ["Chat", "Agents", "Repos", "Sites", "More"]) await expect(nav.getByRole("button", { name: tab })).toBeVisible();
    await noHorizontalOverflow(page);

    await page.getByRole("textbox", { name: "Message" }).fill("hello from my phone");
    await page.getByRole("button", { name: "Send" }).tap();
    await expect(page.getByText("echo: hello from my phone").first()).toBeVisible({ timeout: 20_000 });

    await nav.getByRole("button", { name: "Repos" }).tap();
    await expect(page.locator('[data-repo="webapp"]')).toBeVisible({ timeout: 20_000 });
    await noHorizontalOverflow(page);
    const box = await page.getByRole("button", { name: "Rescan all machines" }).boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(43);

    await nav.getByRole("button", { name: "Chat" }).tap();
    await page.getByRole("button", { name: "Conversations and sessions" }).tap();
    await expect(page.getByRole("dialog", { name: "Conversations" })).toBeVisible();
    await page.getByRole("button", { name: "Close Conversations" }).tap();
    await expect(page.getByRole("dialog", { name: "Conversations" })).toBeHidden();
  });

  test("installable: manifest and icons are served", async ({ request }) => {
    const m = await request.get(`${info.desktop}/manifest.webmanifest`);
    expect(m.ok()).toBe(true);
    const manifest = await m.json();
    expect(manifest.display).toBe("standalone");
    expect((await request.get(`${info.desktop}${manifest.icons[1].src}`)).headers()["content-type"]).toContain("image/png");
  });
});

test("passkeys: pair a phone from the desktop, save a passkey, sign in with it later, revoke it", async ({ browser }) => {
  // Desktop (trusted machine) makes a pairing link
  const desk = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await desk.goto(info.desktop);
  await desk.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "More" }).click();
  await desk.getByRole("button", { name: /Devices & sign-in/ }).click();
  await desk.getByRole("button", { name: /Pair a new device/ }).click();
  const pairing = desk.getByTestId("pairing");
  await expect(pairing.getByRole("img", { name: "Pairing QR code" })).toBeVisible();
  const url = (await pairing.locator(".font-mono-code").textContent())!.trim();
  const token = url.split("#pair=")[1];
  expect(token.length).toBeGreaterThan(20);

  // Phone, remote (through the tunnel listener), with a fingerprint-capable authenticator
  const phoneCtx = await browser.newContext({ ...iphone });
  const phone = await phoneCtx.newPage();
  const cdp = await phoneCtx.newCDPSession(phone);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
  });
  const remote = info.tunnel.replace("127.0.0.1", "localhost");
  await phone.goto(`${remote}/#pair=${token}`);
  await phone.getByRole("button", { name: "Save a passkey on this device" }).tap();
  await expect(phone.getByRole("status", { name: "System status" })).toBeVisible({ timeout: 20_000 });
  expect(phone.url()).not.toContain("pair=");
  expect((await cdp.send("WebAuthn.getCredentials", { authenticatorId })).credentials).toHaveLength(1);

  // The same pairing link can't be used twice
  const replay = await phoneCtx.request.post(`${remote}/api/auth/pair/redeem`, { headers: { "X-Orch-Client": "1" }, data: { token } });
  expect(replay.status()).toBe(401);

  // A day later: no cookie, just the passkey
  await phoneCtx.clearCookies();
  await phone.goto(remote);
  await phone.getByRole("button", { name: "Sign in with passkey" }).tap();
  await expect(phone.getByRole("status", { name: "System status" })).toBeVisible({ timeout: 20_000 });

  // The desktop sees the phone and its passkey, and removes the passkey
  await desk.reload();
  await desk.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "More" }).click();
  await desk.getByRole("button", { name: /Devices & sign-in/ }).click();
  const keys = desk.getByRole("region", { name: "Passkeys" });
  await expect(keys).toContainText("iPhone");
  await expect(desk.getByRole("region", { name: "Signed-in devices" })).toContainText("signed in with passkey");
  desk.once("dialog", (d) => void d.accept());
  await keys.getByRole("button", { name: /Remove passkey/ }).first().click();
  await expect(keys).toContainText("None yet.");

  // Removed passkey no longer works
  await phoneCtx.clearCookies();
  await phone.goto(remote);
  await expect(phone.getByRole("button", { name: "Sign in with passkey" })).toBeHidden();
  const status = await (await phoneCtx.request.get(`${remote}/api/auth/status`)).json();
  expect(status.hasPasskeys).toBe(false);
  await phoneCtx.close();
  await desk.close();
});

test("remote (tunnel) visitors: wrong code refused, right code works (fallback)", async ({ page }) => {
  await page.goto(info.tunnel.replace("127.0.0.1", "localhost"));
  await page.getByRole("button", { name: "Other ways to sign in" }).click();
  const code = page.getByRole("textbox").first();
  await expect(code).toBeVisible({ timeout: 20_000 });
  await code.fill("000000");
  await code.press("Enter");
  await expect(page.getByText(/Invalid|already-used/i).first()).toBeVisible({ timeout: 10_000 });
  await code.fill(totp(info.totpSecret));
  await code.press("Enter");
  await expect(page.getByRole("status", { name: "System status" })).toBeVisible({ timeout: 20_000 });
});
