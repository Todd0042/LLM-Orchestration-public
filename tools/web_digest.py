"""Info-gathering tool: fetches URL text cleanly for LLM analysis."""

import re
import httpx

TOOL_SPEC = {
    "name": "web_digest",
    "category": "research",
    "gpuNeed": "none",
    "description": "Fetch a web page or JSON endpoint and return cleaned plain text.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP/HTTPS URL to fetch"},
        },
        "required": ["url"],
    },
}


def run(url: str) -> dict:
    r = httpx.get(url, timeout=12.0, follow_redirects=True)
    text = r.text
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"status": r.status_code, "url": str(r.url), "excerpt": text[:6000]}
