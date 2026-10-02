# Tropy OCR
Transcribe document images in a [Tropy](https://tropy.org) project
using a **local** vision model via [Ollama](https://ollama.com), and write the
transcriptions directly into Tropy's **Notes** field — one note per photo
(page).

Alternatively, Tropy OCR can call tesseract instead of a local vision model.

## AI declaration
The code for this project was generated using Claude Code (Sonnet 5). This readme 
file was generated from the Claude Code project and then edited by James Poskett.

## Disclaimer
This Python script is an independent, unofficial tool. It is not affiliated with or
endorsed by the Tropy project. It is released **as is and without *warranty**.

It works by reading and writing Tropy's
internal SQLite database directly, so please read the **Safety** section
below before running it on a project.

## Benefits

- Privacy (images do not leave your computer)
- Rights (no external AI model training)
- Cost (no payment for commercial service)
- Reproducability (specific models can be selected and pinned)

## What it does

- Finds photos (pages) in your Tropy project matching the filters you give it.
- Sends each image to a local Ollama vision model and asks for a literal
  transcription (preserving line breaks, no commentary, `[illegible]` for
  unreadable text).
- Writes the result into Tropy's Notes field for that photo.
- Tags both the photo and its parent item (default tag `ocr:auto`) with
  Tropy's own tags feature.
- Appends a short, human-readable marker line to the note itself (e.g.
  `[Automated OCR - model: qwen3-vl:30b-a3b-instruct-q4_K_M - 2026-10-01T15:31:17Z]`)
  so anyone reading that note later knows it's machine-generated, and which
  model/when. This marker is also how the tool recognizes a photo as
  "already done" on later runs — delete a photo's note in Tropy and it will
  automatically be reprocessed next time, no flags needed. Tags are applied
  too, but are purely informational (for browsing/filtering in Tropy) and
  don't affect what gets reprocessed.

## Requirements

- Python 3.10+
- Python packages in requirements.txt
- [Ollama](https://ollama.com) running locally, with a vision-capable model
  pulled (e.g. `ollama pull qwen3-vl:30b-a3b-instruct-q4_K_M`)
- For `--engine tesseract`: the `tesseract` binary installed separately
- Tropy, **closed**, while you run this tool

## Install

macOS/Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

After activating venv, run the tool the same way on any platform:
`python tropy_ocr.py --project ...`

## Safety

- **Make a manual backup** of your .tropy file before running this tool.
- **Close Tropy before running this tool.** It writes directly to Tropy's
  SQLite database file; the tool checks and attempts to abort if Tropy is open.
- Do **not** open Tropy whilst the tool is running. There are no additional safety checks.
- The tool makes a timestamped backup copy of `project.tpy` before writing,
  unless you pass `--no-backup`. Keep this backup until you've confirmed the results
  look right in Tropy.
- Always try `--dry-run` on a few photos first.

## Usage

Basic run, everything not yet transcribed:

```sh
python tropy_ocr.py --project "My Project.tropy"
```

Test on a few photos first, without writing anything:

```sh
python tropy_ocr.py --project "My Project.tropy" --dry-run --limit 5
```

Process a specific item (document) or photo:

```sh
python tropy_ocr.py --project "My Project.tropy" --item 3355
python tropy_ocr.py --project "My Project.tropy" --photo 3994
```

Process everything in a Tropy list you've curated in the app (this is the recommended use):

```sh
python tropy_ocr.py --project "My Project.tropy" --list "To transcribe"
```

Tropy lists can contain sublists. `--list` automatically includes items in all 
nested sublists.

Process everything in a specific Tropy sublist, such as "Parent List > List"

```sh
python tropy_ocr.py --project "My Project.tropy" --list "Parent List > To transcribe"
```

Filter by filename pattern (shell-style glob):

```sh
python tropy_ocr.py --project "My Project.tropy" --filename-glob "wfsw - 1*.jpeg"
```

### Finding item/photo ids

Tropy's own interface does not show these raw numeric ids, so if you want to
use `--item`/`--photo` precisely, preview a filter first with
`--preview` — it prints the matching item/photo ids and filenames and
exits immediately, without running any OCR or touching the database:

```sh
python tropy_ocr.py --project "My Project.tropy" --filename-glob "wfsw - 103*.jpeg" --preview
```

In practice, `--list` (a Tropy list you curate in the app, no ids needed) or
`--filename-glob` (matching the original scan filename shown in Tropy's
Photo info panel) are usually all you need, without looking up an id.

Regenerate notes this tool previously created (e.g. after switching models):

```sh
python tropy_ocr.py --project "My Project.tropy" --overwrite
```

Use the handwriting-tuned prompt instead of the default (auto-detect) one:

```sh
python tropy_ocr.py --project "My Project.tropy" --mode handwritten
```

### Key options

| Flag | Default | Purpose |
|---|---|---|
| `--project` | *(required)* | Path to the `.tropy` project bundle |
| `--model` | `qwen3-vl:30b-a3b-instruct-q4_K_M` | Ollama vision model |
| `--ollama-host` | `http://localhost:11434` | Ollama server URL |
| `--mode` | `auto` | `auto`, `printed`, or `handwritten` prompt style |
| `--item` / `--photo` | — | Restrict to specific item/photo ids (repeatable) |
| `--list` | — | Restrict to items in a named Tropy list |
| `--filename-glob` | — | Restrict to photos whose filename matches a glob |
| `--preview` | off | Print matching item/photo ids and filenames, then exit (no OCR, no writes) |
| `--limit` | — | Process at most N photos |
| `--tag` / `--tag-color` | `ocr:auto` | Tag applied to processed photos |
| `--no-tag` | off | Don't tag photos (tags are informational only; which photos are already done is always detected by the marker footer in their note) |
| `--overwrite` | off | Regenerate notes previously created by this tool |
| `--dry-run` | off | Run OCR and print results; write nothing to the database |
| `--max-dimension` | `2000` | Resize images to this many pixels on the longest edge before sending |
| `--max-tokens` | `4096` | Maximum tokens the model may generate per photo |
| `--no-backup` | off | Skip the automatic backup of `project.tpy` |
| `--force` | off | Proceed even if the project looks open |
| `--log-file` | — | Append a JSONL record per photo for offline review |
| `--language` | `en` | Language code stored on each note |
| `--retries` / `--timeout` | `2` / `120` | Ollama request retry/timeout behavior |

Run `python tropy_ocr.py --help` for the full list.

## Recommended workflow

1. Close Tropy.
2. `--dry-run --limit 5` and read the printed transcriptions against the
   source images to judge quality before writing anything.
3. Run for real on one small `--item` first, then reopen the project in
   Tropy and check the Notes panel for a couple of pages.
4. Run on the rest of the collection. Re-running later only processes new
   or not-yet-transcribed photos, so it's safe to do in batches. Delete a
   photo's note in Tropy to have it picked up again automatically.

## Known limitations

- OCR quality depends entirely on the chosen Ollama model; try a different
  `--model` or `--mode` if results are poor for a particular kind of document.
- Tesseract OCR is faster but much lower quality, and cannot process handwriting.
- This tool writes `notes.text`/`notes.state` to match Tropy's own internal
  format as closely as possible. It is not an official API and could in principle need updating
  if Tropy changes its note storage format in a future release.
- Tropy requires a language tag for notes (default: en). This tool does not detect language
  (to avoid complexity). Therefore, all notes will be marked 'en'. (This is separate from the 
  'item' language, which can be set manually by the user correctly within Tropy.)
## Tropy and MacOS Version
The tool was developed and tested on Tropy Version 1.17.3 (arm64) on Mac OS 26.6.2 (25G83).

It has not been tested on Windows.

## License

MIT — see [LICENSE](LICENSE).
