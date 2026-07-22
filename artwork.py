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
import re
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
# Prefer English/Norwegian-market pressings (right script, expected artwork).
# UK == GB; XE = Europe-wide, XW = Worldwide.
_PREFERRED_COUNTRIES = {"US", "GB", "CA", "XE", "XW", "NO"}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


# Edition/format qualifiers that appear in streaming album titles but not in
# MusicBrainz release-group titles — stripped before searching.
_EDITION_KW = ("remaster", "deluxe", "expanded", "edition", "anniversary",
               "reissue", "bonus", "mono", "stereo", "legacy", "collector",
               "remastered", "super deluxe")
_PAREN_RE = re.compile(r"[\(\[][^\(\)\[\]]*[\)\]]")
_TAIL_RE = re.compile(
    r"\s*-\s*[^-]*(remaster|deluxe|edition|reissue|mono|stereo|anniversary)[^-]*$",
    re.I)


def _clean_album_name(name: str) -> str:
    """Strip edition/remaster qualifiers so the title matches MusicBrainz.

    Only removes a parenthetical/bracketed group when it actually contains an
    edition keyword, so real titles like "(What's the Story) Morning Glory?"
    are preserved.
    """
    def _strip_group(m):
        return "" if any(k in m.group(0).lower() for k in _EDITION_KW) else m.group(0)
    cleaned = _PAREN_RE.sub(_strip_group, name)
    cleaned = _TAIL_RE.sub("", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned or name


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

    def _pick_release_group(self, artist: str, album: str) -> str:
        """Return the best-matching release-group MBID, or ''.

        Album titles like "Paranoid" match both the studio album *and* a
        same-named single/live/remix release group, often all at score 100 —
        so we can't just take the first hit.  Prefer a plain studio Album
        (primary-type Album, no Live/Remix/Compilation secondary types), then
        score, then the earliest release (the original edition).
        """
        qa = artist.replace('"', " ").strip()
        ql = _clean_album_name(album).replace('"', " ").strip()
        data = self._mb_get("release-group",
                            query=f'artist:"{qa}" AND releasegroup:"{ql}"', limit=10)
        groups = [g for g in data.get("release-groups", [])
                  if int(g.get("score", 0)) >= 85]
        if not groups:
            log.info("artwork: no MB match for %s - %s", artist, ql)
            return ""

        def rank(g: dict):
            secondary = g.get("secondary-types") or []
            return (
                0 if g.get("primary-type") == "Album" else 1,
                len(secondary),                          # studio > live/remix/comp
                -int(g.get("score", 0)),
                g.get("first-release-date") or "9999",   # original edition first
            )

        best = min(groups, key=rank)
        sec = best.get("secondary-types") or []
        log.info("artwork: MB group %s (%s%s) for %s - %s",
                 best["id"][:8], best.get("primary-type"),
                 "/" + ",".join(sec) if sec else "", artist, ql)
        return best["id"]

    def _release_ids(self, artist: str, album: str, track_count: int) -> list[str]:
        """Return release MBIDs for the album, best-matching pressings first:
        preferred-country and track-count-matching editions lead."""
        rg_id = self._pick_release_group(artist, album)
        if not rg_id:
            return []
        rel_data = self._mb_get("release", **{"release-group": rg_id,
                                              "limit": 100, "inc": "media"})
        releases = rel_data.get("releases", [])

        def track_total(rel: dict) -> int:
            return sum(m.get("track-count", 0) for m in rel.get("media", []))

        def sort_key(rel: dict):
            country = rel.get("country") or ""
            tt = track_total(rel)
            country_rank = (0 if country in _PREFERRED_COUNTRIES
                            else 1 if country == "" else 2)
            tc_match = 0 if (track_count > 0 and tt == track_count) else 1
            return (country_rank, tc_match, abs(tt - track_count) if track_count else 0)

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

        # Download everything first (exact-byte dedup for identical listings).
        refs.sort(key=lambda r: (_TYPE_ORDER.get(r.type, 8), r.order))
        seen_hashes: set[str] = set()
        downloaded: list[tuple[ArtRef, Image.Image]] = []
        for ref in refs:
            raw = self._download(ref.url)
            if not raw:
                continue
            h = hashlib.sha1(raw).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            try:
                downloaded.append((ref, Image.open(io.BytesIO(raw)).convert("RGB")))
            except Exception:
                continue

        # A booklet can arrive two ways: individual square page scans, or one
        # whole-pamphlet strip (all pages in a single wide/tall image).  Some
        # releases carry *both*; they're the same content at different
        # resolutions, so a byte/pixel hash can't pair them.  Instead: split
        # any strip into pages and keep whichever representation is more
        # complete — ties favour the individual scans (higher quality).
        booklet_indiv: list[tuple[ArtRef, Image.Image]] = []
        booklet_strip: list[tuple[ArtRef, Image.Image]] = []
        others:        list[tuple[ArtRef, Image.Image]] = []
        for ref, img in downloaded:
            if ref.type == "Booklet":
                pages = self._split_spread(img)
                if len(pages) > 1:
                    booklet_strip += [(ref, p) for p in pages]
                else:
                    booklet_indiv.append((ref, img))
            else:
                others.append((ref, img))

        if len(booklet_indiv) >= len(booklet_strip):
            chosen = booklet_indiv
            if booklet_strip:
                log.info("artwork: %s - %s: kept %d individual booklet scans, "
                         "dropped %d strip page(s)", artist, album,
                         len(booklet_indiv), len(booklet_strip))
        else:
            chosen = booklet_strip
            if booklet_indiv:
                log.info("artwork: %s - %s: kept %d strip pages, dropped %d "
                         "individual scan(s)", artist, album,
                         len(booklet_strip), len(booklet_indiv))

        # Assemble in display order: Back etc. by type, then booklet in order.
        ordered: list[tuple[tuple, ArtRef, Image.Image]] = []
        for ref, img in others:
            ordered.append(((_TYPE_ORDER.get(ref.type, 8), ref.order, 0), ref, img))
        for i, (ref, img) in enumerate(chosen):
            ordered.append(((_TYPE_ORDER.get("Booklet", 2), ref.order, i), ref, img))
        ordered.sort(key=lambda t: t[0])

        manifest: list[dict] = []
        seq = 0
        for _, ref, img in ordered:
            img = self._downscale(img)
            seq += 1
            fname = f"{seq:02d}_{ref.type.lower()}.jpg"
            fpath = os.path.join(d, fname)
            try:
                img.save(fpath, "JPEG", quality=88)
            except Exception as e:
                log.debug("artwork: save %s failed: %s", fname, e)
                continue
            manifest.append({"file": fname, "type": ref.type, "source": ref.source})
            # Persist incrementally (usable/resumable if interrupted) and let
            # the caller show the page as it lands.
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
        """Split a multi-page scan into single ~square pages.

        A whole booklet can arrive as one wide strip (all pages side by side)
        or one tall strip; split into as many equal panels as the aspect ratio
        implies (round(long/short)) so each resulting page is under the ratio.
        """
        w, h = img.size
        if w <= 0 or h <= 0:
            return [img]
        if w / h >= _SPREAD_RATIO:
            n = max(2, round(w / h))
            return [img.crop((w * i // n, 0, w * (i + 1) // n, h)) for i in range(n)]
        if h / w >= _SPREAD_RATIO:
            n = max(2, round(h / w))
            return [img.crop((0, h * i // n, w, h * (i + 1) // n)) for i in range(n)]
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
