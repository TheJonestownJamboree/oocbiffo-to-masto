#!/usr/bin/env python3
"""
Bluesky → Mastodon crossposter (no Bluesky login).
- Fetches public posts from a Bluesky handle
- Reposts text + images (with alt) to Mastodon
- Adds attribution link back to Bluesky
- Avoids duplicates via a local state file

Requirements:
  pip install requests python-dotenv
"""

import json
import os
import sys
import time
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

APPVIEW = "https://public.api.bsky.app"  # Bluesky AppView (public)

# --------- Config ---------
load_dotenv()

BLUESKY_HANDLE = os.getenv("BLUESKY_HANDLE", "").strip()
MASTO_BASE = os.getenv("MASTODON_BASE_URL", "").rstrip("/")
MASTO_TOKEN = os.getenv("MASTODON_ACCESS_TOKEN", "").strip()
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
MASTO_VISIBILITY = os.getenv("MASTODON_VISIBILITY", "public")
MASTO_MAX_CHARS = int(os.getenv("MASTODON_MAX_CHARS", "500"))
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "20"))  # Bluesky fetch batch size (≤100)

if not BLUESKY_HANDLE or not MASTO_BASE or not MASTO_TOKEN:
    print("ERROR: Missing required config. Ensure BLUESKY_HANDLE, MASTODON_BASE_URL, MASTODON_ACCESS_TOKEN are set.")
    sys.exit(1)

HEADERS_JSON = {
    "Authorization": f"Bearer {MASTO_TOKEN}",
    "Content-Type": "application/json",
}
HEADERS_AUTH = {"Authorization": f"Bearer {MASTO_TOKEN}"}


# --------- State helpers ---------
def load_state(path: str) -> Dict:
    if Path(path).is_file():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"synced_uris": []}


