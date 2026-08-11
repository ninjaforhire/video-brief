"""Ingest a video via the Gemini API and emit a structured learning brief.

Gemini natively watches the full video (every frame + audio). YouTube URLs
pass straight through as fileData fileUri; any other direct video link
(mp4/webm/mov) is downloaded and uploaded through the Gemini Files API.
Output is a markdown brief saved to ./video-briefs/ for the synthesis phase.
"""

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("video-brief")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
FILES_URL = "https://generativelanguage.googleapis.com/v1beta/{name}"

DEFAULT_MODEL = "gemini-3.6-flash"

# Measured against gemini-3.6-flash on 2026-08-10: a 19s YouTube clip billed
# 1,584 video tokens (83.4/s) and a controlled 300s upload billed 27,300 (91.0/s).
# Used only to estimate runtime when the source reports no duration.
VIDEO_TOKENS_PER_SECOND = 87.0

# USD per 1,000,000 tokens, list price. Rates drift — update when Google moves them.
# Unknown models fall back to "cost unavailable" rather than a guessed number.
PRICING = {"gemini-3.6-flash": {"input": 1.50, "output": 7.50}}

CTA = (
    "Locked out of the videos you actually need (course portals, member areas, "
    "private Zoom/Meet)? Custom builds: https://andrewwebber.dev/video-brief"
)

EXTRACTION_PROMPT = """You are a lossless extraction engine. Watch this entire video and produce an exhaustive structured brief. Do not summarize away detail — capture everything a practitioner would need to reproduce what is taught. No opinions, no recommendations.

Output markdown with these sections:

# Video Brief
## Metadata
Title, creator, approximate length, overall topic.

## Chapter Map
Timestamped outline of every distinct segment.

## Techniques & Workflows
Every technique, workflow, or method demonstrated. For each: name, timestamp, step-by-step procedure exactly as shown.

## On-Screen Artifacts
Every tool, app, CLI command, code snippet, prompt, URL, config, or UI element visible on screen. Transcribe code and prompts verbatim where legible.

## Claims & Numbers
Specific claims, benchmarks, prices, metrics, or comparisons stated, with timestamps.

## Spoken-Only Insights
Important points said aloud but never shown on screen.

## Ambiguities
Anything illegible, skipped, or unclear in the video."""

TRANSCRIPT_PROMPT = """Produce a full timestamped transcript of this video. Format as markdown: one line per utterance block, starting with [MM:SS] (or [H:MM:SS] past one hour). Clean up filler words lightly but keep wording faithful. Note significant on-screen visuals inline as *[visual: ...]* where they carry meaning the words don't."""


