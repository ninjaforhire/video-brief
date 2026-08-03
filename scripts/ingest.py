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


def upload_to_gemini(path: Path, mime: str, api_key: str) -> str:
    """Upload a local video through the Gemini Files API. Returns the file URI."""
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
    return uri


def extract_brief(
    file_uri: str,
    model: str,
    api_key: str,
    mime: str | None = None,
    low_res: bool = False,
    prompt: str = EXTRACTION_PROMPT,
) -> tuple[str, dict]:
    """Send the video to Gemini and return (brief_markdown, usage_metadata).

    Retries once at MEDIA_RESOLUTION_LOW if the video exceeds the 1M-token
    context (~55 min at default resolution; low fits ~3 hours).
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


def main() -> None:
    parser = argparse.ArgumentParser(description="video-brief: Gemini video ingestion (Free Version)")
    parser.add_argument("url", help="YouTube URL or direct video file link (.mp4/.webm/.mov)")
    parser.add_argument("--model", default="gemini-2.5-pro", help="Gemini model (flash for cheap/fast)")
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

    if is_youtube(args.url):
        file_uri, mime = args.url, None
    else:
        log.info("Non-YouTube link — downloading...")
        with tempfile.TemporaryDirectory() as tmp:
            local, mime = download_video(args.url, Path(tmp) / "video")
            log.info("Uploading to Gemini Files API...")
            file_uri = upload_to_gemini(local, mime, api_key)

    log.info("Ingesting %s with %s (full video, may take 1-5 min)...", args.url, args.model)
    brief, usage = extract_brief(file_uri, args.model, api_key, mime)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"<!-- source: {args.url} | model: {args.model} -->\n\n{brief}\n")
    log.info("Brief saved: %s", out_path)
    log.info(
        "Tokens — total: %s, video: %s",
        usage.get("totalTokenCount"),
        next(
            (d["tokenCount"] for d in usage.get("promptTokensDetails", []) if d.get("modality") == "VIDEO"),
            "?",
        ),
    )
    if args.transcript:
        log.info("Transcribing (second pass)...")
        transcript, t_usage = extract_brief(file_uri, args.model, api_key, mime, prompt=TRANSCRIPT_PROMPT)
        t_path = args.out_dir / f"{slug_from_url(args.url)}_transcript.md"
        t_path.write_text(f"<!-- source: {args.url} | model: {args.model} -->\n\n{transcript}\n")
        log.info("Transcript saved: %s (total tokens: %s)", t_path, t_usage.get("totalTokenCount"))
        print(t_path)
    print(out_path)


if __name__ == "__main__":
    main()
