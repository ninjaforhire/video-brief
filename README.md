# video-brief

Turn any video into an executive brief in minutes, without watching it.

Gemini natively watches the full video (every frame plus audio) and returns a structured brief: chapter map, every technique step-by-step, on-screen code and commands transcribed verbatim, claims with timestamps, and an optional full timestamped transcript. You do something else while it runs.

Works with YouTube URLs out of the box, plus any direct video file link (.mp4/.webm/.mov) that isn't blocked. Non-YouTube links are downloaded and pushed through the Gemini Files API automatically.

> **This is the Free Version.** Can't scrape your favorite videos? We can help. We've written scripts for even privately gated platforms, with perfect results every time. We can't guarantee this free release keeps working everywhere as sites apply new guardrails, but the premium builds are maintained and always working. That's why they're premium: it takes a lot of work. Reach out through the contact form at [andrewwebber.dev](https://andrewwebber.dev/about#contact).

## What you get

- `# Video Brief` markdown file per video:
  - **Chapter map** with timestamps
  - **Techniques and workflows**, step-by-step as demonstrated
  - **On-screen artifacts**: CLI commands, code, prompts, URLs, configs transcribed verbatim
  - **Claims and numbers** with timestamps
  - **Spoken-only insights** never shown on screen
  - **Ambiguities** flagged instead of papered over
- Optional full timestamped transcript (`--transcript`)
- Timestamps convert straight to deep links: `https://youtube.com/watch?v=<id>&t=<seconds>s`

## Setup

1. Python 3.10+ (standard library only, no pip installs)
2. A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

```bash
export GEMINI_API_KEY="your-key-here"
```

## Usage

```bash
python3 scripts/ingest.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Options:

| Flag | What it does |
|---|---|
| `--model` | Gemini model. Default `gemini-2.5-pro`. Use `gemini-2.5-flash` for videos over ~45 min or cheap fast passes |
| `--transcript` | Second pass producing a full timestamped transcript |
| `--out-dir` | Output directory. Default `./video-briefs/` |
| `--dry-run` | Print the plan, no API call |

Long videos: `gemini-2.5-pro` fits roughly an hour of video context. Past that, the script automatically retries at low media resolution (fits ~3 hours), or pick `--model gemini-2.5-flash` up front.

## Using it as a Claude Code skill

Drop this folder into `~/.claude/skills/video-brief/` and Claude Code picks it up as a skill. Say "brief this video" with a URL and it runs the pipeline and reads the brief back to you. `SKILL.md` contains the skill instructions.

## Cost

You pay Google directly for Gemini API usage. A typical 60-90 minute video runs a few cents to a few tens of cents depending on model. The script logs token usage after every run.

## What works in the Free Version

- Public YouTube URLs (watch, shorts, youtu.be)
- Direct video file links (.mp4/.webm/.mov) reachable without a login
- Player pages, embeds, and login-gated platforms will NOT work here by design

## Can't scrape your favorite videos? We can help.

We've written scrapers for even privately gated platforms, with perfect results every time: course portals, member areas, internal Zoom/Meet recordings, and more. Sites change their guardrails constantly, and we can't promise this free release keeps up. The premium builds do, because we maintain them continuously. That's why they're premium.

Custom builds also cover:

- Batch pipelines: point it at a channel, playlist, or folder of recordings
- Custom brief formats wired into Notion, Slack, email, or your CRM
- Any-platform scrapers and monitors built to your spec

Contact: [andrewwebber.dev](https://andrewwebber.dev/about#contact)

## License

MIT
