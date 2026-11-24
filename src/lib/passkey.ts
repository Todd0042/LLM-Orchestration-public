/**
 * Passkeys (WebAuthn) in the browser: turn the server's JSON options into a fingerprint/face
 * prompt, and the result back into JSON for the server to verify.
 */

type Json = Record<string, any>;

function toBytes(b64url: string): ArrayBuffer {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(b64url.length / 4) * 4, "=");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function toB64url(buf: ArrayBuffer | null): string {
  if (!buf) return "";
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && "PublicKeyCredential" in window;
}

/** Plain-language reasons for the common browser errors. */
export function describePasskeyError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : "";
  if (name === "NotAllowedError") return "Cancelled, or the passkey prompt timed out.";
  if (name === "InvalidStateError") return "This device already has a passkey for this site.";
  if (name === "SecurityError") return "Passkeys only work on the secure (https) address.";
  return e instanceof Error ? e.message : String(e);
}

export async function createPasskey(options: Json): Promise<Json> {
  const publicKey: PublicKeyCredentialCreationOptions = {
    ...options,
    challenge: toBytes(options.challenge),
    user: { ...options.user, id: toBytes(options.user.id) },
    excludeCredentials: (options.excludeCredentials ?? []).map((c: Json) => ({ ...c, id: toBytes(c.id) })),
  } as PublicKeyCredentialCreationOptions;
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was created");
  const res = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(res.clientDataJSON),
      attestationObject: toB64url(res.attestationObject),
      transports: typeof res.getTransports === "function" ? res.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
}

export async function getPasskey(options: Json): Promise<Json> {
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: toBytes(options.challenge),
    allowCredentials: (options.allowCredentials ?? []).map((c: Json) => ({ ...c, id: toBytes(c.id) })),
  } as PublicKeyCredentialRequestOptions;
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was chosen");
  const res = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(res.clientDataJSON),
      authenticatorData: toB64url(res.authenticatorData),
      signature: toB64url(res.signature),
      userHandle: toB64url(res.userHandle),
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
}
