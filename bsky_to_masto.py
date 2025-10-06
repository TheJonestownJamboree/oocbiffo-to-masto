#!/usr/bin/env python3
"""
Bluesky → Mastodon crossposter (no Bluesky login).
- Fetches public posts from a Bluesky handle (AppView public API)
- Reposts text + images (with alt) to Mastodon
- Adds attribution link back to Bluesky
- Avoids duplicates via a local state file
- Filters strictly to posts authored by the target (skip reposts & non-author items)
- Supports --dry-run and --verbose switches
- Guard to avoid link-only "card" duplicates: REQUIRE_IMAGES=true

Requirements:
  pip install requests python-dotenv
"""

import argparse
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


# --------- CLI & Env ---------
def strtobool(s: Optional[str]) -> bool:
    if not s:
        return False
    return s.strip().lower() in ("1", "true", "t", "yes", "y", "on")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crosspost public Bluesky posts to Mastodon.")
    p.add_argument("--dry-run", action="store_true", help="Print actions without posting.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args()


# Load .env first so env fallbacks work
load_dotenv()

# CLI overrides env
_args = parse_args()
DRY_RUN = _args.dry_run or strtobool(os.getenv("DRY_RUN"))
VERBOSE = _args.verbose or strtobool(os.getenv("VERBOSE"))

BLUESKY_HANDLE = os.getenv("BLUESKY_HANDLE", "").strip()
BLUESKY_FILTER = os.getenv("BLUESKY_FILTER", "posts_with_media")  # media-only by default
MASTO_BASE = os.getenv("MASTODON_BASE_URL", "").rstrip("/")
MASTO_TOKEN = os.getenv("MASTODON_ACCESS_TOKEN", "").strip()
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
MASTO_VISIBILITY = os.getenv("MASTODON_VISIBILITY", "public")
MASTO_MAX_CHARS = int(os.getenv("MASTODON_MAX_CHARS", "500"))
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "20"))  # Bluesky fetch batch size (≤100)
REQUIRE_IMAGES = os.getenv("REQUIRE_IMAGES", "false").strip().lower() in ("1","true","yes","y","on")

HEADERS_JSON = {
    "Authorization": f"Bearer {MASTO_TOKEN}",
    "Content-Type": "application/json",
}
HEADERS_AUTH = {"Authorization": f"Bearer {MASTO_TOKEN}"}


# --------- Logging ---------
def vlog(msg: str):
    if VERBOSE:
        print(f"[VERBOSE] {msg}")


def info(msg: str):
    print(msg)


def warn(msg: str):
    print(f"Warning: {msg}")


def err(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)


# --------- Guard rails ---------
def validate_config():
    missing = []
    if not BLUESKY_HANDLE:
        missing.append("BLUESKY_HANDLE")
    if not MASTO_BASE:
        missing.append("MASTODON_BASE_URL")
    if not MASTO_TOKEN and not DRY_RUN:
        missing.append("MASTODON_ACCESS_TOKEN")

    if missing:
        err(f"Missing required config: {', '.join(missing)}")
        sys.exit(1)

    if VERBOSE:
        info("Configuration:")
        info(f"  BLUESKY_HANDLE={BLUESKY_HANDLE}")
        info(f"  BLUESKY_FILTER={BLUESKY_FILTER}")
        info(f"  MASTODON_BASE_URL={MASTO_BASE}")
        info(f"  STATE_FILE={STATE_FILE}")
        info(f"  MASTODON_VISIBILITY={MASTO_VISIBILITY}")
        info(f"  MASTODON_MAX_CHARS={MASTO_MAX_CHARS}")
        info(f"  FETCH_LIMIT={FETCH_LIMIT}")
        info(f"  REQUIRE_IMAGES={REQUIRE_IMAGES}")
        info(f"  DRY_RUN={DRY_RUN}")
        info(f"  VERBOSE={VERBOSE}")


# --------- State helpers ---------
def load_state(path: str) -> Dict:
    if Path(path).is_file():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"synced_uris": []}


def save_state(path: str, state: Dict) -> None:
    if DRY_RUN:
        vlog("Dry-run active: not saving state.")
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------- Bluesky API ---------
def bsky_get_profile(handle: str) -> dict:
    """
    Resolve a Bluesky handle to its DID (public endpoint).
    """
    url = f"{APPVIEW}/xrpc/app.bsky.actor.getProfile"
    r = requests.get(url, params={"actor": handle}, timeout=30)
    r.raise_for_status()
    return r.json()


def bsky_get_author_feed(handle: str, cursor: Optional[str] = None, limit: int = 20) -> Dict:
    """
    GET app.bsky.feed.getAuthorFeed (no auth required).
    Supports `filter` values like posts_with_media, posts_no_replies, posts_with_video, etc.
    """
    params = {
        "actor": handle,
        "filter": BLUESKY_FILTER,
        "limit": min(max(1, limit), 100),
    }
    if cursor:
        params["cursor"] = cursor
    url = f"{APPVIEW}/xrpc/app.bsky.feed.getAuthorFeed"
    vlog(f"Fetching Bluesky author feed: {url} params={params}")
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
    if DRY_RUN:
        vlog(f"[dry-run] Would upload image: filename={filename}, alt='{alt_text[:60]}'")
        return f"dryrun-media-{int(time.time()*1000)}"

    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    files = {"file": (filename, img_bytes, mime_type)}
    data = {"description": alt_text or ""}
    url = f"{MASTO_BASE}/api/v2/media"
    vlog(f"Uploading media to Mastodon: {url}, filename={filename}, mime={mime_type}")
    r = requests.post(url, headers=HEADERS_AUTH, files=files, data=data, timeout=120)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Mastodon media upload failed: {r.status_code} {r.text}")

    media = r.json()
    media_id = media["id"]

    # If asynchronous (202 or url is null), poll until ready.
    if r.status_code == 202 or not media.get("url"):
        status_url = f"{MASTO_BASE}/api/v1/media/{media_id}"
        vlog(f"Polling media until ready: {status_url}")
        for _ in range(60):  # up to ~60s
            time.sleep(1)
            rr = requests.get(status_url, headers=HEADERS_AUTH, timeout=30)
            rr.raise_for_status()
            media = rr.json()
            if media.get("url"):
                break
        else:
            warn(f"media {media_id} not fully processed yet; proceeding.")
    return media_id


