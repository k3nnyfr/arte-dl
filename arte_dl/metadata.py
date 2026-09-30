"""TMDB matching: reliable series name / year, episode numbering and titles.

Arte's numbering doesn't always follow the reference one: e.g. from season 6,
"Meurtres à Sandhamn" episodes are 88 min on Arte but two 45 min episodes on
TMDB, so Arte's S06E01 becomes S06E01-E02 (multi-episode file, understood by
Plex and Jellyfin).
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from .arte_api import Episode, Series
from .config import MetadataConfig

API = 'https://api.themoviedb.org/3'

GENERIC_TITLE_RE = re.compile(
    r'^\s*(?:episode|épisode|episodio|folge|odcinek|avsnitt|afsnit|jakso|aflevering|épisodio)'
    r'\s*\d+\s*$', re.I)
_PART_WORD = r'(?:part(?:ie)?|teil|del|deel|parte|osa|część)'
# "X part 1", "X (part1)", "X - Partie 2", "X (2)"
PART_SUFFIX_RE = re.compile(
    rf'\s*[-,:]?\s*(?:[(\[]\s*(?:{_PART_WORD}\s*)?\d+\s*[)\]]|{_PART_WORD}\s*\d+)\s*$', re.I)

# Arte duration vs TMDB runtime(s): accepted ratio range
DURATION_TOLERANCE = (0.75, 1.3)


class TMDBError(Exception):
    pass


class TMDBClient:
    def __init__(self, key: str, retries: int = 5):
        self.key = key
        self.retries = retries

    def _get(self, path: str, **params) -> dict | None:
        headers = {'Accept': 'application/json', 'User-Agent': 'arte-dl'}
        if self.key.startswith('eyJ'):  # v4 read access token (JWT)
            headers['Authorization'] = f'Bearer {self.key}'
        else:
            params['api_key'] = self.key
        url = f'{API}{path}?{urllib.parse.urlencode(params)}'
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 401:
                    raise TMDBError('TMDB rejected the API key (HTTP 401)') from e
                if (e.code == 429 or e.code >= 500) and attempt < self.retries:
                    time.sleep(min(int(e.headers.get('Retry-After') or 0) or 2 ** attempt, 30))
                    continue
                raise TMDBError(f'HTTP {e.code} for {path}') from e
            except urllib.error.URLError as e:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise TMDBError(f'{e.reason} for {path}') from e
        raise AssertionError('unreachable')

    @lru_cache(maxsize=None)
    def search(self, query: str, language: str) -> tuple[dict, ...]:
        data = self._get('/search/tv', query=query, language=language, include_adult='false')
        return tuple((data or {}).get('results') or ())

    @lru_cache(maxsize=None)
    def show(self, show_id: int) -> dict:
        data = self._get(f'/tv/{show_id}', append_to_response='external_ids,translations')
        if not data:
            raise TMDBError(f'Unknown TMDB show {show_id}')
        return data

    @lru_cache(maxsize=None)
    def season(self, show_id: int, number: int, language: str) -> dict | None:
        return self._get(f'/tv/{show_id}/season/{number}', language=language)


# --- persisted Arte collection -> TMDB id mapping -----------------------------

def _ids_path() -> Path:
    base = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'
    return Path(base) / 'arte-dl' / 'tmdb-ids.json'


def load_ids() -> dict[str, int]:
    try:
        return json.loads(_ids_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_id(arte_id: str, tmdb_id: int) -> None:
    ids = load_ids()
    if ids.get(arte_id) == tmdb_id:
        return
    ids[arte_id] = tmdb_id
    path = _ids_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids, indent=2, sort_keys=True) + '\n')


# --- series matching ------------------------------------------------------------

def normalize(text: str) -> str:
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return ' '.join(re.sub(r'[^\w]+', ' ', text).split())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def score_candidate(series: Series, queries: list[str], result: dict) -> tuple[float, float]:
    """Returns (score, name similarity)."""
    sim = max((_similarity(q, name) for q in queries
               for name in (result.get('name'), result.get('original_name')) if name), default=0)
    score = sim
    year = (result.get('first_air_date') or '')[:4]
    if series.year and year.isdigit():
        diff = abs(int(year) - series.year)
        score += 0.2 if diff == 0 else 0.1 if diff == 1 else -0.5 if diff > 2 else 0
    if series.original_language and result.get('original_language') == series.original_language:
        score += 0.1
    return score, sim


def find_show(client: TMDBClient, series: Series, language: str) -> tuple[int | None, str]:
    """Search TMDB for the series. Returns (id or None, explanation)."""
    queries = list(dict.fromkeys(q for q in (series.original_title, series.title) if q))
    # "The Hack : sur écoute" -> also try "The Hack"
    queries += [q.split(' : ')[0] for q in queries if ' : ' in q and q.split(' : ')[0] not in queries]
    candidates = {}
    for q in queries:
        for r in client.search(q, language)[:10]:
            candidates[r['id']] = r
    scored = sorted(((score_candidate(series, queries, r), r) for r in candidates.values()),
                    key=lambda x: x[0][0], reverse=True)
    if not scored:
        return None, f'no TMDB result for {queries}'

    def describe(r):
        return f'{r.get("name")} ({(r.get("first_air_date") or "?")[:4]}) id={r["id"]}'

    (score, sim), best = scored[0]
    if sim < 0.8 or score < 0.95:
        return None, f'no confident match (best: {describe(best)}, score {score:.2f})'
    if len(scored) > 1 and scored[1][0][0] > score - 0.05:
        return None, f'ambiguous: {describe(best)} / {describe(scored[1][1])}'
    return best['id'], f'matched {describe(best)}'


# --- episode alignment -------------------------------------------------------------

def _duration_ok(arte_seconds: int | None, runtimes: list[int | None]) -> bool | None:
    """None when it can't be checked."""
    if not arte_seconds or not runtimes or not all(runtimes):
        return None
    ratio = arte_seconds / (60 * sum(runtimes))
    return DURATION_TOLERANCE[0] <= ratio <= DURATION_TOLERANCE[1]


