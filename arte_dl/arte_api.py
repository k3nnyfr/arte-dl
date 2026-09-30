"""Resolve arte.tv URLs into a Series -> Season -> Episode structure.

yt-dlp can flatten an RC-xxxxxx collection, but it loses the season structure
and episode numbering, so we query the same Arte APIs it uses ourselves.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API_PLAYER = 'https://api.arte.tv/api/player/v2'
API_OPA = 'https://api.arte.tv/api/opa/v3'

try:  # keep the public token in sync with yt-dlp when it rotates
    from yt_dlp.extractor.arte import ArteTVPlaylistIE
    _OPA_TOKEN = ArteTVPlaylistIE._API_TOKEN
except (ImportError, AttributeError):
    _OPA_TOKEN = 'Nzc1Yjc1ZjJkYjk1NWFhN2I2MWEwMmRlMzAzNjI5NmU3NWU3ODg4ODJjOWMxNTMxYzEzZGRjYjg2ZGE4MmIwOA'

URL_RE = re.compile(
    r'arte\.tv/(?P<lang>fr|de|en|es|it|pl)/videos/(?P<id>RC-\d{6}|\d{6}-\d{3}-[AF])')
EPISODE_ID_RE = re.compile(r'^\d{6}-\d{3}-[AF]$')
COLLECTION_ID_RE = re.compile(r'RC-\d{6}')
SEASON_RE = re.compile(r'\b(?:saison|staffel|season|temporada|stagione|sezon)\s*(\d+)', re.I)
EPISODE_NUMBER_RE = re.compile(r'\((\d+)\s*/\s*(\d+)\)\s*$')


class ArteError(Exception):
    pass


@dataclass
class Episode:
    id: str
    url: str
    series: str
    season: int
    number: int
    title: str
    description: str | None = None
    duration: int | None = None     # seconds
    total: int | None = None        # episodes in the season, from Arte's "(n/m)"
    last_number: int | None = None  # set when the file holds several episodes (S06E01-E02)

    def __post_init__(self):
        # Arte's own numbering, kept when metadata matching renumbers the episode
        self.arte_season, self.arte_number, self.arte_title = self.season, self.number, self.title

    @property
    def numbers(self) -> list[int]:
        return list(range(self.number, (self.last_number or self.number) + 1))

    @property
    def label(self) -> str:
        return f'S{self.season:02d}' + '-'.join(f'E{n:02d}' for n in (
            [self.number, self.last_number] if self.last_number else [self.number]))


@dataclass
class Season:
    id: str
    number: int
    title: str
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class Series:
    id: str
    title: str
    lang: str
    seasons: list[Season] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # season ids with nothing online
    original_title: str | None = None
    original_language: str | None = None  # ISO 639-1
    year: int | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None

    @property
    def episodes(self) -> list[Episode]:
        return [e for s in self.seasons for e in s.episodes]


def parse_url(url: str) -> tuple[str, str]:
    m = URL_RE.search(url)
    if not m:
        raise ArteError(f'Not an arte.tv series or episode URL: {url}')
    return m['lang'], m['id']


class ArteClient:
    def __init__(self, lang: str, retries: int = 6):
        self.lang = lang
        self.retries = retries

    def _get(self, url: str, headers: dict[str, str]) -> dict | None:
        """GET a JSON document; None on 404. Retries on rate limiting / server errors."""
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', **headers})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if (e.code == 429 or e.code >= 500) and attempt < self.retries:
                    delay = int(e.headers.get('Retry-After') or 0) or 2 ** attempt
                    time.sleep(min(delay, 60))
                    continue
                raise ArteError(f'HTTP {e.code} for {url}') from e
            except urllib.error.URLError as e:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ArteError(f'{e.reason} for {url}') from e
        raise AssertionError('unreachable')

    def program(self, program_id: str) -> dict:
        data = self._get(f'{API_OPA}/programs/{self.lang}/{program_id}',
                         {'Authorization': f'Bearer {_OPA_TOKEN}'})
        programs = (data or {}).get('programs') or []
        if not programs:
            raise ArteError(f'Unknown Arte program {program_id}')
        return programs[0]

    def playlist(self, collection_id: str) -> dict | None:
        data = self._get(f'{API_PLAYER}/playlist/{self.lang}/{collection_id}',
                         {'x-validated-age': '18'})
        return ((data or {}).get('data') or {}).get('attributes')

    # --- resolution -------------------------------------------------------

    def resolve(self, url: str) -> Series:
        """Series URL -> every season; season URL -> that season; episode URL -> that episode."""
        _, pid = parse_url(url)
        if EPISODE_ID_RE.match(pid):
            return self._resolve_episode(pid)

        prog = self.program(pid)
        if prog.get('catalogType') == 'SEASON':
            series_id = next((p for p in prog.get('parents') or []
                              if COLLECTION_ID_RE.fullmatch(p) and p != pid), None)
            if series_id:
                return self._series(series_id, only_season=pid)
            return self._series(pid)  # orphan season: treat as its own series
        return self._series(pid, prog=prog)

    def _resolve_episode(self, episode_id: str) -> Series:
        prog = self.program(episode_id)
        collections = prog.get('collections') or []
        season = next((c for c in collections if c.get('catalogType') == 'SEASON'), None)
        coll = season or (collections[0] if collections else None)
        series_id = None
        if coll:
            m = COLLECTION_ID_RE.search(coll.get('url') or '')
            series_id = m[0] if m else coll.get('collectionId')
        if series_id:
            series = self._series(series_id, only_season=season and season.get('collectionId'),
                                  only_episode=episode_id)
            if any(s.episodes for s in series.seasons):
                return series
        # Standalone program, or not found in its collection: single episode, season 1.
        title = prog.get('title') or episode_id
        ep = Episode(id=episode_id, url=f'https://www.arte.tv/{self.lang}/videos/{episode_id}/',
                     series=title, season=1, number=1, title=prog.get('subtitle') or title,
                     description=prog.get('shortDescription'))
        return Series(id=episode_id, title=title, lang=self.lang,
                      seasons=[Season(id=episode_id, number=1, title=title, episodes=[ep])],
                      original_title=(prog.get('originalTitle') or '').strip(' ()') or None,
                      original_language=(prog.get('originalLanguage') or {}).get('iso6391Code'),
                      year=prog.get('productionYear'))

    def _series(self, series_id: str, *, prog: dict | None = None,
                only_season: str | None = None, only_episode: str | None = None) -> Series:
        prog = prog or self.program(series_id)
        series = Series(id=series_id, title=(prog.get('title') or series_id).strip(), lang=self.lang,
                        original_title=(prog.get('originalTitle') or '').strip(' ()') or None,
                        original_language=(prog.get('originalLanguage') or {}).get('iso6391Code'),
                        year=prog.get('productionYear'))

        refs = [c for c in prog.get('children') or [] if c.get('catalogType') == 'SEASON']
        refs.sort(key=lambda c: c.get('order') or 0)
        if not refs:  # mini-series: the collection itself holds the episodes
            refs = [{'programId': series_id}]

        for idx, ref in enumerate(refs, 1):
            sid = ref['programId']
            if only_season and sid != only_season:
                continue
            attrs = self.playlist(sid)
            items = (attrs or {}).get('items') or []
            if not items and sid == series_id:
                items = self._fallback_items(prog)
            if not items:
                series.unavailable.append(sid)
                continue
            stitle = ((attrs or {}).get('metadata') or {}).get('title') or series.title
            m = SEASON_RE.search(stitle)
            number = int(m[1]) if m else (ref.get('order') or idx)
            season = Season(id=sid, number=number, title=stitle)
            season.episodes = self._episodes(items, series.title, number, only_episode)
            if season.episodes:
                series.seasons.append(season)
        series.seasons.sort(key=lambda s: s.number)
        return series

    def _fallback_items(self, prog: dict) -> list[dict]:
        """Build playlist-like items from the OPA 'videos' list."""
        return [{'providerId': v.get('programId'), 'title': v.get('title'),
                 'subtitle': v.get('subtitle'), 'link': {'url': v.get('url')},
                 'description': v.get('shortDescription')}
                for v in prog.get('videos') or [] if v.get('kind') == 'SHOW']

    def _episodes(self, items: list[dict], series_title: str, season: int,
                  only_episode: str | None) -> list[Episode]:
        episodes = []
        position = 0
        for it in items:
            pid = it.get('providerId') or ''
            if not EPISODE_ID_RE.match(pid):
                continue
            position += 1
            raw_title = (it.get('title') or '').strip()
            m = EPISODE_NUMBER_RE.search(raw_title)
            number = int(m[1]) if m else position
            total = int(m[2]) if m else None
            title = (it.get('subtitle') or '').strip() or (f'Épisode {number}' if m else raw_title)
            if only_episode and pid != only_episode:
                continue
            episodes.append(Episode(
                id=pid,
                url=((it.get('link') or {}).get('url')
                     or f'https://www.arte.tv/{self.lang}/videos/{pid}/'),
                series=series_title, season=season, number=number, title=title,
                description=it.get('description'),
                duration=(it.get('duration') or {}).get('seconds'), total=total))
        return episodes
