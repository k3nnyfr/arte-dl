"""Pick video / audio / subtitle tracks from a yt-dlp info dict according to the config.

Arte HLS masters expose one video ladder plus several audio renditions whose
format ids look like "VF-STF-audio_0-suédois__VO_", "VF-STF-audio_0-français",
"VF-STF-audio_0-français__audiodescription_", "...__confort_audio_".
Subtitles are keyed "fr" (full), "fr-forced", "fr-acc" (SDH), "de-forced", ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config

AUDIO_ID_RE = re.compile(r'-audio_\d+-(?P<name>.+)$')

CODEC_ALIASES = {
    'avc': ('avc1', 'avc3', 'h264'),
    'h264': ('avc1', 'avc3', 'h264'),
    'hevc': ('hev1', 'hvc1', 'h265', 'hevc'),
    'h265': ('hev1', 'hvc1', 'h265', 'hevc'),
    'av1': ('av01',),
    'vp9': ('vp9', 'vp09'),
}

LANGUAGE_NAMES = {
    'fr': 'Français', 'de': 'Deutsch', 'en': 'English', 'es': 'Español',
    'it': 'Italiano', 'pl': 'Polski', 'sv': 'Svenska', 'da': 'Dansk',
    'no': 'Norsk', 'fi': 'Suomi', 'nl': 'Nederlands', 'pt': 'Português',
}
SUB_KINDS = {'': 'full', 'forced': 'forced', 'acc': 'sdh'}
SUB_KIND_LABELS = {'full': '', 'forced': ' (forcés)', 'sdh': ' (SDH)'}


class SelectionError(Exception):
    pass


@dataclass
class AudioTrack:
    format_id: str
    lang: str | None
    title: str
    original: bool = False
    kind: str = 'main'  # main | ad | comfort


@dataclass
class SubtitleTrack:
    key: str
    lang: str
    kind: str  # full | forced | sdh
    title: str


@dataclass
class Selection:
    video: dict
    audio: list[AudioTrack] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    default_subtitle: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def format_spec(self) -> str:
        return '+'.join([self.video['format_id'], *(a.format_id for a in self.audio)])


def normalize_lang(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.lower().split('-')[0]
    if len(lang) == 3:
        from yt_dlp.utils import ISO639Utils
        return ISO639Utils.long2short(lang) or lang
    return lang


def parse_audio(fmt: dict) -> AudioTrack:
    m = AUDIO_ID_RE.search(fmt['format_id'])
    name = m['name'] if m else (fmt.get('format_note') or fmt.get('language') or 'audio')
    # "suédois__VO_" (processed) or "suédois (VO)" (raw)
    parts = [p.strip('_ ') for p in re.sub(r'[\s()]', '_', name).split('__')]
    base = parts[0].replace('_', ' ').strip()
    flags = ' '.join(parts[1:]).lower().replace('_', ' ')
    original = bool(re.search(r'\bvo\b', flags))
    kind = 'ad' if 'audiodescription' in flags else 'comfort' if 'confort' in flags else 'main'

    lang = normalize_lang(fmt.get('language'))
    label = base[:1].upper() + base[1:] if base else LANGUAGE_NAMES.get(lang or '', lang or 'Audio')
    suffix = [s for s, on in (('VO', original), ('audiodescription', kind == 'ad'),
                              ('confort audio', kind == 'comfort')) if on]
    title = f'{label} ({", ".join(suffix)})' if suffix else label
    return AudioTrack(format_id=fmt['format_id'], lang=lang, title=title,
                      original=original, kind=kind)


def audio_matches(spec: str, track: AudioTrack) -> bool:
    spec = spec.lower()
    if spec in ('original', 'vo'):
        return track.original and track.kind == 'main'
    lang, _, kind = spec.partition('-')
    kind = {'': 'main', 'ad': 'ad', 'comfort': 'comfort'}.get(kind)
    if kind is None:
        raise SelectionError(f'Invalid audio track "{spec}" (use original, fr, fr-ad, fr-comfort...)')
    return track.lang == lang and track.kind == kind


def _codec_rank(vcodec: str, preferences: list[str]) -> int:
    vcodec = (vcodec or '').lower()
    for i, pref in enumerate(preferences):
        if vcodec.startswith(CODEC_ALIASES.get(pref.lower(), (pref.lower(),))):
            return len(preferences) - i
    return 0


def is_video(f: dict) -> bool:
    return f.get('vcodec') not in (None, 'none') and bool(f.get('height'))


def pick_video(formats: list[dict], cfg: Config) -> dict:
    videos = [f for f in formats if is_video(f)]
    if not videos:
        raise SelectionError('No video format found')
    capped = [f for f in videos if not cfg.video.max_height or f['height'] <= cfg.video.max_height]
    if not capped:  # nothing that small: the lowest height available
        lowest = min(f['height'] for f in videos)
        capped = [f for f in videos if f['height'] == lowest]
    return max(capped, key=lambda f: (
        f['height'], _codec_rank(f.get('vcodec'), cfg.video.codecs), f.get('tbr') or 0))


def select(info: dict, cfg: Config) -> Selection:
    formats = info.get('formats') or []
    video = pick_video(formats, cfg)
    sel = Selection(video=video)

    # Audio renditions (Arte reports acodec=None for them, so test vcodec only). Keep
    # the first occurrence of each format id: several "versions" can repeat them.
    audio_tracks, seen = [], set()
    for f in formats:
        if f.get('vcodec') == 'none' and f['format_id'] not in seen:
            seen.add(f['format_id'])
            audio_tracks.append(parse_audio(f))

    chosen = []
    for spec in cfg.audio.tracks:
        track = next((t for t in audio_tracks if audio_matches(spec, t)), None)
        if track is None:
            sel.warnings.append(f'audio "{spec}" not available')
        elif all(t.format_id != track.format_id for t in chosen):
            chosen.append(track)
    if not chosen and audio_tracks:
        best = max((f for f in formats if f['format_id'] in seen),
                   key=lambda f: (f.get('language_preference') or 0, f.get('abr') or 0))
        chosen.append(parse_audio(best))
        sel.warnings.append(f'no configured audio track found, falling back to "{chosen[0].title}"')
    elif not audio_tracks and video.get('acodec') in (None, 'none'):
        sel.warnings.append('no separate audio track found')
    sel.audio = chosen

    # Subtitles: the same file can be listed under several keys (e.g. "sv" and "fr").
    subs = info.get('subtitles') or {}
    seen_urls = set()
    for key in cfg.subtitles.tracks:
        entries = subs.get(key)
        if not entries:
            sel.warnings.append(f'subtitles "{key}" not available')
            continue
        url = entries[0].get('url')
        if url in seen_urls:
            continue
        seen_urls.add(url)
        lang, _, suffix = key.partition('-')
        kind = SUB_KINDS.get(suffix, 'full')
        name = LANGUAGE_NAMES.get(lang, lang)
        sel.subtitles.append(SubtitleTrack(key=key, lang=lang, kind=kind,
                                           title=name + SUB_KIND_LABELS[kind]))
    sel.default_subtitle = _default_subtitle(sel, cfg.subtitles.default)
    return sel


def _default_subtitle(sel: Selection, mode: str) -> int | None:
    subs = sel.subtitles
    if not subs or mode == 'none':
        return None
    if mode != 'auto':
        return next((i for i, s in enumerate(subs) if s.key == mode), None)
    audio_lang = sel.audio[0].lang if sel.audio else None
    sub_lang = subs[0].lang

    def find(kind):
        return next((i for i, s in enumerate(subs) if s.lang == sub_lang and s.kind == kind), None)

    if audio_lang == sub_lang:
        return find('forced')
    full = find('full')
    return full if full is not None else find('sdh')
