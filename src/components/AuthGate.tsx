import { useEffect, useState } from "react";
import { Fingerprint, KeyRound, Link2, Loader2, ShieldCheck } from "lucide-react";
import type { AuthStatus } from "@/types/orch";
import { loginTotp, pairRedeem, passkeyLoginOptions, passkeyLoginVerify, passkeyRegisterOptions, passkeyRegisterVerify } from "@/lib/api";
import { createPasskey, describePasskeyError, getPasskey, passkeysSupported } from "@/lib/passkey";

interface AuthGateProps {
  status: AuthStatus;
  onAuthenticated: () => void;
}

type Step = "signin" | "pairing" | "offer-passkey" | "code";

function pairTokenFromUrl(): string {
  const m = window.location.hash.match(/pair=([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}

/**
 * Sign-in: passkey (fingerprint/face) first. A pairing link from one of your machines signs a new
 * device in once and then offers to save a passkey on it. The 6-digit code is a hidden fallback.
 */
export function AuthGate({ status, onAuthenticated }: AuthGateProps) {
  const [step, setStep] = useState<Step>(() => (pairTokenFromUrl() ? "pairing" : "signin"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const canPasskey = passkeysSupported() && Boolean(status.passkeyCapable);

  useEffect(() => {
    const token = pairTokenFromUrl();
    if (!token) return;
    history.replaceState(null, "", window.location.pathname + window.location.search); // never leave the token in the address bar
    pairRedeem(token)
      .then(() => (canPasskey ? setStep("offer-passkey") : onAuthenticated()))
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
        setStep("signin");
      });
  }, []);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(describePasskeyError(e));
    } finally {
      setBusy(false);
    }
  };

  const signInWithPasskey = () =>
    run(async () => {
      const { challengeId, options } = await passkeyLoginOptions();
      await passkeyLoginVerify(challengeId, await getPasskey(options));
      onAuthenticated();
    });

  const savePasskey = () =>
    run(async () => {
      const { challengeId, options } = await passkeyRegisterOptions();
      await passkeyRegisterVerify(challengeId, await createPasskey(options));
      onAuthenticated();
    });

  const submitCode = (e: React.FormEvent) => {
    e.preventDefault();
    const clean = code.replace(/\s+/g, "");
    if (clean.length !== 6) return setError("Enter the 6-digit code.");
    void run(async () => {
      await loginTotp(clean);
      onAuthenticated();
    });
  };

  const primary = "tap w-full inline-flex items-center justify-center gap-2 rounded-xl px-4 py-3 text-base font-semibold cursor-pointer disabled:opacity-50";
  const link = "text-sm underline cursor-pointer";

  return (
    <div className="fixed inset-0 h-dvh w-full flex items-center justify-center p-4" style={{ backgroundColor: "var(--bg-canvas)", color: "var(--text-primary)" }}>
      <main className="w-full max-w-sm rounded-2xl border p-6 space-y-5 shadow-2xl" style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-strong)" }} aria-labelledby="signin-title">
        <div className="flex items-center gap-2">
          <ShieldCheck className="w-6 h-6" style={{ color: "var(--accent-primary)" }} />
          <h1 id="signin-title" className="text-lg font-semibold">LLM-Orchestration</h1>
        </div>

        {step === "pairing" && (
          <p className="flex items-center gap-2 text-sm" style={{ color: "var(--text-secondary)" }}>
            <Loader2 className="w-4 h-4 animate-spin" /> Signing this device in…
          </p>
        )}

        {step === "offer-passkey" && (
          <div className="space-y-4">
            <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
              You're in. Save a passkey on this device so next time it's just your fingerprint or face: no codes, no apps.
            </p>
            <button type="button" disabled={busy} onClick={() => void savePasskey()} className={primary} style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>
              {busy ? <Loader2 className="w-5 h-5 animate-spin" /> : <Fingerprint className="w-5 h-5" />} Save a passkey on this device
            </button>
            <button type="button" onClick={onAuthenticated} className={link} style={{ color: "var(--text-muted)" }}>Not now</button>
          </div>
        )}

        {step === "signin" && (
          <div className="space-y-4">
            {canPasskey && status.hasPasskeys ? (
              <button type="button" disabled={busy} onClick={() => void signInWithPasskey()} className={primary} style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }} autoFocus>
                {busy ? <Loader2 className="w-5 h-5 animate-spin" /> : <Fingerprint className="w-5 h-5" />} Sign in with passkey
              </button>
            ) : (
              <div className="rounded-xl border p-3 text-sm space-y-1.5" style={{ borderColor: "var(--border-subtle)", color: "var(--text-secondary)" }}>
                <p className="flex items-center gap-2 font-medium" style={{ color: "var(--text-primary)" }}><Link2 className="w-4 h-4" /> New device?</p>
                <p>On your desktop or laptop, open the app → More → Devices → <b>Pair a new device</b>, then scan the QR code with this device.</p>
                {!canPasskey && <p className="text-xs">Passkeys need the secure (https://) address you set up for remote access.</p>}
              </div>
            )}
            <button type="button" onClick={() => setStep("code")} className={link} style={{ color: "var(--text-muted)" }}>Other ways to sign in</button>
          </div>
        )}

        {step === "code" && (
          <form onSubmit={submitCode} className="space-y-3">
            <label className="block text-sm" htmlFor="code" style={{ color: "var(--text-secondary)" }}>
              <KeyRound className="inline w-4 h-4 mr-1" /> 6-digit authenticator code
            </label>
            <input id="code" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={7} autoFocus
                   className="w-full rounded-xl border px-3 py-3 text-center text-xl tracking-[0.4em] font-mono-code bg-transparent" style={{ borderColor: "var(--border-strong)" }} />
            <button type="submit" disabled={busy} className={primary} style={{ backgroundColor: "var(--bg-elevated)", border: "1px solid var(--border-strong)" }}>
              {busy && <Loader2 className="w-4 h-4 animate-spin" />} Sign in
            </button>
            <button type="button" onClick={() => setStep("signin")} className={link} style={{ color: "var(--text-muted)" }}>Back</button>
          </form>
        )}

        {error && <p role="alert" className="text-sm" style={{ color: "var(--accent-crimson)" }}>{error}</p>}
      </main>
    </div>
  );
}