def align(arte: list[Episode], tmdb: list[dict]) -> tuple[dict[str, list[int]] | None, str]:
    """Map each Arte episode of a season to one or several consecutive TMDB episode numbers."""
    tmdb = sorted(tmdb, key=lambda e: e['episode_number'])
    numbers = [e['episode_number'] for e in tmdb]
    runtime = {e['episode_number']: e.get('runtime') for e in tmdb}
    arte = sorted(arte, key=lambda e: e.arte_number)
    c = len(numbers)
    m = max([e.total or 0 for e in arte] + [e.arte_number for e in arte])
    if not c:
        return None, 'TMDB season has no episodes'

    def check(mapping):
        results = [_duration_ok(e.duration, [runtime[n] for n in mapping[e.id]]) for e in arte]
        return False not in results

    if c % m == 0:
        k = c // m
        mapping = {e.id: numbers[(e.arte_number - 1) * k: e.arte_number * k] for e in arte}
        if check(mapping):
            return mapping, '1:1' if k == 1 else f'1 Arte episode = {k} TMDB episodes'
        if k == 1:  # same count but durations disagree: still the most likely mapping
            return mapping, '1:1 (durations differ)'

    # Uneven split: walk the whole season by duration (needs every Arte episode and runtime).
    if len(arte) == m and all(e.duration for e in arte) and all(runtime.values()):
        mapping, i = {}, 0
        for e in arte:
            group, acc = [], 0
            while i < c and (not group or abs(acc + 60 * runtime[numbers[i]] - e.duration)
                             < abs(acc - e.duration)):
                acc += 60 * runtime[numbers[i]]
                group.append(numbers[i])
                i += 1
            if not group:
                break
            mapping[e.id] = group
        if i == c and len(mapping) == len(arte) and check(mapping):
            return mapping, 'aligned by duration'
    return None, f'Arte has {m} episode(s), TMDB {c}: no reliable mapping'


# --- localized titles ---------------------------------------------------------------

def _is_generic(title: str | None) -> bool:
    return not title or not title.strip() or bool(GENERIC_TITLE_RE.match(title))