def masto_post_status(text: str, media_ids: Optional[List[str]] = None) -> Dict:
    if DRY_RUN:
        info("[dry-run] Would post Mastodon status:")
        info(text)
        if media_ids:
            info(f"[dry-run] Would attach media_ids: {media_ids}")
        return {"dry_run": True}

    payload = {"status": text, "visibility": MASTO_VISIBILITY}
    if media_ids:
        payload["media_ids"] = media_ids
    url = f"{MASTO_BASE}/api/v1/statuses"
    vlog(f"Posting to Mastodon: {url} payload_keys={list(payload.keys())}")
    r = requests.post(url, headers=HEADERS_JSON, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


# --------- Helpers ---------
def safe_truncate_for_masto(text: str, link: str, max_chars: int) -> str:
    base = text.strip()
    suffix = f"\n\nOriginal Bluesky: {link}"
    if len(base) + len(suffix) <= max_chars:
        return base + suffix
    allowed = max_chars - len(suffix) - 1
    allowed = max(0, allowed)
    truncated = (base[:allowed] + "…") if len(base) > allowed else base
    return truncated + suffix


def download_bytes(url: str) -> Tuple[bytes, str]:
    if DRY_RUN:
        vlog(f"[dry-run] Would download image bytes from: {url}")
        return b"", "bsky.jpg"
    vlog(f"Downloading image: {url}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    content = r.content
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
    validate_config()

    # Resolve DID for reliable author filtering (public endpoint)
    profile = bsky_get_profile(BLUESKY_HANDLE)
    target_did = profile.get("did")
    if not target_did:
        err(f"Could not resolve DID for handle {BLUESKY_HANDLE}")
        sys.exit(1)
    if VERBOSE:
        info(f"Resolved {BLUESKY_HANDLE} → DID {target_did}")

    state = load_state(STATE_FILE)
    seen = set(state.get("synced_uris", []))

    cursor = None
    batch = bsky_get_author_feed(BLUESKY_HANDLE, cursor=cursor, limit=FETCH_LIMIT)
    feed = batch.get("feed", [])
    vlog(f"Fetched {len(feed)} feed items from Bluesky.")
    feed = list(reversed(feed))  # oldest → newest

    new_synced: List[str] = []
    for item in feed:
        post = item.get("post") or {}
        uri = post.get("uri")
        if not uri:
            continue

        if uri in seen:
            vlog(f"Skipping already-synced post: {uri}")
            continue

        # Skip reposts (feed includes reposts via "reason")
        reason = item.get("reason") or {}
        if reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
            vlog(f"Skipping repost by {BLUESKY_HANDLE}: {uri}")
            continue

        # Keep ONLY posts authored by the target account (compare DID)
        author = post.get("author") or {}
        if author.get("did") != target_did:
            vlog(f"Skipping non-author post from {author.get('handle')} for uri={uri}")
            continue

        record = post.get("record") or {}
        text = record.get("text", "") or ""
        author_handle = author.get("handle") or BLUESKY_HANDLE

        attribution = bsky_post_url_from_uri(author_handle, uri)
        status = safe_truncate_for_masto(text, attribution, MASTO_MAX_CHARS)
        vlog(f"Prepared status length={len(status)} for uri={uri}")

        # Images (if any)
        embed = post.get("embed")
        image_entries = extract_images_from_embed(embed)
        if image_entries:
            vlog(f"Found {len(image_entries)} image(s) for uri={uri}")

        # === Guards to avoid link-only duplicate posts ===
        # Require images if configured (skips link-only posts that render as a card)
        if REQUIRE_IMAGES and not image_entries:
            vlog(f"Skipping post without images to avoid link-card duplicate: {uri}")
            continue
        # Also skip when status would be only the attribution link
        only_attribution = status.strip().lower().startswith("original bluesky: ") and (not image_entries)
        if only_attribution:
            vlog(f"Skipping link-only/attribution-only status: {uri}")
            continue
        # ================================================

        media_ids: List[str] = []
        for idx, img in enumerate(image_entries):
            try:
                content, filename = download_bytes(img["url"])
                media_id = masto_upload_image(content, f"{idx}_{filename}", img.get("alt", ""))
                media_ids.append(media_id)
            except Exception as e:
                warn(f"Image upload failed ({img['url']}): {e}")

        try:
            resp = masto_post_status(status, media_ids=media_ids if media_ids else None)
            if not DRY_RUN:
                info(f"Posted to Mastodon: {resp.get('url') or resp.get('uri') or '(ok)'}")
                seen.add(uri)
                new_synced.append(uri)
            else:
                info(f"[dry-run] Would mark as synced: {uri}")
            time.sleep(1)  # be polite
        except Exception as e:
            err(f"Failed to post status for {uri}: {e}")

    if not DRY_RUN and new_synced:
        state["synced_uris"] = sorted(seen)
        save_state(STATE_FILE, state)
        info(f"Synced {len(new_synced)} new post(s).")
    elif DRY_RUN:
        info("Dry-run complete. State not saved.")
    else:
        info("No new posts to sync.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.")
