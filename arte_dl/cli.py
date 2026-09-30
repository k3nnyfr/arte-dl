"""Command-line entry point."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .arte_api import ArteClient, ArteError, Series, parse_url
from .config import ConfigError, default_config_path, load_config
from .download import DownloadError, destination, download, plan
from .metadata import TMDBError
from .metadata import apply as apply_metadata
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='arte-dl',
        description='Download arte.tv series (every season) into Series/Season XX/*.mkv')
    p.add_argument('urls', nargs='+', metavar='URL',
                   help='arte.tv series (RC-xxxxxx), season or episode URL')
    p.add_argument('-c', '--config', type=Path,
                   help=f'TOML config file (default: {default_config_path()})')
    p.add_argument('-o', '--output', help='output root directory (overrides [output] directory)')
    p.add_argument('-s', '--seasons', type=parse_ranges, help='only these seasons, e.g. "1,3-5"')
    p.add_argument('-e', '--episodes', type=parse_ranges, help='only these episode numbers')
    p.add_argument('-l', '--list', action='store_true',
                   help='list seasons / episodes and destination paths, then exit')
    p.add_argument('-n', '--dry-run', action='store_true',
                   help='also show the selected tracks for each episode, without downloading')
    p.add_argument('-f', '--force', action='store_true', help='re-download existing files')
    p.add_argument('--tmdb-id', type=int, metavar='ID',
                   help='TMDB show id to use instead of searching, with a single URL (remembered for next runs)')
    p.add_argument('--no-metadata', action='store_true', help="don't query TMDB, use Arte data only")
    p.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    return p


def _filter(series: Series, args) -> None:
    for season in series.seasons:
        season.episodes = [e for e in season.episodes
                           if not args.episodes or args.episodes & set(e.numbers)]
    series.seasons = [s for s in series.seasons
                      if s.episodes and (not args.seasons or s.number in args.seasons)]


def process(url: str, cfg, args) -> tuple[int, int]:
    """Returns (ok, failed) episode counts."""
    lang, _ = parse_url(url)
    series = ArteClient(lang).resolve(url)
    total = len(series.episodes)
    print(f'== {series.title} ({series.id}) — {len(series.seasons)} season(s), {total} episode(s)')
    if series.unavailable:
        print(f'   not available online: {", ".join(series.unavailable)}')

    meta = cfg.metadata
    if meta.provider == 'tmdb' and not args.no_metadata:
        if meta.key:
            try:
                apply_metadata(series, meta, forced_id=args.tmdb_id)
            except TMDBError as e:
                print(f'   TMDB: {e} — keeping Arte metadata', file=sys.stderr)
        else:
            print('   TMDB: no API key ([metadata] api_key or TMDB_API_KEY) — using Arte metadata')
    _filter(series, args)

    ok = failed = 0
    for season in series.seasons:
        print(f'-- Season {season.number}: {season.title}')
        for ep in season.episodes:
            dest = destination(series, ep, cfg)
            exists = dest.exists()
            arte_label = f'S{ep.arte_season:02d}E{ep.arte_number:02d}'
            renumbered = f'  (Arte {arte_label})' if arte_label != ep.label else ''
            print(f'   {ep.label} [{ep.id}] {ep.title}{renumbered}')
            print(f'      -> {dest}{"  (exists)" if exists else ""}')
            if args.list or (exists and not args.force):
                ok += 1
                continue
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
                    download(series, ep, info, sel, dest, cfg)
                    print('      done')
                ok += 1
            except KeyboardInterrupt:
                raise
            except (DownloadError, SelectionError, subprocess.CalledProcessError) as e:
                failed += 1
                print(f'      FAILED: {e}', file=sys.stderr)
            except Exception as e:  # keep going with the next episode
                failed += 1
                print(f'      FAILED: {type(e).__name__}: {e}', file=sys.stderr)
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
    if args.output:
        cfg.output.directory = args.output
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