def save_state(path: str, state: Dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------- Bluesky API ---------
def bsky_get_author_feed(handle: str, cursor: Optional[str] = None, limit: int = 20) -> Dict:
    """
    GET app.bsky.feed.getAuthorFeed (no auth required).
    filter=posts_no_replies to avoid replies.
    """
    params = {
        "actor": handle,
        "filter": "posts_no_replies",
        "limit": min(max(1, limit), 100),
    }
    if cursor:
        params["cursor"] = cursor
    url = f"{APPVIEW}/xrpc/app.bsky.feed.getAuthorFeed"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def bsky_post_url_from_uri(author_handle: str, at_uri: str) -> str:
    # at://did:plc:.../app.bsky.feed.post/<rkey>  → https://bsky.app/profile/<handle>/post/<rkey>
    rkey = at_uri.split("/")[-1]
    return f"https://bsky.app/profile/{author_handle}/post/{rkey}"


def extract_images_from_embed(embed_obj: Optional[Dict]) -> List[Dict]:
    """
    Returns [{url: <img_url>, alt: <alt>}]
    Supports:
      - app.bsky.embed.images#view
      - app.bsky.embed.recordWithMedia#view (when media is images)
    """
    images: List[Dict] = []
    if not embed_obj or not isinstance(embed_obj, dict):
        return images

    def pull(view: Dict):
        for im in view.get("images", []) or []:
            url = im.get("fullsize") or im.get("thumb")
            if url:
                images.append({"url": url, "alt": im.get("alt") or ""})

    t = embed_obj.get("$type")
    if t == "app.bsky.embed.images#view":
        pull(embed_obj)
    elif t == "app.bsky.embed.recordWithMedia#view":
        media = embed_obj.get("media") or {}
        if media.get("$type") == "app.bsky.embed.images#view":
            pull(media)

    return images[:4]  # Mastodon supports up to 4 images per status


# --------- Mastodon API ---------
def masto_upload_image(img_bytes: bytes, filename: str, alt_text: str) -> str:
    """
    Upload to /api/v2/media (async for large). Poll /api/v1/media/:id until ready (url present).
    Returns media id.
    """
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    files = {"file": (filename, img_bytes, mime_type)}
    data = {"description": alt_text or ""}
    url = f"{MASTO_BASE}/api/v2/media"
    r = requests.post(url, headers=HEADERS_AUTH, files=files, data=data, timeout=120)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Mastodon media upload failed: {r.status_code} {r.text}")

    media = r.json()
    media_id = media["id"]

    # If asynchronous (202 or url is null), poll until ready.
    if r.status_code == 202 or not media.get("url"):
        status_url = f"{MASTO_BASE}/api/v1/media/{media_id}"
        for _ in range(60):  # up to ~60s
            time.sleep(1)
            rr = requests.get(status_url, headers=HEADERS_AUTH, timeout=30)
            rr.raise_for_status()
            media = rr.json()
            if media.get("url"):
                break
        else:
            print(f"Warning: media {media_id} not fully processed yet; proceeding.")
    return media_id


def masto_post_status(text: str, media_ids: Optional[List[str]] = None) -> Dict:
    payload = {
        "status": text,
        "visibility": MASTO_VISIBILITY,
    }
    if media_ids:
        payload["media_ids"] = media_ids
    url = f"{MASTO_BASE}/api/v1/statuses"
    r = requests.post(url, headers=HEADERS_JSON, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


# --------- Helpers ---------
def safe_truncate_for_masto(text: str, link: str, max_chars: int) -> str:
    """
    Ensure final post <= max_chars, always keeping the attribution link.
    """
    base = text.strip()
    suffix = f"\n\nOriginal Bluesky: {link}"
    if len(base) + len(suffix) <= max_chars:
        return base + suffix
    # Leave room for suffix + ellipsis
    allowed = max_chars - len(suffix) - 1
    allowed = max(0, allowed)
    truncated = (base[:allowed] + "…") if len(base) > allowed else base
    return truncated + suffix


def download_bytes(url: str) -> Tuple[bytes, str]:
    """
    Download bytes and return (content, filename).
    Attempts to guess a sensible filename extension.
    """
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    content = r.content

    # Derive a filename from URL path; Bluesky CDN often ends with @jpeg/@png
    path = urlparse(url).path
    ext = Path(path).suffix or ""
    if not ext and "@jpeg" in path:
        ext = ".jpg"
    elif not ext and "@png" in path:
        ext = ".png"
    filename = f"bsky{ext or ''}"
    return content, filename


# --------- Main ---------
def main():
    state = load_state(STATE_FILE)
    seen = set(state.get("synced_uris", []))

    cursor = None
    batch = bsky_get_author_feed(BLUESKY_HANDLE, cursor=cursor, limit=FETCH_LIMIT)
    feed = batch.get("feed", [])

    # Oldest → newest so threads read naturally on Mastodon
    feed = list(reversed(feed))

    new_synced: List[str] = []
    for item in feed:
        post = item.get("post") or {}
        uri = post.get("uri")
        if not uri or uri in seen:
            continue

        # Skip reposts or quotes authored by others (this script focuses on original posts)
        # If needed, you can include logic for quotes by detecting post.get('embed', {'$type': 'app.bsky.embed.record#view'})
        record = post.get("record") or {}
        text = record.get("text", "") or ""
        author = post.get("author") or {}
        author_handle = author.get("handle") or BLUESKY_HANDLE

        attribution = bsky_post_url_from_uri(author_handle, uri)
        status = safe_truncate_for_masto(text, attribution, MASTO_MAX_CHARS)

        # Collect images (if any)
        embed = post.get("embed")
        image_entries = extract_images_from_embed(embed)

        media_ids: List[str] = []
        for idx, img in enumerate(image_entries):
            try:
                content, filename = download_bytes(img["url"])
                # Append index to filename to keep them distinct
                media_id = masto_upload_image(content, f"{idx}_{filename}", img.get("alt", ""))
                media_ids.append(media_id)
            except Exception as e:
                print(f"Image upload failed ({img['url']}): {e}")

        try:
            resp = masto_post_status(status, media_ids=media_ids if media_ids else None)
            print(f"Posted to Mastodon: {resp.get('url') or resp.get('uri') or '(ok)'}")
            seen.add(uri)
            new_synced.append(uri)
            # Be polite to both APIs
            time.sleep(1)
        except Exception as e:
            print(f"Failed to post status for {uri}: {e}")

    if new_synced:
        state["synced_uris"] = sorted(seen)
        save_state(STATE_FILE, state)
        print(f"Synced {len(new_synced)} new post(s).")
    else:
        print("No new posts to sync.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.")