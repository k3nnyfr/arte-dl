"""Minimal WebVTT -> SRT conversion.

Arte serves VTT files with CRLF line endings and STYLE blocks, which older
ffmpeg releases (e.g. 4.4, also used by yt-dlp --convert-subs) silently turn
into empty subtitles. Converting ourselves avoids depending on the ffmpeg version.
"""
from __future__ import annotations

import html
import re

_TIMING_RE = re.compile(
    r'^\s*(?P<start>(?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>(?:\d+:)?\d{2}:\d{2}[.,]\d{3})')
_KEEP_TAGS_RE = re.compile(r'</?(?:i|b|u)>')
_TAG_RE = re.compile(r'<[^>]*>')


def _timestamp(ts: str) -> str:
    ts = ts.replace(',', '.')
    parts = ts.split(':')
    if len(parts) == 2:
        parts.insert(0, '0')
    h, m, s = parts
    sec, ms = s.split('.')
    return f'{int(h):02d}:{int(m):02d}:{int(sec):02d},{ms}'


def _clean(line: str) -> str:
    # Keep <i>, <b>, <u>; drop <c.class>, <v Speaker>, <lang>, inline timestamps...
    kept = []

    def stash(m):
        kept.append(m[0])
        return f'\x00{len(kept) - 1}\x00'

    line = _KEEP_TAGS_RE.sub(stash, line)
    line = html.unescape(_TAG_RE.sub('', line)).replace(' ', ' ')
    return re.sub(r'\x00(\d+)\x00', lambda m: kept[int(m[1])], line)


def vtt_to_srt(vtt: str) -> str:
    text = vtt.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n{2,}', text):
        lines = block.strip('\n').split('\n')
        timing_idx = next((i for i, l in enumerate(lines) if _TIMING_RE.match(l)), None)
        if timing_idx is None:  # header, STYLE, NOTE, REGION blocks
            continue
        m = _TIMING_RE.match(lines[timing_idx])
        payload = [c for c in (_clean(l).strip() for l in lines[timing_idx + 1:]) if c]
        if payload:
            cues.append((_timestamp(m['start']), _timestamp(m['end']), payload))
    return ''.join(f'{n}\n{start} --> {end}\n' + '\n'.join(payload) + '\n\n'
                   for n, (start, end, payload) in enumerate(cues, 1))
