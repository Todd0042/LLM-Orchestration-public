import { useEffect } from "react";
import { ApprovalModal } from "@/components/ApprovalModal";
import { AuthGate } from "@/components/AuthGate";
import { RepoLocksModal } from "@/components/ops/RepoLocksModal";
import { SettingsModal } from "@/components/SettingsModal";
import { SudoPasswordModal } from "@/components/SudoPasswordModal";
import { Shell } from "@/components/shell/Shell";
import { setOnUnauthorized } from "@/lib/api";
import { useEventStream } from "@/lib/useEventStream";
import { bootstrap, handleEvent, refreshStatus, saveSettings, setConnected, toast } from "@/state/actions";
import { setState, useApp } from "@/state/store";

/**
 * Touch screens get 10% bigger text and controls. Phones in Chrome's "Desktop site" mode pretend
 * the screen is ~980px wide and shrink everything; undo that by the ratio to the real width.
 */
function deviceScale(): number {
  const touch = window.matchMedia("(pointer: coarse)").matches;
  if (!touch) return 1;
  const real = Math.min(window.screen.width, window.screen.height);
  const desktopSiteMode = real > 0 && real < 900 && window.innerWidth > real * 1.25;
  return 1.1 * (desktopSiteMode ? window.innerWidth / real : 1);
}

/** Login gate, startup, live events and the app-wide pop-ups. Layout lives in Shell. */
export default function App() {
  const auth = useApp((s) => s.auth);
  const settings = useApp((s) => s.settings);
  const settingsOpen = useApp((s) => s.settingsOpen);
  const locksOpen = useApp((s) => s.locksOpen);
  const peersStatus = useApp((s) => s.peersStatus);
  const models = useApp((s) => s.models);
  const tools = useApp((s) => s.tools);
  const approvals = useApp((s) => s.approvals);
  const passwordQueue = useApp((s) => s.passwordQueue);
  const textScale = useApp((s) => s.textScale);
  const toastMsg = useApp((s) => s.toast);
  const signedIn = Boolean(auth) && !(auth?.authRequired && !auth.authenticated);

  useEffect(() => {
    setOnUnauthorized(() => setState((s) => ({ auth: s.auth ? { ...s.auth, authenticated: false, authRequired: true } : s.auth })));
    bootstrap().catch((e) => toast(`Can't reach the orchestrator: ${e instanceof Error ? e.message : e}`, "error"));
  }, []);

  useEffect(() => {
    const apply = () => {
      document.documentElement.style.fontSize = `${16 * textScale * deviceScale()}px`;
    };
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, [textScale]);

  useEffect(() => {
    if (!signedIn) return;
    const t = window.setInterval(() => void refreshStatus(), 30_000);
    return () => window.clearInterval(t);
  }, [signedIn]);

  const live = useEventStream(signedIn, handleEvent);
  useEffect(() => setConnected(live.connected), [live.connected]);

  if (auth && !signedIn) {
    return <AuthGate status={auth} onAuthenticated={() => void bootstrap()} />;
  }

  return (
    <div className="fixed inset-0 h-dvh w-full overflow-hidden" style={{ backgroundColor: "var(--bg-canvas)" }}>
      <Shell />

      {settings && settingsOpen && (
        <SettingsModal open settings={settings} peersStatus={peersStatus} models={models} tools={tools}
                       onSave={saveSettings} onClose={() => setState({ settingsOpen: false })} />
      )}
      <RepoLocksModal open={locksOpen} onClose={() => setState({ locksOpen: false })} />
      <SudoPasswordModal promptEvent={passwordQueue[0] ?? null} onClose={() => setState((s) => ({ passwordQueue: s.passwordQueue.slice(1) }))} />
      <ApprovalModal request={passwordQueue.length ? null : approvals[0] ?? null} queued={approvals.length}
                     onDone={(rid) => setState((s) => ({ approvals: s.approvals.filter((a) => a.requestId !== rid) }))} />
      {toastMsg && (
        <div role={toastMsg.tone === "error" ? "alert" : "status"} className="fixed left-1/2 -translate-x-1/2 z-50 max-w-[min(35rem,92vw)] rounded-lg border px-3 py-2 text-sm shadow-xl"
             style={{ bottom: "calc(5rem + env(safe-area-inset-bottom))", backgroundColor: "var(--bg-elevated)", borderColor: toastMsg.tone === "error" ? "var(--accent-crimson)" : "var(--border-strong)" }}
             onClick={() => setState({ toast: null })}>
          {toastMsg.text}
        </div>
      )}
    </div>
  );
}