def merge_titles(titles: list[str]) -> str:
    """Titles of the episodes held in one file: "X (part 1)", "X (part 2)" -> "X"."""
    if len(titles) == 1:
        return titles[0]
    stripped = list(dict.fromkeys(PART_SUFFIX_RE.sub('', t).strip() for t in titles))
    return stripped[0] if len(stripped) == 1 else ' / '.join(stripped)


@dataclass
class Metadata:
    client: TMDBClient
    show: dict
    languages: list[str]

    @property
    def show_id(self) -> int:
        return self.show['id']

    def tmdb_language(self, lang: str) -> str:
        return (self.show.get('original_language') or 'en') if lang == 'original' else lang

    def series_name(self, arte_title: str) -> str:
        translations = (self.show.get('translations') or {}).get('translations') or []
        for lang in self.languages:
            if lang == 'arte':
                return arte_title
            if lang == 'original':
                name = self.show.get('original_name')
            else:
                iso, _, region = lang.partition('-')
                matches = [t for t in translations if t.get('iso_639_1') == iso
                           and (not region or t.get('iso_3166_1') == region)]
                # Untranslated: TMDB (and Plex / Jellyfin) show the original name in that language
                name = next((t['data'].get('name') for t in matches if t['data'].get('name')),
                            self.show.get('original_name'))
            if name:
                return name
        return self.show.get('name') or arte_title

    def episodes(self, season: int, lang: str) -> dict[int, dict]:
        data = self.client.season(self.show_id, season, self.tmdb_language(lang)) or {}
        return {e['episode_number']: e for e in data.get('episodes') or []}

    def localize(self, ep: Episode) -> None:
        """Pick the episode title and synopsis following the language priority."""
        title = overview = None
        for lang in self.languages:
            if lang == 'arte':
                t, o = (None if _is_generic(ep.arte_title) else ep.arte_title), ep.description
            else:
                eps = self.episodes(ep.season, lang)
                found = [eps.get(n) or {} for n in ep.numbers]
                names = [f.get('name') for f in found]
                t = None if any(_is_generic(n) for n in names) else merge_titles(names)
                o = '\n\n'.join(f['overview'] for f in found if f.get('overview')) or None
            title = title or t
            overview = overview or o
            if title and overview:
                break
        ep.title = title or ep.arte_title
        ep.description = overview or ep.description


def apply(series: Series, cfg: MetadataConfig, forced_id: int | None = None,
          log=print) -> None:
    """Rename / renumber the series' episodes from TMDB. Keeps Arte data when unsure."""
    client = TMDBClient(cfg.key)
    first_lang = next((l for l in cfg.languages if l not in ('arte', 'original')), 'en-US')

    show_id = forced_id or load_ids().get(series.id)
    if show_id:
        how = 'forced' if forced_id else 'remembered'
    else:
        show_id, how = find_show(client, series, first_lang)
        if not show_id:
            log(f'   TMDB: {how} — keeping Arte metadata (use --tmdb-id to set it)')
            return
    show = client.show(show_id)
    save_id(series.id, show_id)

    meta = Metadata(client, show, cfg.languages)
    series.tmdb_id = show_id
    series.tvdb_id = (show.get('external_ids') or {}).get('tvdb_id')
    series.year = int(show['first_air_date'][:4]) if show.get('first_air_date') else series.year
    name = meta.series_name(series.title)
    log(f'   TMDB: {how} — "{name}" ({series.year}) https://www.themoviedb.org/tv/{show_id}')

    tmdb_seasons = {s['season_number'] for s in show.get('seasons') or [] if s.get('episode_count')}
    for season in series.seasons:
        for ep in season.episodes:
            ep.series = name
        if season.number not in tmdb_seasons:
            log(f'   TMDB: season {season.number} not found — keeping Arte numbering')
            continue
        tmdb_eps = list(meta.episodes(season.number, first_lang).values())
        mapping, how = align(season.episodes, tmdb_eps)
        if mapping is None:
            log(f'   TMDB: season {season.number}: {how} — keeping Arte numbering')
            continue
        if how != '1:1':
            log(f'   TMDB: season {season.number}: {how}')
        for ep in season.episodes:
            nums = mapping[ep.id]
            ep.number, ep.last_number = nums[0], (nums[-1] if len(nums) > 1 else None)
            meta.localize(ep)
