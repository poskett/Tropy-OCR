# Declaration: Generated with assistance of Claude Code (Sonnet 5).
# All comments below this line are human-authored.

import argparse
import base64
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
import requests
from PIL import Image

DEFAULT_MODEL = "qwen3-vl:30b-a3b-instruct-q4_K_M"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TAG = "ocr:auto"
DEFAULT_TESSERACT_LANG = "eng"

MARKER_PREFIX = "[Automated OCR - model:"
MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r".*?\]")

NO_TEXT_INSTRUCTION = (
    "If the image contains no legible text at all - for example a blank or "
    "blurred surface, or a photograph of an object, wall, desk, or person "
    "with no writing on it - respond with exactly: [no text identified]. "
    "Do not invent, guess, or hallucinate any transcription in that case."
)

PROMPTS = {
    "auto": (
        "Transcribe all text visible in this image of a historical document "
        "exactly as it appears. Include both printed and handwritten text if "
        "present. Preserve the original line breaks. Do not add any "
        "commentary, headings, markdown formatting, or summary. If a word or "
        "passage is illegible, write [illegible] in its place. Output only "
        "the transcription. " + NO_TEXT_INSTRUCTION
    ),
    "printed": (
        "Transcribe the printed text visible in this image of a historical "
        "document exactly as it appears, preserving the original line "
        "breaks. Do not add any commentary, headings, markdown formatting, "
        "or summary. If a word or passage is illegible, write [illegible] in "
        "its place. Output only the transcription. " + NO_TEXT_INSTRUCTION
    ),
    "handwritten": (
        "Transcribe the handwritten text visible in this image of a "
        "historical document as accurately as possible, preserving the "
        "original line breaks. Do your best even where the handwriting is "
        "difficult to read; if a word is uncertain, give your best guess "
        "followed by [?], and use [illegible] where no reasonable guess is "
        "possible. Do not add any commentary, headings, markdown "
        "formatting, or summary. Output only the transcription. "
        + NO_TEXT_INSTRUCTION
    ),
}


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe Tropy project photos with a local Ollama vision "
            "model and write the results into Tropy Notes."
        )
    )
    parser.add_argument("--project", required=True, help="Path to the .tropy project bundle")
    parser.add_argument(
        "--engine",
        choices=["vision", "tesseract"],
        default="vision",
        help=(
            "OCR engine to use: 'vision' calls a local Ollama vision model, "
            "'tesseract' calls the local tesseract through Python instead"
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama vision model name (--engine vision only)")
    parser.add_argument("--ollama-host", default=DEFAULT_HOST, help="Ollama server URL (--engine vision only)")
    parser.add_argument(
        "--tesseract-lang",
        default=DEFAULT_TESSERACT_LANG,
        help="Tesseract language code, e.g. eng, fra, lat (--engine tesseract only)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "printed", "handwritten"],
        default="auto",
        help="Transcription style hint (--engine vision only)",
    )
    parser.add_argument("--item", type=int, action="append", help="Restrict to this item id (repeatable)")
    parser.add_argument("--photo", type=int, action="append", help="Restrict to this photo id (repeatable)")
    parser.add_argument("--list", dest="list_name", help="Restrict to items in this Tropy list")
    parser.add_argument("--filename-glob", help="Restrict to photos whose filename matches this glob pattern")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print the item/photo ids and filenames matching your filters, then exit without running OCR",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many photos")
    parser.add_argument(
        "--output",
        choices=["notes", "txt", "both"],
        default="notes",
        help=(
            "Where to write transcriptions: 'notes' writes into Tropy Notes (default), "
            "'txt' writes one .txt file per photo under --txt-dir instead, 'both' does both"
        ),
    )
    parser.add_argument(
        "--txt-dir",
        help=(
            "Directory to write .txt transcription files into, one per photo under "
            "item_<item_id>/photo_<photo_id>_<filename>.txt (required with --output txt or both)"
        ),
    )
    parser.add_argument("--tag", default=DEFAULT_TAG, help="Tag applied to processed photos")
    parser.add_argument("--tag-color", help="Color for the tag, e.g. #ff8c19")
    parser.add_argument("--no-tag", action="store_true", help="Do not tag processed photos")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate notes previously created by this tool",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run OCR and print results without writing to the database",
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=2000,
        help="Resize images so the longest edge is at most this many pixels",
    )
    parser.add_argument("--no-backup", action="store_true", help="Skip the automatic backup of project.tpy")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if the project looks like it is currently open",
    )
    parser.add_argument("--log-file", help="Write a JSONL record for every processed photo to this path")
    parser.add_argument("--language", default="en", help="Language code stored on each note")
    parser.add_argument("--retries", type=int, default=2, help="Number of retries for failed Ollama requests")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds for each Ollama request")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Maximum tokens the model may generate per photo (raise for dense full pages)",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help=(
            "Context window size given to the model. Must comfortably fit the image "
            "tokens, the prompt, and --max-tokens combined, or the request fails with "
            "a context-overflow error."
        ),
    )
    parser.add_argument(
        "--repeat-penalty",
        type=float,
        default=1.3,
        help=(
            "Penalty applied to already-generated tokens. Above 1.0 to stop the model "
            "looping on the same token on blank or badly damaged pages."
        ),
    )
    return parser


