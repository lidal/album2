"""
Extended album artwork: back cover + booklet pages from online sources.

The front cover already comes reliably from Mopidy (embedded / backend art),
so this module focuses on the parts we otherwise lack — the back cover and
booklet scans — and is written to be extended with more sources later.

Primary source: MusicBrainz (release lookup) + the Cover Art Archive (the
scans themselves).  Because a single album exists as many MusicBrainz
"releases" (pressings/editions) and each release may have scanned different
parts, we search the *release group*, enumerate its releases, and merge the
art across them: front from wherever it is best, back from wherever, and the
booklet from whichever release scanned the most complete set.  Duplicates
(the same scan attached to many releases) are collapsed by content hash.

Wide booklet spreads (two facing pages in one scan) are split down the middle
so each page fills the square screen on its own.

API etiquette:
  * MusicBrainz WS/2 enforces ~1 request/second per IP (HTTP 503 otherwise)
    and requires a descriptive User-Agent with contact info — both handled
    here by a shared limiter and the session header.
  * The Cover Art Archive (coverartarchive.org) has no rate limit, so its
    listing and image requests are not throttled; we only retry gently on the
    slow/occasionally-failing archive.org image redirects.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import requests
from PIL import Image

log = logging.getLogger(__name__)

_ARTWORK_CACHE_DIR = os.path.expanduser("~/.cache/album2/artwork")
_ARTWORK_INDEX_PATH = os.path.expanduser("~/.cache/album2/artwork_index.json")

# Contact string baked into the User-Agent, per MusicBrainz policy.
_CONTACT = "jonaslidal@gmail.com"
_USER_AGENT = f"album2/1.0 ( {_CONTACT} )"

# Type ordering for the on-screen carousel.  Front is supplied by the player's
# embedded art (image 0), so by default this module fetches only what's missing.
_TYPE_ORDER = {"Front": 0, "Back": 1, "Booklet": 2, "Medium": 3,
               "Tray": 4, "Spine": 5, "Obi": 6, "Other": 9}
_DEFAULT_TYPES = ("Back", "Booklet")

# A scan wider than this ratio is treated as a two-page spread and split.
_SPREAD_RATIO = 1.5
# Longest edge stored on disk (screen is 720px; 1000 leaves headroom to crop).
_STORE_MAX_PX = 1000
# Cap releases inspected per album so a huge release group can't stall forever.
_MAX_RELEASES = 12


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


# ── rate limiter ──────────────────────────────────────────────────────────────

class _RateLimiter:
    """Serialize calls to a host and guarantee a minimum spacing between them."""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


# MusicBrainz enforces ~1 req/s per IP; the Cover Art Archive has no limit.
_mb_limiter = _RateLimiter(1.1)


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ArtRef:
    """A single artwork image advertised by a source, not yet downloaded."""
    type: str            # normalized: Front | Back | Booklet | ...
    url: str             # direct image URL (may redirect)
    source: str          # provider id, e.g. "caa"
    approved: bool = False
    order: int = 0       # position within its type (booklet page order)


@dataclass
class FetchResult:
    images: list[str] = field(default_factory=list)   # ordered file paths
    found: int = 0


# ── provider base + MusicBrainz/CAA implementation ────────────────────────────

class ArtworkProvider:
    """Base class for artwork sources. Implement `collect`."""
    id = "base"

    def collect(self, artist: str, album: str, track_count: int,
                types: tuple[str, ...]) -> list[ArtRef]:
        raise NotImplementedError


class MusicBrainzCAAProvider(ArtworkProvider):
    """MusicBrainz release-group search + Cover Art Archive per-release scans."""
    id = "caa"

    _MB = "https://musicbrainz.org/ws/2"
    _CAA = "https://coverartarchive.org"

    def __init__(self, session: requests.Session):
        self._s = session

    # -- HTTP helpers --

    def _mb_get(self, path: str, **params) -> dict:
        params["fmt"] = "json"
        _mb_limiter.wait()
        try:
            r = self._s.get(f"{self._MB}/{path}", params=params, timeout=15)
            if r.status_code == 503:
                # MusicBrainz throttled us — back off once and retry.
                time.sleep(2.0)
                _mb_limiter.wait()
                r = self._s.get(f"{self._MB}/{path}", params=params, timeout=15)
            if r.status_code != 200:
                log.debug("MB %s -> %s", path, r.status_code)
                return {}
            return r.json()
        except Exception as e:
            log.debug("MB %s failed: %s", path, e)
            return {}

    def _caa_get(self, entity: str, mbid: str) -> dict:
        for attempt in range(2):
            try:
                r = self._s.get(f"{self._CAA}/{entity}/{mbid}", timeout=15)
                if r.status_code == 404:
                    return {}          # no art for this entity — normal
                if r.status_code == 200:
                    return r.json()
                log.debug("CAA %s/%s -> %s", entity, mbid, r.status_code)
            except Exception as e:
                log.debug("CAA %s/%s failed (try %d): %s", entity, mbid, attempt, e)
            time.sleep(0.5)
        return {}

    # -- collection --

    @staticmethod
    def _norm_type(types: list[str] | None, front: bool, back: bool) -> str:
        if front:
            return "Front"
        if back:
            return "Back"
        if not types:
            return "Other"
        for cand in ("Front", "Back", "Booklet", "Medium", "Tray", "Spine", "Obi"):
            if cand in types:
                return cand
        return "Other"

    def _release_ids(self, artist: str, album: str, track_count: int) -> list[str]:
        """Return release MBIDs for the album, best-matching pressings first."""
        # Escape Lucene-special double quotes in the query terms.
        qa = artist.replace('"', " ").strip()
        ql = album.replace('"', " ").strip()
        data = self._mb_get(
            "release-group",
            query=f'artist:"{qa}" AND releasegroup:"{ql}"',
            limit=5,
        )
        groups = data.get("release-groups", [])
        if not groups:
            return []
        # Highest-scoring group wins; ignore weak fuzzy matches.
        best = groups[0]
        if int(best.get("score", 0)) < 85:
            log.info("artwork: weak MB match for %s - %s (score %s) — skipping",
                     artist, album, best.get("score"))
            return []
        rg_id = best["id"]
        rel_data = self._mb_get("release", **{"release-group": rg_id,
                                              "limit": 50, "inc": "media"})
        releases = rel_data.get("releases", [])

        def track_total(rel: dict) -> int:
            return sum(m.get("track-count", 0) for m in rel.get("media", []))

        # Prefer pressings whose track count matches the album we actually have,
        # so booklet/back scans line up with the right edition.
        def sort_key(rel: dict):
            tt = track_total(rel)
            matches = (track_count > 0 and tt == track_count)
            return (0 if matches else 1, abs(tt - track_count) if track_count else 0)

        releases.sort(key=sort_key)
        return [r["id"] for r in releases[:_MAX_RELEASES]]

    def collect(self, artist: str, album: str, track_count: int,
                types: tuple[str, ...]) -> list[ArtRef]:
        want = set(types)
        release_ids = self._release_ids(artist, album, track_count)
        if not release_ids:
            return []

        # Gather candidate images per type across all releases.
        by_type: dict[str, list[ArtRef]] = {}
        booklets: dict[str, list[ArtRef]] = {}   # release_id -> ordered booklet refs

        for rel_id in release_ids:
            listing = self._caa_get("release", rel_id)
            images = listing.get("images", []) if listing else []
            rel_booklet: list[ArtRef] = []
            for order, img in enumerate(images):
                ntype = self._norm_type(img.get("types"),
                                        img.get("front", False),
                                        img.get("back", False))
                if ntype not in want:
                    continue
                # Prefer a sensibly-sized thumbnail over the full original
                # (originals can be 20MB scans); fall back to the original.
                thumbs = img.get("thumbnails", {}) or {}
                url = (thumbs.get("1200") or thumbs.get("500")
                       or img.get("image") or thumbs.get("250"))
                if not url:
                    continue
                ref = ArtRef(type=ntype, url=url, source=self.id,
                             approved=bool(img.get("approved")), order=order)
                if ntype == "Booklet":
                    rel_booklet.append(ref)
                else:
                    by_type.setdefault(ntype, []).append(ref)
            if rel_booklet:
                booklets[rel_id] = rel_booklet
            # Early exit: once we have a back and a booklet set, stop scanning
            # more pressings (avoids dozens of CAA calls on huge release groups).
            have_back = "Back" not in want or by_type.get("Back")
            have_booklet = "Booklet" not in want or booklets
            if have_back and have_booklet:
                break

        result: list[ArtRef] = []
        # One best Front/Back etc.: prefer approved.
        for ntype, refs in by_type.items():
            refs.sort(key=lambda r: (0 if r.approved else 1, r.order))
            result.append(refs[0])
        # Booklet: take the most complete single set (coherent edition).
        if booklets:
            best_set = max(booklets.values(), key=len)
            result.extend(best_set)
        return result


# ── orchestrator ──────────────────────────────────────────────────────────────

class ArtworkFetcher:
    """Fetches, caches, and serves extended artwork for albums.

    Cache layout::

        ~/.cache/album2/artwork/<md5(album_uri)>/
            manifest.json          # [{file, type, source}, ...]  ([] = none found)
            01_back.jpg
            02_booklet.jpg
            ...
        ~/.cache/album2/artwork_index.json   # [album_uri, ...]  fetched/attempted

    An empty manifest is a sentinel: the album was looked up and nothing was
    found, so it is not re-queried on every open.
    """

    def __init__(self, types: tuple[str, ...] = _DEFAULT_TYPES):
        self._types = types
        os.makedirs(_ARTWORK_CACHE_DIR, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._providers: list[ArtworkProvider] = [
            MusicBrainzCAAProvider(self._session),
        ]
        self._index: set[str] = self._load_index()
        self._index_lock = threading.Lock()

    # -- index --

    def _load_index(self) -> set[str]:
        try:
            with open(_ARTWORK_INDEX_PATH) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_index(self):
        try:
            os.makedirs(os.path.dirname(_ARTWORK_INDEX_PATH), exist_ok=True)
            with self._index_lock:
                data = list(self._index)
            tmp = _ARTWORK_INDEX_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, _ARTWORK_INDEX_PATH)
        except Exception as e:
            log.warning("artwork: index save failed: %s", e)

    def is_done(self, album_uri: str) -> bool:
        with self._index_lock:
            return album_uri in self._index

    @property
    def index_size(self) -> int:
        with self._index_lock:
            return len(self._index)

    # -- cache access --

    def _album_dir(self, album_uri: str) -> str:
        return os.path.join(_ARTWORK_CACHE_DIR, _md5(album_uri))

    def cached_images(self, album_uri: str) -> list[str] | None:
        """Ordered image paths for the album, or None if never fetched.

        Returns an empty list when the album was fetched but had no extra art.
        """
        d = self._album_dir(album_uri)
        manifest = os.path.join(d, "manifest.json")
        if not os.path.exists(manifest):
            return None
        try:
            with open(manifest) as f:
                entries = json.load(f)
        except Exception:
            return None
        paths = []
        for e in entries:
            p = os.path.join(d, e["file"])
            if os.path.exists(p):
                paths.append(p)
        return paths

    # -- fetching --

    def fetch(self, album_uri: str, artist: str, album: str,
              track_count: int = 0, force: bool = False, on_image=None) -> list[str]:
        """Fetch (or return cached) extended artwork; returns ordered paths.

        *on_image*, if given, is called with each image path as it is saved,
        so callers can populate a UI progressively during a slow fetch.
        """
        if not force:
            cached = self.cached_images(album_uri)
            if cached is not None:
                if on_image:
                    for p in cached:
                        on_image(p)
                return cached
        try:
            return self._do_fetch(album_uri, artist, album, track_count, on_image)
        except Exception as e:
            log.warning("artwork: fetch failed for %s (%s - %s): %s",
                        album_uri, artist, album, e)
            return []

    def _do_fetch(self, album_uri: str, artist: str, album: str,
                  track_count: int, on_image=None) -> list[str]:
        refs: list[ArtRef] = []
        for provider in self._providers:
            try:
                refs.extend(provider.collect(artist, album, track_count, self._types))
            except Exception as e:
                log.debug("artwork: provider %s failed: %s", provider.id, e)

        d = self._album_dir(album_uri)
        os.makedirs(d, exist_ok=True)

        seen_hashes: set[str] = set()
        manifest: list[dict] = []
        seq = 0

        # Stable order: Front, Back, Booklet(page order), then the rest.
        refs.sort(key=lambda r: (_TYPE_ORDER.get(r.type, 8), r.order))

        for ref in refs:
            raw = self._download(ref.url)
            if not raw:
                continue
            h = hashlib.sha1(raw).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:
                continue
            # Split a two-page booklet spread into single pages.
            pages = self._split_spread(img) if ref.type in ("Booklet", "Other") else [img]
            for page in pages:
                page = self._downscale(page)
                seq += 1
                fname = f"{seq:02d}_{ref.type.lower()}.jpg"
                fpath = os.path.join(d, fname)
                try:
                    page.save(fpath, "JPEG", quality=88)
                except Exception as e:
                    log.debug("artwork: save %s failed: %s", fname, e)
                    continue
                manifest.append({"file": fname, "type": ref.type, "source": ref.source})
                # Persist the manifest incrementally so a fetch interrupted
                # partway through still leaves a usable (and resumable) set,
                # and notify the caller so the UI can show the page now.
                self._write_manifest(d, manifest)
                if on_image:
                    try:
                        on_image(fpath)
                    except Exception:
                        pass

        # Final manifest (possibly empty = sentinel) and mark done.
        self._write_manifest(d, manifest)
        with self._index_lock:
            self._index.add(album_uri)
        self._save_index()

        log.info("artwork: %s - %s → %d image(s)", artist, album, len(manifest))
        return [os.path.join(d, e["file"]) for e in manifest]

    @staticmethod
    def _write_manifest(album_dir: str, manifest: list[dict]):
        try:
            tmp = os.path.join(album_dir, "manifest.json.tmp")
            with open(tmp, "w") as f:
                json.dump(manifest, f)
            os.replace(tmp, os.path.join(album_dir, "manifest.json"))
        except Exception as e:
            log.warning("artwork: manifest save failed: %s", e)

    def _download(self, url: str) -> bytes | None:
        for attempt in range(3):
            try:
                r = self._session.get(url, timeout=25)
                if r.status_code == 200 and r.content:
                    return r.content
                if r.status_code == 404:
                    return None
            except Exception as e:
                log.debug("artwork: download %s try %d: %s", url[-40:], attempt, e)
            time.sleep(1.0 + attempt)
        return None

    @staticmethod
    def _split_spread(img: Image.Image) -> list[Image.Image]:
        w, h = img.size
        if h > 0 and w / h >= _SPREAD_RATIO:
            mid = w // 2
            return [img.crop((0, 0, mid, h)), img.crop((mid, 0, w, h))]
        return [img]

    @staticmethod
    def _downscale(img: Image.Image) -> Image.Image:
        w, h = img.size
        longest = max(w, h)
        if longest > _STORE_MAX_PX:
            scale = _STORE_MAX_PX / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        return img