def is_youtube(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host.endswith("youtube.com") or host.endswith("youtu.be")


def slug_from_url(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url)
    if match:
        return match.group(1)
    stem = Path(urllib.parse.urlparse(url).path).stem
    return re.sub(r"[^\w-]", "-", stem)[:60] or "video"


def download_video(url: str, dest: Path) -> tuple[Path, str]:
    """Download a direct video link to dest. Returns (path, mime_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": "video-brief/1.0"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        mime = resp.headers.get_content_type()
        if not mime.startswith("video/"):
            guessed = mimetypes.guess_type(url)[0]
            if guessed and guessed.startswith("video/"):
                mime = guessed
            else:
                raise RuntimeError(
                    f"URL did not return a video (Content-Type: {mime}). "
                    "This works with direct video file links (.mp4/.webm/.mov). "
                    "Player pages, embeds, and login-gated platforms need a custom build."
                )
        size = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                size += len(chunk)
    log.info("Downloaded %.1f MB (%s)", size / 1e6, mime)
    return dest, mime


def upload_to_gemini(path: Path, mime: str, api_key: str) -> tuple[str, float | None]:
    """Upload a local video through the Files API. Returns (file URI, duration seconds).

    Duration comes back as videoMetadata.videoDuration (e.g. "300s") once the
    file reaches ACTIVE; it is None when Gemini reports no metadata.
    """
    size = path.stat().st_size
    start = urllib.request.Request(
        f"{UPLOAD_URL}?key={api_key}",
        data=json.dumps({"file": {"display_name": path.name}}).encode(),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(start, timeout=60) as resp:
        session_url = resp.headers["X-Goog-Upload-URL"]

    upload = urllib.request.Request(
        session_url,
        data=path.read_bytes(),
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    with urllib.request.urlopen(upload, timeout=1800) as resp:
        info = json.loads(resp.read())["file"]

    name, uri = info["name"], info["uri"]
    while info.get("state") == "PROCESSING":
        log.info("Gemini processing upload...")
        time.sleep(5)
        poll = urllib.request.Request(f"{FILES_URL.format(name=name)}?key={api_key}")
        with urllib.request.urlopen(poll, timeout=60) as resp:
            info = json.loads(resp.read())
    if info.get("state") == "FAILED":
        raise RuntimeError(f"Gemini could not process the video: {json.dumps(info)[:500]}")

    raw = (info.get("videoMetadata") or {}).get("videoDuration")
    duration = None
    if isinstance(raw, str) and raw.endswith("s"):
        try:
            duration = float(raw[:-1])
        except ValueError:
            duration = None
    return uri, duration


def extract_brief(
    file_uri: str,
    model: str,
    api_key: str,
    mime: str | None = None,
    low_res: bool = False,
    prompt: str = EXTRACTION_PROMPT,
) -> tuple[str, dict]:
    """Send the video to Gemini and return (brief_markdown, usage_metadata).

    Retries once at MEDIA_RESOLUTION_LOW if the video overflows the 1M-token
    context. On gemini-3.6-flash that ceiling is roughly 3.2 hours of video, and
    the retry is a no-op for token count: LOW, MEDIUM, and HIGH all billed the
    same 1,584 video tokens for a 19s clip when measured on 2026-08-10. The
    retry stays for older models where the setting still moves the number.
    """
    file_data: dict = {"fileUri": file_uri}
    if mime:
        file_data["mimeType"] = mime
    body = {
        "contents": [{"parts": [{"fileData": file_data}, {"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 32768},
    }
    if low_res:
        body["generationConfig"]["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if not low_res and "token count exceeds" in detail:
            log.info("Video exceeds 1M-token context — retrying at low media resolution...")
            return extract_brief(file_uri, model, api_key, mime, low_res=True, prompt=prompt)
        raise RuntimeError(f"Gemini API {exc.code}: {detail[:1000]}") from exc
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:2000]}") from exc
    return text, data.get("usageMetadata", {})


def video_tokens(usage: dict) -> int:
    """Video-modality tokens billed for one call."""
    return next(
        (d.get("tokenCount", 0) for d in usage.get("promptTokensDetails", [])
         if d.get("modality") == "VIDEO"),
        0,
    )


def call_cost(model: str, usage: dict) -> float | None:
    """USD for one call, or None when the model has no published rate here.

    Output billing includes thinking tokens, which routinely exceed the visible
    answer (a 19s probe returned 21 visible tokens against 134 thinking tokens),
    so leaving them out would understate the bill several times over.
    """
    rates = PRICING.get(model.split("/")[-1])
    if not rates:
        return None
    if "free" in str(usage.get("serviceTier", "")).lower():
        return 0.0
    output_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return (
        usage.get("promptTokenCount", 0) / 1e6 * rates["input"]
        + output_tokens / 1e6 * rates["output"]
    )


def clock(seconds: float) -> str:
    """Seconds as M:SS, or H:MM:SS past an hour."""
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def run_summary(model: str, usages: list[dict], elapsed: float, duration: float | None) -> str:
    """One line: time saved, what it cost, what it took."""
    runtime = duration
    approx = ""
    if runtime is None:
        tokens = max((video_tokens(u) for u in usages), default=0)
        if tokens:
            runtime = tokens / VIDEO_TOKENS_PER_SECOND
            approx = "~"

    costs = [call_cost(model, u) for u in usages]
    if any(c is None for c in costs):
        cost_text = "cost unavailable for this model"
    else:
        total = sum(costs)
        free = all("free" in str(u.get("serviceTier", "")).lower() for u in usages)
        cost_text = f"cost ${total:.2f}" + (" (free tier)" if free or total == 0 else "")

    if runtime is None:
        return f"Ran in {clock(elapsed)} · {cost_text}"

    saved = max(runtime - elapsed, 0) / 60
    return (
        f"Saved {approx}{saved:.0f} min · watched {approx}{clock(runtime)} "
        f"in {clock(elapsed)} · {cost_text}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="video-brief: Gemini video ingestion (Free Version)")
    parser.add_argument("url", help="YouTube URL or direct video file link (.mp4/.webm/.mov)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model (default: gemini-3.6-flash)")
    parser.add_argument("--out-dir", type=Path, default=Path("video-briefs"))
    parser.add_argument(
        "--transcript",
        action="store_true",
        help="Also produce a full timestamped transcript (second Gemini pass)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print request plan, no API call")
    args = parser.parse_args()

    out_path = args.out_dir / f"{slug_from_url(args.url)}_brief.md"
    source = "YouTube" if is_youtube(args.url) else "direct link (download + Files API upload)"
    if args.dry_run:
        log.info("Would ingest %s [%s] with %s -> %s", args.url, source, args.model, out_path)
        return

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("GEMINI_API_KEY not set")
        sys.exit(1)

    started = time.monotonic()
    duration: float | None = None

    if is_youtube(args.url):
        file_uri, mime = args.url, None
    else:
        log.info("Non-YouTube link — downloading...")
        with tempfile.TemporaryDirectory() as tmp:
            local, mime = download_video(args.url, Path(tmp) / "video")
            log.info("Uploading to Gemini Files API...")
            file_uri, duration = upload_to_gemini(local, mime, api_key)

    log.info("Ingesting %s with %s (full video, may take 1-5 min)...", args.url, args.model)
    brief, usage = extract_brief(file_uri, args.model, api_key, mime)
    usages = [usage]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"<!-- source: {args.url} | model: {args.model} -->\n\n{brief}\n")
    log.info("Brief saved: %s", out_path)
    log.info(
        "Tokens — total: %s, video: %s", usage.get("totalTokenCount"), video_tokens(usage) or "?"
    )

    if args.transcript:
        log.info("Transcribing (second pass)...")
        transcript, t_usage = extract_brief(file_uri, args.model, api_key, mime, prompt=TRANSCRIPT_PROMPT)
        usages.append(t_usage)
        t_path = args.out_dir / f"{slug_from_url(args.url)}_transcript.md"
        t_path.write_text(f"<!-- source: {args.url} | model: {args.model} -->\n\n{transcript}\n")
        log.info("Transcript saved: %s (total tokens: %s)", t_path, t_usage.get("totalTokenCount"))
        print(t_path)

    print(out_path)
    print()
    print(run_summary(args.model, usages, time.monotonic() - started, duration))
    print(CTA)


if __name__ == "__main__":
    main()