def warn_on_irrelevant_engine_flags(args):
    if args.engine == "tesseract":
        vision_only = {
            "--mode": (args.mode, "auto"),
            "--model": (args.model, DEFAULT_MODEL),
            "--ollama-host": (args.ollama_host, DEFAULT_HOST),
            "--num-ctx": (args.num_ctx, 8192),
            "--repeat-penalty": (args.repeat_penalty, 1.3),
            "--max-tokens": (args.max_tokens, 4096),
        }
        ignored = [flag for flag, (value, default) in vision_only.items() if value != default]
        if ignored:
            print(f"Note: {', '.join(ignored)} is ignored with --engine tesseract", file=sys.stderr)
    else:
        if args.tesseract_lang != DEFAULT_TESSERACT_LANG:
            print("Note: --tesseract-lang is ignored with --engine vision", file=sys.stderr)


def project_paths(project_arg):
    project_dir = Path(project_arg)
    db_path = project_dir / "project.tpy"
    assets_dir = project_dir / "assets"
    if not db_path.exists():
        raise SystemExit(f"project.tpy not found in {project_dir}")
    return project_dir, db_path, assets_dir


def file_is_open_elsewhere(db_path):
    sidecar_paths = [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]
    try:
        result = subprocess.run(
            ["lsof"] + [str(p) for p in sidecar_paths],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    return bool(result.stdout.strip())


def check_not_open(db_path, force):
    if force:
        return
    opened_elsewhere = file_is_open_elsewhere(db_path)
    if opened_elsewhere:
        raise SystemExit(
            f"{db_path} is currently open by another process (likely Tropy is still open). "
            "Close Tropy before running this tool, or pass --force if you are sure it is closed."
        )
    if opened_elsewhere is None:
        try:
            probe = sqlite3.connect(str(db_path), timeout=1)
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            probe.close()
        except sqlite3.OperationalError as error:
            raise SystemExit(
                f"{db_path} appears to be locked by another process (likely Tropy is still open): {error}. "
                "Close Tropy before running this tool, or pass --force if you are sure it is closed."
            )


def backup_database(db_path, skip):
    if skip:
        return None
    checkpoint_conn = sqlite3.connect(str(db_path))
    checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint_conn.close()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{timestamp}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_tag(conn, name, color):
    row = conn.execute("SELECT tag_id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row["tag_id"]
    cursor = conn.execute("INSERT INTO tags (name, color) VALUES (?, ?)", (name, color))
    return cursor.lastrowid


def lookup_tag(conn, name):
    row = conn.execute("SELECT tag_id FROM tags WHERE name = ?", (name,)).fetchone()
    return row["tag_id"] if row else None


def tag_photo(conn, photo_id, tag_id):
    conn.execute(
        "INSERT OR IGNORE INTO taggings (tag_id, id) VALUES (?, ?)",
        (tag_id, photo_id),
    )


def tag_item(conn, item_id, tag_id):
    conn.execute(
        "INSERT OR IGNORE INTO taggings (tag_id, id) VALUES (?, ?)",
        (tag_id, item_id),
    )


def list_path(conn, list_id):
    names = []
    current = list_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT name, parent_list_id FROM lists WHERE list_id = ?", (current,)
        ).fetchone()
        if not row:
            break
        names.append(row["name"])
        current = row["parent_list_id"]
    return " > ".join(reversed(names))


def resolve_list_ids(conn, list_name):
    if " > " in list_name:
        segments = list_name.split(" > ")
        parent_id = 0
        list_id = None
        for depth, segment in enumerate(segments):
            row = conn.execute(
                "SELECT list_id FROM lists WHERE parent_list_id = ? AND name = ?",
                (parent_id, segment),
            ).fetchone()
            if not row:
                where = " > ".join(segments[:depth]) or "the top level"
                raise SystemExit(
                    f"No list named '{segment}' found under {where} "
                    f"while resolving path '{list_name}'."
                )
            list_id = row["list_id"]
            parent_id = list_id
    else:
        rows = conn.execute(
            "SELECT list_id FROM lists WHERE name = ?", (list_name,)
        ).fetchall()
        if not rows:
            raise SystemExit(f"No list named '{list_name}' found in this project.")
        if len(rows) > 1:
            paths = ", ".join(
                f"'{list_path(conn, row['list_id'])}' (list_id {row['list_id']})" for row in rows
            )
            raise SystemExit(
                f"Multiple lists named '{list_name}' exist ({paths}). "
                f"Re-run with --list using the full path, e.g. "
                f"--list \"{list_path(conn, rows[0]['list_id'])}\". "
                "If the paths above are identical, these lists are true "
                "duplicates at the same level in Tropy's database (not "
                "something this tool can disambiguate) — rename one of "
                "them in Tropy first."
            )
        list_id = rows[0]["list_id"]

    descendant_rows = conn.execute(
        "WITH RECURSIVE descendants(list_id) AS ("
        "SELECT ? "
        "UNION ALL "
        "SELECT lists.list_id FROM lists, descendants "
        "WHERE lists.parent_list_id = descendants.list_id"
        ") SELECT list_id FROM descendants",
        (list_id,),
    ).fetchall()
    return [row["list_id"] for row in descendant_rows]


def build_selection(conn, args):
    clauses = [
        "photos.id NOT IN (SELECT id FROM trash)",
        "photos.item_id NOT IN (SELECT id FROM trash)",
    ]
    params = []
    if args.item:
        placeholders = ",".join("?" for _ in args.item)
        clauses.append(f"photos.item_id IN ({placeholders})")
        params.extend(args.item)
    if args.photo:
        placeholders = ",".join("?" for _ in args.photo)
        clauses.append(f"photos.id IN ({placeholders})")
        params.extend(args.photo)
    if args.list_name:
        list_ids = resolve_list_ids(conn, args.list_name)
        placeholders = ",".join("?" for _ in list_ids)
        clauses.append(
            "photos.item_id IN ("
            "SELECT list_items.id FROM list_items "
            f"WHERE list_items.list_id IN ({placeholders}) "
            "AND list_items.deleted IS NULL)"
        )
        params.extend(list_ids)
    if args.filename_glob:
        clauses.append("photos.filename GLOB ?")
        params.append(args.filename_glob)
    if not args.overwrite:
        clauses.append(
            "photos.id NOT IN ("
            "SELECT id FROM notes WHERE deleted IS NULL AND text LIKE ?)"
        )
        params.append(f"%{MARKER_PREFIX}%")
    where = " AND ".join(clauses)
    sql = (
        "SELECT photos.id AS photo_id, photos.item_id AS item_id, "
        "photos.path AS path, photos.filename AS filename "
        "FROM photos "
        f"WHERE {where} "
        "ORDER BY photos.item_id, photos.position"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def report_excluded_explicit_ids(args, photos):
    if args.overwrite:
        return
    matched_item_ids = {photo["item_id"] for photo in photos}
    matched_photo_ids = {photo["photo_id"] for photo in photos}
    missing_items = set(args.item or []) - matched_item_ids
    missing_photos = set(args.photo or []) - matched_photo_ids
    missing = sorted(missing_items) + sorted(missing_photos)
    if missing:
        kind = "item/photo" if missing_items and missing_photos else ("item" if missing_items else "photo")
        ids = ", ".join(str(i) for i in missing)
        print(
            f"Note: {len(missing)} explicitly requested {kind} id(s) ({ids}) were already "
            f"processed and excluded; use --overwrite to reprocess them.",
            file=sys.stderr,
        )


def apply_limit(photos, args):
    if args.limit is not None:
        photos = photos[: args.limit]
    return photos


def load_image(project_dir, relative_path, max_dimension):
    full_path = project_dir / relative_path
    with Image.open(full_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        longest = max(width, height)
        if longest > max_dimension:
            scale = max_dimension / float(longest)
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        return image.copy()


def image_to_b64(image):
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def verify_tesseract_available(lang):
    try:
        available_langs = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError:
        raise SystemExit(
            "tesseract is not installed or not on PATH. Install it first, e.g. on "
            "macOS: brew install tesseract tesseract-lang"
        )
    if not available_langs:
        raise SystemExit(
            "Could not determine which tesseract language packs are installed "
            "(tesseract --list-langs reported none). Check your tesseract install."
        )
    if lang not in available_langs:
        listing = ", ".join(sorted(available_langs))
        raise SystemExit(
            f"Tesseract language '{lang}' is not installed.\n"
            f"Install it, e.g. on macOS: brew install tesseract-lang\n"
            f"Languages currently available: {listing}"
        )


def call_tesseract(image, lang, timeout):
    try:
        return pytesseract.image_to_string(image, lang=lang, timeout=timeout)
    except (pytesseract.TesseractError, RuntimeError) as error:
        raise RuntimeError(f"tesseract failed: {error}") from error


def verify_model_available(host, model):
    url = f"{host.rstrip('/')}/api/tags"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        available = {entry.get("name") for entry in response.json().get("models", [])}
    except (requests.RequestException, ValueError) as error:
        raise SystemExit(f"Could not reach Ollama at {host}: {error}")
    if model not in available:
        listing = ", ".join(sorted(name for name in available if name)) or "(none)"
        raise SystemExit(
            f"Model '{model}' is not available on {host}.\n"
            f"Pull it first with: ollama pull {model}\n"
            f"Models currently available: {listing}"
        )


def describe_ollama_error(error, server_detail):
    detail_lower = server_detail.lower()
    if server_detail and "repeat limit" in detail_lower:
        return (
            f"{error} - {server_detail} "
            f"(the model got stuck generating the same token repeatedly, usually "
            f"on blank, very faint, or badly damaged pages; try raising "
            f"--repeat-penalty above its current value, or inspect this photo "
            f"to see if it is actually legible)"
        )
    if server_detail and ("context" in detail_lower or "slot" in detail_lower):
        return (
            f"{error} - {server_detail} "
            f"(the image plus prompt used more tokens than the model's context "
            f"window allows; try raising --num-ctx, or lowering --max-dimension "
            f"or --max-tokens)"
        )
    if server_detail:
        return f"{error} - {server_detail}"
    return str(error)


def call_ollama(host, model, prompt, image_b64, timeout, retries, max_tokens, num_ctx, repeat_penalty):
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_predict": max_tokens,
            "num_ctx": num_ctx,
            "repeat_penalty": repeat_penalty,
        },
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return data.get("response", "").strip()
        except requests.HTTPError as error:
            server_detail = ""
            try:
                server_detail = response.json().get("error", "")
            except ValueError:
                server_detail = response.text.strip()
            last_error = describe_ollama_error(error, server_detail)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except (requests.RequestException, ValueError) as error:
            last_error = str(error)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Ollama request failed after {retries + 1} attempts: {last_error}")


def clean_ocr_text(raw_text):
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def is_no_text_response(text):
    normalized = text.strip().strip(".").lower()
    return normalized in {"[no text identified]", "no text identified"}


def make_marker(model_name):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{MARKER_PREFIX} {model_name} - {timestamp}]"


def finalize_note_text(ocr_text, model_name):
    text = ocr_text.rstrip("\n")
    return f"{text}\n\n{make_marker(model_name)}"


def txt_output_path(txt_dir, photo):
    item_dir = Path(txt_dir) / f"item_{photo['item_id']}"
    stem = Path(photo["filename"]).stem
    return item_dir / f"photo_{photo['photo_id']}_{stem}.txt"


def write_txt_file(path, text, backup_existing):
    if backup_existing and path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak-{timestamp}")
        shutil.copy2(path, backup_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_note_state(text):
    lines = text.split("\n")
    paragraphs = []
    for line in lines:
        if line.strip() == "":
            paragraphs.append({"type": "paragraph"})
        else:
            paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    if not paragraphs:
        paragraphs = [{"type": "paragraph"}]
    doc = {"type": "doc", "content": paragraphs}
    return {"doc": doc, "selection": {"type": "text", "anchor": 1, "head": 1}}


def build_note_plain_text(text):
    return " ".join(text.split("\n"))


def existing_marker_note_ids(conn, photo_id):
    rows = conn.execute(
        "SELECT note_id, text FROM notes WHERE id = ? AND deleted IS NULL",
        (photo_id,),
    ).fetchall()
    return [row["note_id"] for row in rows if MARKER_RE.search(row["text"])]


def soft_delete_notes(conn, note_ids):
    if not note_ids:
        return
    placeholders = ",".join("?" for _ in note_ids)
    conn.execute(
        f"UPDATE notes SET deleted = CURRENT_TIMESTAMP WHERE note_id IN ({placeholders})",
        note_ids,
    )


def insert_note(conn, photo_id, text, state, language):
    conn.execute(
        "INSERT INTO notes (id, text, state, language) VALUES (?, ?, ?, ?)",
        (photo_id, text, json.dumps(state), language),
    )


def process_photo(conn, project_dir, photo, args, tag_id):
    start = time.time()
    txt_path = None
    if args.output in ("txt", "both"):
        txt_path = txt_output_path(args.txt_dir, photo)
        if txt_path.exists() and not args.overwrite:
            return {
                "photo_id": photo["photo_id"],
                "filename": photo["filename"],
                "status": "skipped",
                "elapsed": time.time() - start,
            }
    try:
        image = load_image(project_dir, photo["path"], args.max_dimension)
        if args.engine == "tesseract":
            raw_text = call_tesseract(image, args.tesseract_lang, args.timeout)
            engine_label = f"tesseract {args.tesseract_version}:{args.tesseract_lang}"
        else:
            image_b64 = image_to_b64(image)
            prompt = PROMPTS[args.mode]
            raw_text = call_ollama(
                args.ollama_host,
                args.model,
                prompt,
                image_b64,
                args.timeout,
                args.retries,
                args.max_tokens,
                args.num_ctx,
                args.repeat_penalty,
            )
            engine_label = args.model
        ocr_text = clean_ocr_text(raw_text)
        no_text = not ocr_text or is_no_text_response(ocr_text)
        if no_text:
            ocr_text = "[no text identified]"
        final_text = finalize_note_text(ocr_text, engine_label)
        plain_text = build_note_plain_text(final_text)
        state = build_note_state(final_text)
    except Exception as error:
        return {
            "photo_id": photo["photo_id"],
            "filename": photo["filename"],
            "status": "failed",
            "error": str(error),
            "elapsed": time.time() - start,
        }
    if args.dry_run:
        return {
            "photo_id": photo["photo_id"],
            "filename": photo["filename"],
            "status": "dry-run",
            "chars": len(ocr_text),
            "no_text": no_text,
            "elapsed": time.time() - start,
            "text": ocr_text,
        }
    language = args.language.strip().lower()
    try:
        if args.output in ("notes", "both"):
            if args.overwrite:
                stale_ids = existing_marker_note_ids(conn, photo["photo_id"])
                soft_delete_notes(conn, stale_ids)
            insert_note(conn, photo["photo_id"], plain_text, state, language)
        if args.output in ("txt", "both"):
            write_txt_file(txt_path, final_text, backup_existing=args.overwrite)
        if not args.no_tag:
            tag_photo(conn, photo["photo_id"], tag_id)
            tag_item(conn, photo["item_id"], tag_id)
        conn.commit()
    except Exception as error:
        conn.rollback()
        return {
            "photo_id": photo["photo_id"],
            "filename": photo["filename"],
            "status": "failed",
            "error": f"write failed: {error}",
            "elapsed": time.time() - start,
        }
    return {
        "photo_id": photo["photo_id"],
        "filename": photo["filename"],
        "status": "ok",
        "chars": len(ocr_text),
        "no_text": no_text,
        "elapsed": time.time() - start,
    }


def format_progress(index, total, photo, result):
    base = f"[{index}/{total}] photo {photo['photo_id']} {photo['filename']} - {result['status']}"
    if result["status"] in ("ok", "dry-run"):
        suffix = " - no text identified" if result.get("no_text") else f" - {result['chars']} chars"
        return f"{base}{suffix} - {result['elapsed']:.1f}s"
    if result["status"] == "failed":
        return f"{base} - {result['error']}"
    return base


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    warn_on_irrelevant_engine_flags(args)
    if args.output in ("txt", "both") and not args.txt_dir:
        raise SystemExit("--txt-dir is required when --output is 'txt' or 'both'")
    project_dir, db_path, _assets_dir = project_paths(args.project)
    check_not_open(db_path, args.force)
    if args.preview:
        conn = connect(db_path)
        photos = build_selection(conn, args)
        photos = apply_limit(photos, args)
        for photo in photos:
            print(f"item {photo['item_id']:>6}  photo {photo['photo_id']:>6}  {photo['filename']}")
        print(f"{len(photos)} photo(s) matched")
        conn.close()
        return
    if args.engine == "tesseract":
        verify_tesseract_available(args.tesseract_lang)
        args.tesseract_version = str(pytesseract.get_tesseract_version())
    else:
        verify_model_available(args.ollama_host, args.model)
    writes_db = args.output in ("notes", "both") or not args.no_tag
    if not args.dry_run and writes_db:
        backup_path = backup_database(db_path, args.no_backup)
        if backup_path:
            print(f"Backed up {db_path.name} to {backup_path.name}")
    conn = connect(db_path)
    tag_id = None
    if not args.no_tag:
        if args.dry_run:
            tag_id = lookup_tag(conn, args.tag)
        else:
            tag_id = ensure_tag(conn, args.tag, args.tag_color)
            conn.commit()
    photos = build_selection(conn, args)
    if args.item or args.photo:
        report_excluded_explicit_ids(args, photos)
    photos = apply_limit(photos, args)
    total = len(photos)
    print(f"Selected {total} photo(s) to process")
    counts = {}
    failed_ids = []
    log_handle = open(args.log_file, "a", encoding="utf-8") if args.log_file else None
    try:
        for index, photo in enumerate(photos, start=1):
            result = process_photo(conn, project_dir, photo, args, tag_id)
            print(format_progress(index, total, photo, result))
            if result["status"] == "dry-run":
                print("-" * 60)
                print(result["text"])
                print("-" * 60)
            if log_handle:
                log_handle.write(json.dumps(result) + "\n")
                log_handle.flush()
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] == "failed":
                failed_ids.append(photo["photo_id"])
    finally:
        if log_handle:
            log_handle.close()
        conn.close()
    print(
        f"Done. ok={counts.get('ok', 0)} dry-run={counts.get('dry-run', 0)} "
        f"failed={counts.get('failed', 0)}"
    )
    if failed_ids:
        print("Failed photo ids:", ", ".join(str(i) for i in failed_ids))


if __name__ == "__main__":
    main()
