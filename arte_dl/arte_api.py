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
# Arte genre codes (labels are localized)
GENRE_DOCUMENTARY, GENRE_CINEMA = 1, 2


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


@dataclass
class Movie:
    """A film or a documentary, possibly in several parts (Arte's "(1/2)", "(2/2)")."""
    id: str
    title: str
    lang: str
    kind: str = 'film'  # "film" or "documentary"
    parts: list[Episode] = field(default_factory=list)
    total_parts: int = 1
    description: str | None = None
    original_title: str | None = None
    original_language: str | None = None
    year: int | None = None
    tmdb_id: int | None = None
    tmdb_type: str = 'movie'  # a documentary may only exist as a TV mini-series on TMDB
    imdb_id: str | None = None

    @property
    def episodes(self) -> list[Episode]:
        return self.parts

    @property
    def multipart(self) -> bool:
        return self.total_parts > 1 or len(self.parts) > 1


def kind_of(prog: dict) -> str | None:
    """"film", "documentary" or None (series, magazine...) from an OPA program."""
    code = (prog.get('genre') or {}).get('code')
    if prog.get('catalogType') == 'MOVIE' or code == GENRE_CINEMA:
        return 'film'
    return 'documentary' if code == GENRE_DOCUMENTARY else None


def _original(prog: dict) -> dict:
    return {'original_title': (prog.get('originalTitle') or '').strip(' ()') or None,
            'original_language': (prog.get('originalLanguage') or {}).get('iso6391Code'),
            'year': prog.get('productionYear') or None}


def merge(items: list) -> list:
    """Merge the Series / Movies resolved separately from one collection's videos."""
    out: dict[str, Series | Movie] = {}
    for it in items:
        prev = out.setdefault(it.id, it)
        if prev is it:
            continue
        if isinstance(prev, Movie) and isinstance(it, Movie):
            known = {p.id for p in prev.parts}
            prev.parts += [p for p in it.parts if p.id not in known]
            prev.parts.sort(key=lambda p: p.number)
        elif isinstance(prev, Series) and isinstance(it, Series):
            seasons = {s.id: s for s in prev.seasons}
            for season in it.seasons:
                if season.id in seasons:
                    known = {e.id for e in seasons[season.id].episodes}
                    seasons[season.id].episodes += [e for e in season.episodes if e.id not in known]
                    seasons[season.id].episodes.sort(key=lambda e: e.number)
                else:
                    prev.seasons.append(season)
            prev.seasons.sort(key=lambda s: s.number)
    return list(out.values())


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

    def resolve(self, url: str, as_series: bool = False) -> list[Series | Movie]:
        """Series URL -> every season; season URL -> that season; episode URL -> that episode;
        film / documentary -> a Movie (every part of a multi-part documentary);
        thematic collection (e.g. a film trilogy) -> each of its videos.
        `as_series` keeps multi-part documentaries as mini-series."""
        _, pid = parse_url(url)
        if EPISODE_ID_RE.match(pid):
            return [self._resolve_video(pid, as_series)]

        prog = self.program(pid)
        catalog = prog.get('catalogType')
        if catalog == 'TOPIC':
            return self._topic(pid, prog, as_series)
        if catalog == 'SEASON':
            series_id = next((p for p in prog.get('parents') or []
                              if COLLECTION_ID_RE.fullmatch(p) and p != pid), None)
            if series_id:
                return [self._series(series_id, only_season=pid)]
            return [self._series(pid)]  # orphan season: treat as its own series
        if catalog == 'MINI_SERIES' and not as_series and self._collection_kind(prog) == 'documentary':
            return [self._multipart(pid, prog)]
        return [self._series(pid, prog=prog)]

    def _collection_kind(self, prog: dict) -> str | None:
        """Some collections have no genre: use their first video's."""
        kind = kind_of(prog)
        if kind or prog.get('genre'):
            return kind
        first = next((v.get('programId') for v in prog.get('videos') or []
                      if v.get('kind') == 'SHOW' and EPISODE_ID_RE.match(v.get('programId') or '')), None)
        return kind_of(self.program(first)) if first else None

    def _topic(self, pid: str, prog: dict, as_series: bool) -> list[Series | Movie]:
        """Thematic collection (trilogy, cycle...): each video on its own."""
        attrs = self.playlist(pid)
        items = (attrs or {}).get('items') or self._fallback_items(prog)
        ids = list(dict.fromkeys(it.get('providerId') for it in items
                                 if EPISODE_ID_RE.match(it.get('providerId') or '')))
        return merge([self._resolve_video(vid, as_series) for vid in ids])

    def _resolve_video(self, video_id: str, as_series: bool) -> Series | Movie:
        prog = self.program(video_id)
        kind = kind_of(prog)
        # Thematic collections ("Comédie", "Le parrain - La trilogie") aren't series
        collections = [c for c in prog.get('collections') or []
                       if c.get('catalogType') not in (None, 'TOPIC')]
        season = next((c for c in collections if c.get('catalogType') == 'SEASON'), None)
        coll = season or (collections[0] if collections and kind != 'film' else None)
        series_id = None
        if coll:
            m = COLLECTION_ID_RE.search(coll.get('url') or '')
            series_id = m[0] if m else coll.get('collectionId')
        if series_id and not season and coll.get('catalogType') == 'MINI_SERIES' and not as_series:
            cprog = self.program(series_id)
            if self._collection_kind(cprog) == 'documentary':
                movie = self._multipart(series_id, cprog, only_part=video_id)
                if movie.parts:
                    return movie
                series_id = None
        if series_id:
            series = self._series(series_id, only_season=season and season.get('collectionId'),
                                  only_episode=video_id)
            if any(s.episodes for s in series.seasons):
                return series
        # Film, standalone documentary or program: a single-file movie.
        title = (prog.get('title') or video_id).strip()
        part = Episode(id=video_id, url=f'https://www.arte.tv/{self.lang}/videos/{video_id}/',
                       series=title, season=1, number=1, title=title,
                       description=prog.get('shortDescription'), duration=prog.get('durationSeconds'))
        return Movie(id=video_id, title=title, lang=self.lang, kind=kind or 'film', parts=[part],
                     description=prog.get('shortDescription'), **_original(prog))

    def _multipart(self, pid: str, prog: dict, only_part: str | None = None) -> Movie:
        title = (prog.get('title') or pid).strip()
        items = ((self.playlist(pid) or {}).get('items')) or self._fallback_items(prog)
        parts = self._episodes(items, title, 1, None)
        total = max([p.total or 0 for p in parts] + [len(parts)])
        return Movie(id=pid, title=title, lang=self.lang, kind='documentary',
                     parts=[p for p in parts if not only_part or p.id == only_part],
                     total_parts=total, description=prog.get('shortDescription'), **_original(prog))

    def _series(self, series_id: str, *, prog: dict | None = None,
                only_season: str | None = None, only_episode: str | None = None) -> Series:
        prog = prog or self.program(series_id)
        series = Series(id=series_id, title=(prog.get('title') or series_id).strip(), lang=self.lang,
                        **_original(prog))

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
