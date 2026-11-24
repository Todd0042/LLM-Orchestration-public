"""GPU lease coordinator (gpu-scheduler or local fallback) + ComfyUI image/video workflows."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any
import uuid

import httpx

from .cli_agents import resolve_cli_binary
from .config import ARTIFACTS_DIR, infer_model_profile, model_dirs


class GpuBroker:
    """Exclusive GPU turns for ComfyUI/music tools, via gpu-scheduler (see llm.GpuTurns).

    Never kills other apps' model servers: if the scheduler is missing, work just proceeds.
    """

    def __init__(self, turns):
        self.turns = turns
        self._open: dict[str, object] = {}

    async def status(self) -> dict[str, Any]:
        return await self.turns.status()

    async def acquire_exclusive(self, app_id: str = "llm_orchestration", job_id: str = "media") -> str | None:
        cm = self.turns.turn("exclusive", job_id, wait_sec=900.0)
        lease = await cm.__aenter__()
        key = str(lease.get("lease_id") or f"local_{uuid.uuid4().hex[:8]}")
        self._open[key] = cm
        return key

    async def release_exclusive(self, lease_id: str | None) -> None:
        cm = self._open.pop(lease_id or "", None)
        if cm is not None:
            await cm.__aexit__(None, None, None)


async def discover_node_models(settings: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    overrides = settings.get("modelOverrides") or {}

    def build_entry(mid: str, kind: str, status: str, ctx: int = 16384) -> dict[str, Any]:
        inferred = infer_model_profile(mid, kind=kind)
        ov = overrides.get(mid) or {}
        return {
            "id": mid,
            "nodeId": node_id,
            "kind": kind,
            "displayName": ov.get("displayName") or inferred["displayName"],
            "strengths": ov.get("strengths") or inferred["strengths"],
            "fit": ov.get("fit") or inferred["fit"],
            "contextLength": int(ov.get("contextLength") or ctx),
            "status": status,
        }

    # Advertise live CLI agents installed on this node (AGY & Claude)
    if resolve_cli_binary("agy"):
        models.append(build_entry("agy-cli", "llm", "cli-ready", 200000))
    if resolve_cli_binary("claude"):
        models.append(build_entry("claude-cli", "llm", "cli-ready", 200000))

    llm_base = settings["paths"]["llmBaseUrl"].rstrip("/")
    discovered_online = False
    for candidate_base in dict.fromkeys([llm_base, "http://127.0.0.1:11434/v1"]):
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(f"{candidate_base}/models")
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid = str(m.get("id") or "")
                        if not mid:
                            continue
                        st = str((m.get("status") or {}).get("value") or "ready")
                        ctx = int(m.get("context_length") or 16384)
                        entry_kind = "embed" if "embed" in mid.lower() else "llm"
                        models.append(build_entry(mid, entry_kind, st, ctx))
                    discovered_online = True
                    break
        except Exception:
            continue

    if not discovered_online:
        # Offline fallback: inspect local GGUF folder if present
        llm_dir, _, _ = model_dirs(settings)
        if llm_dir and llm_dir.is_dir():
            for f in sorted(llm_dir.glob("*.gguf")):
                models.append(build_entry(f.name, "llm", "offline-gguf"))

    # Inspect ComfyUI local checkpoints & video diffusion models
    _, comfy_models, _ = model_dirs(settings)
    comfy_models = comfy_models or Path("/nonexistent")
    for f in sorted((comfy_models / "checkpoints").glob("*.safetensors")) if (comfy_models / "checkpoints").is_dir() else []:
        models.append(build_entry(f.name, "image", "comfy"))
    for f in sorted((comfy_models / "unet").glob("*.gguf")) if (comfy_models / "unet").is_dir() else []:
        models.append(build_entry(f.name, "image", "comfy"))
    for f in sorted((comfy_models / "diffusion_models").glob("*.safetensors")) if (comfy_models / "diffusion_models").is_dir() else []:
        models.append(build_entry(f.name, "video", "comfy"))

    return models


async def generate_comfy_media(
    broker: GpuBroker,
    settings: dict[str, Any],
    kind: str,
    prompt: str,
    checkpoint: str,
    chat_id: str,
) -> dict[str, Any]:
    """Submits a ComfyUI prompt to :8188 (acquiring GPU first) and saves output artifact."""
    comfy_url = settings["paths"]["comfyUrl"].rstrip("/")
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = ARTIFACTS_DIR / chat_id
    out_dir.mkdir(parents=True, exist_ok=True)

    lease_id = await broker.acquire_exclusive(job_id=f"{kind}_{uuid.uuid4().hex[:6]}")
    try:
        # Verify ComfyUI is reachable (auto-launch only if local comfy wrapper exists)
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.get(f"{comfy_url}/system_stats")
            except Exception:
                _, _, launcher = model_dirs(settings)
                if not launcher or not launcher.is_file():
                    raise RuntimeError(f"ComfyUI unreachable at {comfy_url}")
                await asyncio.create_subprocess_exec("bash", "-c", f"nohup '{launcher}' >/tmp/llm-orch-comfy.log 2>&1 &")
                for _ in range(30):
                    await asyncio.sleep(1.0)
                    try:
                        r = await client.get(f"{comfy_url}/system_stats")
                        if r.status_code == 200:
                            break
                    except Exception:
                        pass

        # Standard SDXL / Checkpoint graph for reliable API execution
        seed = int(time.time() * 1000) % 2_147_483_647
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 6.5,
                    "denoise": 1,
                    "latent_image": ["5", 0],
                    "model": ["4", 0],
                    "negative": ["7", 0],
                    "positive": ["6", 0],
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "seed": seed,
                    "steps": 20,
                },
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 1, "height": 1024, "width": 1024}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": prompt}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": "blurry, deformed, watermark"}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"llmorch_{kind}", "images": ["8", 0]}},
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            pr = await client.post(f"{comfy_url}/prompt", json={"prompt": workflow})
            if pr.status_code >= 400:
                raise RuntimeError(f"ComfyUI error: {pr.text}")
            prompt_id = pr.json()["prompt_id"]

            saved_files: list[Path] = []
            for _ in range(180):
                await asyncio.sleep(1.0)
                hr = await client.get(f"{comfy_url}/history/{prompt_id}")
                hist = hr.json()
                if prompt_id in hist:
                    outputs = hist[prompt_id].get("outputs", {})
                    for node_out in outputs.values():
                        for img in node_out.get("images", []) + node_out.get("gifs", []):
                            fn = img["filename"]
                            sub = img.get("subfolder", "")
                            typ = img.get("type", "output")
                            dl = await client.get(f"{comfy_url}/view", params={"filename": fn, "subfolder": sub, "type": typ})
                            dest = out_dir / fn
                            dest.write_bytes(dl.content)
                            saved_files.append(dest)
                    break

            if not saved_files:
                raise RuntimeError("ComfyUI timed out waiting for rendered artifact.")
            first = saved_files[0]
            return {
                "path": str(first),
                "filename": first.name,
                "kind": "video" if first.suffix.lower() in (".mp4", ".webm", ".gif") else "image",
                "seed": seed,
                "model": checkpoint,
                "prompt": prompt,
            }
    finally:
        await broker.release_exclusive(lease_id)
