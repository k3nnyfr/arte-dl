"""Download one episode with yt-dlp, then remux it into the final MKV with ffmpeg.

yt-dlp downloads and merges the selected video + audio formats and the VTT
subtitles into a temporary directory; a final ffmpeg pass adds the subtitles,
track languages / titles / default & forced flags and episode tags.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import ISO639Utils

from .arte_api import Episode, Movie, Series
from .config import Config
from .selection import Selection, select
from .subtitles import vtt_to_srt

_FORBIDDEN = re.compile(r'[\x00-\x1f"*<>?|\\]')


class DownloadError(Exception):
    pass


def sanitize(name: str) -> str:
    name = name.replace('/', '-').replace(':', ' -')
    name = _FORBIDDEN.sub('', name)
    return re.sub(r'\s+', ' ', name).strip().rstrip('.')


class EpisodeNumbers:
    """{episode:02d} -> "01", or "01-E02" for a file holding several episodes."""

    def __init__(self, numbers: list[int]):
        self.numbers = numbers

    def __format__(self, spec: str) -> str:
        first, last = self.numbers[0], self.numbers[-1]
        return format(first, spec) + (f'-E{format(last, spec)}' if last != first else '')


# Groups left empty by a missing field: "Series ()", "[tmdbid-]", "{tmdb-}"
_EMPTY_GROUP_RE = re.compile(r'\s*[(\[{]\s*(?:[a-z]+-)?\s*[)\]}]')


def _render(templates: list[str], fields: dict) -> list[str]:
    try:
        return [_EMPTY_GROUP_RE.sub('', tpl.format(**fields)).strip() for tpl in templates]
    except (KeyError, ValueError, AttributeError) as e:
        raise DownloadError(f'Invalid output template: {e!r}') from None


def destination(item: Series | Movie, ep: Episode, cfg: Config) -> Path:
    if isinstance(item, Movie):
        return movie_destination(item, ep, cfg)
    series = item
    fields = {'series': sanitize(ep.series), 'year': series.year or '',
              'tmdb_id': series.tmdb_id or '', 'tvdb_id': series.tvdb_id or '',
              'season': ep.season, 'episode': EpisodeNumbers(ep.numbers),
              'title': sanitize(ep.title), 'id': ep.id}
    series_dir, season_dir, filename = _render(
        [cfg.output.series_dir, cfg.output.season_dir, cfg.output.filename], fields)
    return cfg.output_dir / series_dir / season_dir / f'{filename}.mkv'


def movie_destination(movie: Movie, part: Episode, cfg: Config) -> Path:
    o = cfg.output
    fields = {'title': sanitize(movie.title), 'year': movie.year or '',
              'original_title': sanitize(movie.original_title or movie.title),
              'tmdb_id': movie.tmdb_id or '', 'imdb_id': movie.imdb_id or '', 'id': movie.id,
              'part': part.number}
    filename_tpl = o.movie_filename + (o.part_suffix if movie.multipart else '')
    movie_dir, filename = _render([o.movie_dir, filename_tpl], fields)
    return cfg.movie_root(movie.kind) / movie_dir / f'{filename}.mkv'


def _ydl_params(**extra) -> dict:
    return {
        'quiet': True,
        'no_warnings': False,
        'noprogress': False,
        'retries': 10,
        'fragment_retries': 10,
        'extractor_retries': 5,
        **extra,
    }


def extract(ep: Episode) -> dict:
    """Extract and process formats (yt-dlp rewrites format ids while processing:
    "suédois (VO)" -> "suédois__VO_"), without downloading."""
    with YoutubeDL(_ydl_params()) as ydl:
        info = ydl.extract_info(ep.url, download=False)
    if not info or not info.get('formats'):
        raise DownloadError('No formats found (not available in this country / expired?)')
    # Same cleanup as --load-info-json, so the info dict can be processed again
    return YoutubeDL.sanitize_info(info, remove_private_keys=True)


def plan(ep: Episode, cfg: Config) -> tuple[dict, Selection]:
    info = extract(ep)
    return info, select(info, cfg)


def _lang3(lang: str | None) -> str:
    return (lang and ISO639Utils.short2long(lang)) or 'und'


def download(item: Series | Movie, ep: Episode, info: dict, sel: Selection, dest: Path, cfg: Config) -> None:
    work = cfg.output_dir / '.arte-dl-tmp' / ep.id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    params = _ydl_params(
        format=sel.format_spec,
        allow_multiple_audio_streams=len(sel.audio) > 1,
        merge_output_format='mkv',
        outtmpl={'default': str(work / 'media.%(ext)s'),
                 'subtitle': str(work / 'sub.%(ext)s')},
        writesubtitles=bool(sel.subtitles),
        subtitleslangs=[re.escape(s.key) for s in sel.subtitles],
        subtitlesformat='vtt',
    )
    with YoutubeDL(params) as ydl:
        ydl.process_ie_result(info, download=True)

    media = next((p for p in work.glob('media.*')
                  if p.suffix not in ('.part', '.ytdl') and '.f' not in p.stem), None)
    if media is None:
        raise DownloadError(f'yt-dlp produced no media file in {work}')
    sub_files = []
    for s in sel.subtitles:
        vtt = work / f'sub.{s.key}.vtt'
        if not vtt.exists():
            raise DownloadError(f'subtitle file missing: {vtt.name}')
        srt_text = vtt_to_srt(vtt.read_text(encoding='utf-8-sig', errors='replace'))
        if not srt_text:
            raise DownloadError(f'subtitle file {vtt.name} has no cues')
        srt = vtt.with_suffix('.srt')
        srt.write_text(srt_text, encoding='utf-8')
        sub_files.append(srt)

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + '.part.mkv')
    subprocess.run(_mux_command(media, sub_files, item, ep, sel, part), check=True)
    part.replace(dest)
    shutil.rmtree(work, ignore_errors=True)
    try:
        work.parent.rmdir()  # .arte-dl-tmp, if no other download is in progress
    except OSError:
        pass


def _tags(item: Series | Movie, ep: Episode) -> dict[str, str]:
    if isinstance(item, Movie):
        title = item.title
        if item.multipart:
            title += f' ({ep.number}/{item.total_parts})'
            if ep.title != item.title:
                title += f' - {ep.title}'
        return {
            'title': title,
            'part_number': str(ep.number) if item.multipart else '',
            'total_parts': str(item.total_parts) if item.multipart else '',
            'description': (ep.description if item.multipart else None) or item.description or '',
            'comment': ep.url,
            'tmdb': f'{item.tmdb_type}/{item.tmdb_id}' if item.tmdb_id else '',
            'imdb': item.imdb_id or '',
            'date': str(item.year or ''),
        }
    return {
        'title': ep.title,
        'show': ep.series,
        'season_number': str(ep.season),
        'episode_sort': str(ep.number),
        'episode_id': ep.label,
        'description': ep.description or '',
        'comment': ep.url,
        'tmdb': f'tv/{item.tmdb_id}' if item.tmdb_id else '',
        'date': str(item.year or ''),
    }


def _mux_command(media: Path, subs: list[Path], item: Series | Movie, ep: Episode, sel: Selection,
                 out: Path) -> list[str]:
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', str(media)]
    for path in subs:
        cmd += ['-i', str(path)]
    cmd += ['-map', '0:v:0', '-map', '0:a?']
    for i in range(len(subs)):
        cmd += ['-map', f'{i + 1}:0']
    cmd += ['-c', 'copy', '-c:s', 'srt', '-map_metadata', '-1']

    for k, v in _tags(item, ep).items():
        if v:
            cmd += ['-metadata', f'{k}={v}']

    for i, a in enumerate(sel.audio):
        cmd += [f'-metadata:s:a:{i}', f'language={_lang3(a.lang)}',
                f'-metadata:s:a:{i}', f'title={a.title}',
                f'-disposition:a:{i}', 'default' if i == 0 else '0']
    for i, s in enumerate(sel.subtitles):
        flags = [f for f, on in (('default', i == sel.default_subtitle),
                                 ('forced', s.kind == 'forced'),
                                 ('hearing_impaired', s.kind == 'sdh')) if on]
        cmd += [f'-metadata:s:s:{i}', f'language={_lang3(s.lang)}',
                f'-metadata:s:s:{i}', f'title={s.title}',
                f'-disposition:s:{i}', '+'.join(flags) or '0']
    return cmd + [str(out)]
