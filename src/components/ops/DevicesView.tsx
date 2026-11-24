import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Fingerprint, Link2, Smartphone, Trash2 } from "lucide-react";
import { fetchDevices, pairStart, passkeyRegisterOptions, passkeyRegisterVerify, revokeDevice, type DevicesInfo } from "@/lib/api";
import { createPasskey, describePasskeyError, passkeysSupported } from "@/lib/passkey";
import { toast } from "@/state/actions";
import { IconButton } from "@/components/ui/IconButton";

function when(ts: number): string {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

/** Passkeys and signed-in devices: add a passkey here, pair a new device by QR code, remove anything. */
export function DevicesView() {
  const [info, setInfo] = useState<DevicesInfo | null>(null);
  const [pair, setPair] = useState<{ url: string; qr: string; until: number } | null>(null);
  const [now, setNow] = useState(Date.now());

  const load = () => fetchDevices().then(setInfo).catch((e) => toast(String(e), "error"));
  useEffect(() => {
    void load();
  }, []);
  useEffect(() => {
    if (!pair) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [pair]);

  const addPasskey = async () => {
    try {
      const { challengeId, options } = await passkeyRegisterOptions();
      await passkeyRegisterVerify(challengeId, await createPasskey(options));
      toast("Passkey saved on this device");
      void load();
    } catch (e) {
      toast(describePasskeyError(e), "error");
    }
  };

  const startPair = async () => {
    const p = await pairStart();
    setPair({ url: p.url, qr: await QRCode.toDataURL(p.url, { margin: 1, width: 240 }), until: Date.now() + p.expiresInSec * 1000 });
  };

  const remove = async (kind: "passkey" | "session", id: string, label: string) => {
    if (!window.confirm(`Remove ${label}? ${kind === "passkey" ? "It won't be able to sign in anymore." : "That device will be signed out."}`)) return;
    await revokeDevice(kind, id);
    toast(`${label} removed`);
    void load();
  };

  const secsLeft = pair ? Math.max(0, Math.round((pair.until - now) / 1000)) : 0;

  return (
    <div className="p-3 sm:p-4 space-y-4 max-w-2xl" data-testid="devices-view">
      <div className="grid gap-2 sm:grid-cols-2">
        <button type="button" onClick={() => void startPair()} className="tap rounded-xl border p-3 text-left cursor-pointer" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
          <span className="flex items-center gap-2 text-sm font-semibold"><Link2 className="w-4 h-4" style={{ color: "var(--accent-primary)" }} /> Pair a new device</span>
          <span className="block text-xs mt-1" style={{ color: "var(--text-muted)" }}>Shows a QR code. Scan it with your phone to sign it in once, then save a passkey there.</span>
        </button>
        <button type="button" onClick={() => void addPasskey()} disabled={!passkeysSupported() || !info?.passkeyCapable}
                className="tap rounded-xl border p-3 text-left cursor-pointer disabled:opacity-50" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
          <span className="flex items-center gap-2 text-sm font-semibold"><Fingerprint className="w-4 h-4" style={{ color: "var(--accent-primary)" }} /> Save a passkey on this device</span>
          <span className="block text-xs mt-1" style={{ color: "var(--text-muted)" }}>
            {info?.passkeyCapable ? "Sign in with your fingerprint or face from now on." : "Open the app at its https address to use passkeys."}
          </span>
        </button>
      </div>

      {pair && (
        <div className="rounded-xl border p-4 flex flex-col sm:flex-row items-center gap-4" style={{ borderColor: "var(--accent-primary)", backgroundColor: "var(--bg-panel)" }} data-testid="pairing">
          {secsLeft > 0 ? (
            <>
              <img src={pair.qr} alt="Pairing QR code" className="w-48 h-48 rounded-lg bg-white p-1" />
              <div className="space-y-2 text-sm min-w-0">
                <p>Scan with your phone's camera. Works once, for {Math.floor(secsLeft / 60)}:{String(secsLeft % 60).padStart(2, "0")} more.</p>
                <p className="text-xs break-all font-mono-code" style={{ color: "var(--text-muted)" }}>{pair.url}</p>
                <button type="button" className="text-xs underline cursor-pointer" onClick={() => void navigator.clipboard?.writeText(pair.url).then(() => toast("Link copied"))}>Copy link</button>
              </div>
            </>
          ) : (
            <p className="text-sm">That code expired. <button type="button" className="underline cursor-pointer" onClick={() => void startPair()}>Make a new one</button></p>
          )}
        </div>
      )}

      <section className="space-y-1.5" aria-label="Passkeys">
        <h3 className="text-sm font-semibold">Passkeys</h3>
        {!info?.passkeys.length && <p className="text-xs" style={{ color: "var(--text-muted)" }}>None yet.</p>}
        {info?.passkeys.map((p) => (
          <div key={p.id} className="flex items-center gap-3 rounded-xl border px-3 py-2" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
            <Fingerprint className="w-4 h-4 shrink-0" style={{ color: "var(--accent-primary)" }} />
            <div className="flex-1 min-w-0 text-sm">
              <div className="truncate font-medium">{p.name}</div>
              <div className="text-xs" style={{ color: "var(--text-muted)" }}>added {when(p.createdAt)} · last used {when(p.lastUsedAt)}</div>
            </div>
            <IconButton label={`Remove passkey ${p.name}`} onClick={() => void remove("passkey", p.id, p.name)}><Trash2 className="w-4 h-4" /></IconButton>
          </div>
        ))}
      </section>

      <section className="space-y-1.5" aria-label="Signed-in devices">
        <h3 className="text-sm font-semibold">Signed-in devices</h3>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>Each stays signed in for 30 days after it was last used. Your own machines don't need this.</p>
        {!info?.sessions.length && <p className="text-xs" style={{ color: "var(--text-muted)" }}>None.</p>}
        {info?.sessions.map((s) => (
          <div key={s.id} className="flex items-center gap-3 rounded-xl border px-3 py-2" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
            <Smartphone className="w-4 h-4 shrink-0" />
            <div className="flex-1 min-w-0 text-sm">
              <div className="truncate font-medium">{s.name || "Device"}{s.id === info.currentSessionId ? " (this one)" : ""}</div>
              <div className="text-xs" style={{ color: "var(--text-muted)" }}>signed in with {s.method} · last active {when(s.lastSeen)}</div>
            </div>
            <IconButton label={`Sign out ${s.name || "device"}`} onClick={() => void remove("session", s.id, s.name || "this device")}><Trash2 className="w-4 h-4" /></IconButton>
          </div>
        ))}
      </section>
    </div>
  );
}
