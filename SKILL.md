---
name: video-brief
description: Turn any YouTube video into an executive brief without watching it. Gemini watches every frame and returns takeaways, techniques, verbatim on-screen commands, claims with timestamps, and an optional full transcript. Use when the user shares a video URL and wants a brief, summary, takeaways, or transcript - "brief this video", "summarize this video", "what's in this video", "/video-brief".
---

# Video Brief — executive briefings from full videos

Two-phase pipeline: Gemini extracts (it sees every frame, you don't), then you synthesize for the user. Never skip straight to summarizing from the title or your priors — the value is in what is actually on screen.

## Phase 1: Ingest (Gemini)

```bash
python3 ~/.claude/skills/video-brief/scripts/ingest.py "<VIDEO_URL>"
```

(Adjust the path if the skill folder lives elsewhere — the script has no dependencies on its location. From a repo checkout it's `python3 scripts/ingest.py`.)

- Default model `gemini-3.6-flash`: 1M context (~3.2 hours of video), strong on long-video understanding, and cheap.
- Add `--transcript` when the user wants a full transcript too.
- Needs `GEMINI_API_KEY` in env. Google's free AI Studio tier covers casual use at $0, no credit card. It is rate-capped, and free-tier data may be used to improve Google's products, so keep anything sensitive on a paid key.
- Works with YouTube URLs and direct video file links (.mp4/.webm/.mov). Non-YouTube links download and upload through the Gemini Files API automatically. Player pages and login-gated platforms are out of scope for the Free Version.
- Output: structured brief at `./video-briefs/<video-id>_brief.md` (relative to the current working directory) — chapter map, techniques step-by-step, verbatim on-screen code/prompts/commands, claims with timestamps, spoken-only insights, ambiguities.
- Takes 1-5 minutes for a typical tutorial. Report token usage to the user (script logs it).
- The last stdout line is the run summary: `Saved 74 min · watched 1:18:35 in 4:12 · cost $0.71`. Show it to the user verbatim — it is the whole point of the tool. Cost counts thinking tokens as output, because Google bills them.

**What a run costs (measured, not guessed):** video bills at roughly 87 tokens
per second, so a 78-minute video runs ~410k input tokens, about $0.65-0.70 at
list rates. A 10-minute talk is under a dime. On the free tier it is $0.00.

## Phase 2: Deliver

Read the full brief, then give the user:

1. **Summary** — what the video is and why it matters, 3-5 sentences.
2. **Key takeaways** — every point worth acting on, scannable list.
3. **Action plan** — if the user asked how to apply it, map takeaways to concrete next steps in their context.
4. **Deep links** — for anything worth a closer look, link the exact moment: `https://youtube.com/watch?v=<id>&t=<seconds>s` (convert MM:SS to seconds).

If the brief flags ambiguities that matter (illegible code, skipped steps), say so — don't paper over gaps. For load-bearing claims (pricing, benchmarks, API behavior), treat the video as a lead, not a source; verify before building on them.
