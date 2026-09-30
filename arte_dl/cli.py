"""Command-line entry point."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .arte_api import ArteClient, ArteError, Movie, Series, parse_url
from .config import ConfigError, default_config_path, load_config
from .download import DownloadError, destination, download, plan
from .metadata import TMDBError
from .metadata import apply as apply_metadata
from .metadata import apply_movie, parse_ref
from .selection import SelectionError


def parse_ranges(spec: str) -> set[int]:
    """"1,3-5" -> {1, 3, 4, 5}"""
    numbers = set()
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        lo, sep, hi = part.partition('-')
        try:
            numbers.update(range(int(lo), int(hi) + 1) if sep else {int(lo)})
        except ValueError:
            raise argparse.ArgumentTypeError(f'invalid range "{part}"') from None
    return numbers


TMDB_REF_RE = re.compile(r'^(?:(?:movie|tv)/)?\d+$')


def tmdb_ref(value: str) -> str:
    if not TMDB_REF_RE.match(value):
        raise argparse.ArgumentTypeError(f'expected 123, movie/123 or tv/123, not "{value}"')
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='arte-dl',
        description='Download arte.tv series (every season) into Series/Season XX/*.mkv, '
                    'films and documentaries into Title (year)/*.mkv')
    p.add_argument('urls', nargs='+', metavar='URL',
                   help='arte.tv series (RC-xxxxxx), season, episode, film, documentary or collection URL')
    p.add_argument('-c', '--config', type=Path,
                   help=f'TOML config file (default: {default_config_path()})')
    p.add_argument('-o', '--output', help='output root directory (overrides [output] directory)')
    p.add_argument('-s', '--seasons', type=parse_ranges, help='only these seasons, e.g. "1,3-5"')
    p.add_argument('-e', '--episodes', type=parse_ranges,
                   help='only these episode numbers (documentaries: part numbers)')
    p.add_argument('-l', '--list', action='store_true',
                   help='list seasons / episodes and destination paths, then exit')
    p.add_argument('-n', '--dry-run', action='store_true',
                   help='also show the selected tracks for each episode, without downloading')
    p.add_argument('-f', '--force', action='store_true', help='re-download existing files')
    p.add_argument('--as-series', action='store_true',
                   help='file a documentary in several parts as a mini-series (Season 01/…E01) '
                        'instead of a movie in parts')
    p.add_argument('--tmdb-id', type=tmdb_ref, metavar='ID',
                   help='TMDB id to use instead of searching, with a single URL (remembered for next runs): '
                        'show id for a series, movie id for a film, "tv/ID" for a documentary filed as a TV show')
    p.add_argument('--no-metadata', action='store_true', help="don't query TMDB, use Arte data only")
    p.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    return p


def _filter(item: Series | Movie, args) -> None:
    if isinstance(item, Movie):
        item.parts = [p for p in item.parts if not args.episodes or p.number in args.episodes]
        return
    for season in item.seasons:
        season.episodes = [e for e in season.episodes
                           if not args.episodes or args.episodes & set(e.numbers)]
    item.seasons = [s for s in item.seasons
                    if s.episodes and (not args.seasons or s.number in args.seasons)]


def _apply_metadata(item: Series | Movie, cfg, args) -> None:
    meta = cfg.metadata
    if meta.provider != 'tmdb' or args.no_metadata:
        return
    if not meta.key:
        print('   TMDB: no API key ([metadata] api_key or TMDB_API_KEY) — using Arte metadata')
        return
    try:
        if isinstance(item, Movie):
            apply_movie(item, meta, forced=args.tmdb_id)
        else:
            kind, forced = parse_ref(args.tmdb_id, 'tv') or ('tv', None)
            if kind != 'tv':
                print(f'   TMDB: --tmdb-id {args.tmdb_id} is not a TV show — ignored', file=sys.stderr)
                forced = None
            apply_metadata(item, meta, forced_id=forced)
    except TMDBError as e:
        print(f'   TMDB: {e} — keeping Arte metadata', file=sys.stderr)


def _fetch(item: Series | Movie, ep, label: str, cfg, args, note: str = '') -> bool:
    """Show one episode / part and download it. False on failure."""
    dest = destination(item, ep, cfg)
    exists = dest.exists()
    print(f'   {label} [{ep.id}] {ep.title}{note}')
    print(f'      -> {dest}{"  (exists)" if exists else ""}')
    if args.list or (exists and not args.force):
        return True
    try:
        info, sel = plan(ep, cfg)
        h = sel.video
        print(f'      video : {h["format_id"]} {h.get("height")}p {h.get("vcodec")}')
        print(f'      audio : {", ".join(a.title for a in sel.audio) or "(muxed)"}')
        print('      subs  : ' + (', '.join(
            s.title + (' *' if i == sel.default_subtitle else '')
            for i, s in enumerate(sel.subtitles)) or '-'))
        for w in sel.warnings:
            print(f'      warning: {w}')
        if not args.dry_run:
            download(item, ep, info, sel, dest, cfg)
            print('      done')
        return True
    except KeyboardInterrupt:
        raise
    except (DownloadError, SelectionError, subprocess.CalledProcessError) as e:
        print(f'      FAILED: {e}', file=sys.stderr)
    except Exception as e:  # keep going with the next episode
        print(f'      FAILED: {type(e).__name__}: {e}', file=sys.stderr)
    return False


def process(url: str, cfg, args) -> tuple[int, int]:
    """Returns (ok, failed) episode counts."""
    lang, _ = parse_url(url)
    ok = failed = 0
    for item in ArteClient(lang).resolve(url, as_series=args.as_series):
        if isinstance(item, Movie):
            kind = 'Documentary' if item.kind == 'documentary' else 'Film'
            parts = f', {len(item.parts)}/{item.total_parts} part(s)' if item.multipart else ''
            print(f'== {kind}: {item.title} ({item.year or "?"}) [{item.id}]{parts}')
        else:
            print(f'== {item.title} ({item.id}) — {len(item.seasons)} season(s), '
                  f'{len(item.episodes)} episode(s)')
            if item.unavailable:
                print(f'   not available online: {", ".join(item.unavailable)}')
        _apply_metadata(item, cfg, args)
        _filter(item, args)

        if isinstance(item, Movie):
            for part in item.parts:
                label = f'part {part.number}/{item.total_parts}' if item.multipart else 'film'
                if _fetch(item, part, label, cfg, args):
                    ok += 1
                else:
                    failed += 1
            continue
        for season in item.seasons:
            print(f'-- Season {season.number}: {season.title}')
            for ep in season.episodes:
                arte_label = f'S{ep.arte_season:02d}E{ep.arte_number:02d}'
                renumbered = f'  (Arte {arte_label})' if arte_label != ep.label else ''
                if _fetch(item, ep, ep.label, cfg, args, renumbered):
                    ok += 1
                else:
                    failed += 1
    return ok, failed


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tmdb_id and len(args.urls) > 1:
        parser.error('--tmdb-id only makes sense with a single URL')
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with yt-dlp's stderr
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f'config error: {e}', file=sys.stderr)
        return 2
    if args.output:  # a single root for series, films and documentaries
        cfg.output.directory = args.output
        cfg.output.movies_directory = cfg.output.documentaries_directory = ''
    if not (args.list or args.dry_run) and not shutil.which('ffmpeg'):
        print('ffmpeg not found in PATH', file=sys.stderr)
        return 2

    ok = failed = 0
    for url in args.urls:
        try:
            o, f = process(url, cfg, args)
        except ArteError as e:
            print(f'error: {e}', file=sys.stderr)
            failed += 1
            continue
        except KeyboardInterrupt:
            print('\ninterrupted', file=sys.stderr)
            return 130
        ok, failed = ok + o, failed + f
    if failed:
        print(f'\n{failed} failure(s), {ok} ok', file=sys.stderr)
    return 1 if failed else 0
