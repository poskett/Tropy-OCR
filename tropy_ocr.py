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
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

DEFAULT_MODEL = "qwen3-vl:30b-a3b-instruct-q4_K_M"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TAG = "ocr:auto"

MARKER_PREFIX = "[Automated OCR - model:"
MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r".*?\]")

PROMPTS = {
    "auto": (
        "Transcribe all text visible in this image of a historical document "
        "exactly as it appears. Include both printed and handwritten text if "
        "present. Preserve the original line breaks. Do not add any "
        "commentary, headings, markdown formatting, or summary. If a word or "
        "passage is illegible, write [illegible] in its place. Output only "
        "the transcription."
    ),
    "printed": (
        "Transcribe the printed text visible in this image of a historical "
        "document exactly as it appears, preserving the original line "
        "breaks. Do not add any commentary, headings, markdown formatting, "
        "or summary. If a word or passage is illegible, write [illegible] in "
        "its place. Output only the transcription."
    ),
    "handwritten": (
        "Transcribe the handwritten text visible in this image of a "
        "historical document as accurately as possible, preserving the "
        "original line breaks. Do your best even where the handwriting is "
        "difficult to read; if a word is uncertain, give your best guess "
        "followed by [?], and use [illegible] where no reasonable guess is "
        "possible. Do not add any commentary, headings, markdown "
        "formatting, or summary. Output only the transcription."
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
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama vision model name")
    parser.add_argument("--ollama-host", default=DEFAULT_HOST, help="Ollama server URL")
    parser.add_argument(
        "--mode",
        choices=["auto", "printed", "handwritten"],
        default="auto",
        help="Transcription style hint",
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
    parser.add_argument("--tag", default=DEFAULT_TAG, help="Tag applied to processed photos")
    parser.add_argument("--tag-color", help="Color for the tag, e.g. #ff8c19")
    parser.add_argument("--no-tag", action="store_true", help="Do not tag processed photos")
    parser.add_argument(
        "--no-marker",
        action="store_true",
        help="Do not append the automated-OCR marker footer to notes",
    )
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
    return parser


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


def build_selection(conn, args, tag_id):
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
        if args.no_tag:
            clauses.append(
                "photos.id NOT IN ("
                "SELECT id FROM notes WHERE deleted IS NULL AND text LIKE ?)"
            )
            params.append(f"%{MARKER_PREFIX}%")
        else:
            clauses.append("photos.id NOT IN (SELECT id FROM taggings WHERE tag_id = ?)")
            params.append(tag_id)
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


def apply_limit(photos, args):
    if args.limit is not None:
        photos = photos[: args.limit]
    return photos


def load_image_b64(project_dir, relative_path, max_dimension):
    full_path = project_dir / relative_path
    with Image.open(full_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        longest = max(width, height)
        if longest > max_dimension:
            scale = max_dimension / float(longest)
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return base64.b64encode(buffer.getvalue()).decode("ascii")


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


def call_ollama(host, model, prompt, image_b64, timeout, retries, max_tokens):
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0, "num_predict": max_tokens},
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return data.get("response", "").strip()
        except (requests.RequestException, ValueError) as error:
            last_error = error
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


def make_marker(model_name):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{MARKER_PREFIX} {model_name} - {timestamp}]"


def finalize_note_text(ocr_text, model_name, add_marker):
    text = ocr_text.rstrip("\n")
    if add_marker:
        text = f"{text}\n\n{make_marker(model_name)}"
    return text


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
    try:
        image_b64 = load_image_b64(project_dir, photo["path"], args.max_dimension)
        prompt = PROMPTS[args.mode]
        raw_text = call_ollama(
            args.ollama_host,
            args.model,
            prompt,
            image_b64,
            args.timeout,
            args.retries,
            args.max_tokens,
        )
        ocr_text = clean_ocr_text(raw_text)
        if not ocr_text:
            raise RuntimeError("empty OCR response")
        final_text = finalize_note_text(ocr_text, args.model, not args.no_marker)
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
            "elapsed": time.time() - start,
            "text": ocr_text,
        }
    language = args.language.strip().lower()
    try:
        if args.overwrite:
            stale_ids = existing_marker_note_ids(conn, photo["photo_id"])
            soft_delete_notes(conn, stale_ids)
        insert_note(conn, photo["photo_id"], plain_text, state, language)
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
            "error": f"database write failed: {error}",
            "elapsed": time.time() - start,
        }
    return {
        "photo_id": photo["photo_id"],
        "filename": photo["filename"],
        "status": "ok",
        "chars": len(ocr_text),
        "elapsed": time.time() - start,
    }


def format_progress(index, total, photo, result):
    base = f"[{index}/{total}] photo {photo['photo_id']} {photo['filename']} - {result['status']}"
    if result["status"] in ("ok", "dry-run"):
        return f"{base} - {result['chars']} chars - {result['elapsed']:.1f}s"
    if result["status"] == "failed":
        return f"{base} - {result['error']}"
    return base


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    project_dir, db_path, _assets_dir = project_paths(args.project)
    check_not_open(db_path, args.force)
    if args.preview:
        conn = connect(db_path)
        tag_id = lookup_tag(conn, args.tag) if not args.no_tag else None
        photos = build_selection(conn, args, tag_id)
        photos = apply_limit(photos, args)
        for photo in photos:
            print(f"item {photo['item_id']:>6}  photo {photo['photo_id']:>6}  {photo['filename']}")
        print(f"{len(photos)} photo(s) matched")
        conn.close()
        return
    verify_model_available(args.ollama_host, args.model)
    if not args.dry_run:
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
    photos = build_selection(conn, args, tag_id)
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
