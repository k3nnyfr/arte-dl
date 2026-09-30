import pytest

from arte_dl.config import Config
from arte_dl.selection import SelectionError, parse_audio, select


def audio(name, lang):
    return {'format_id': f'VF-STF-audio_0-{name}', 'vcodec': 'none', 'acodec': None, 'language': lang}


def video(fid, height, vcodec, tbr):
    return {'format_id': fid, 'vcodec': vcodec, 'acodec': 'none', 'height': height, 'tbr': tbr}


# Formats as seen on https://www.arte.tv/fr/videos/059534-001-A/ after yt-dlp processing
INFO = {
    'formats': [
        audio('allemand', 'de'),
        audio('allemand__audiodescription_', 'de'),
        audio('français__audiodescription_', 'fr'),
        audio('français__confort_audio_', 'fr'),
        audio('suédois__VO_', 'sv'),
        audio('français', 'fr'),
        video('VF-STF-427', 216, 'avc1.42e00d', 427),
        video('VF-STF-2314', 720, 'avc1.4d401f', 2314),
        video('VF-STF-2312', 1080, 'avc1.4d0028', 2312),
        video('VF-STF-3117', 1080, 'hev1.2.4.L123.B0', 3117),
    ],
    'subtitles': {
        'fr-forced': [{'url': 'https://x/st_VF-FRA.m3u8'}],
        'fr': [{'url': 'https://x/st_VO-FRA.m3u8'}],
        'fr-acc': [{'url': 'https://x/st_VF-MAL.m3u8'}],
        'sv': [{'url': 'https://x/st_VO-FRA.m3u8'}],  # same file as "fr"
    },
}


def test_parse_audio_flags():
    vo = parse_audio(audio('suédois__VO_', 'sv'))
    assert (vo.original, vo.kind, vo.title) == (True, 'main', 'Suédois (VO)')
    ad = parse_audio(audio('français__audiodescription_', 'fr'))
    assert (ad.original, ad.kind, ad.title) == (False, 'ad', 'Français (audiodescription)')
    raw = parse_audio(audio('français (confort audio)', 'fr'))  # unprocessed id
    assert (raw.kind, raw.title) == ('comfort', 'Français (confort audio)')


def test_default_selection():
    sel = select(INFO, Config())
    assert sel.video['format_id'] == 'VF-STF-3117'
    assert [a.title for a in sel.audio] == ['Suédois (VO)', 'Français']
    assert sel.format_spec == 'VF-STF-3117+VF-STF-audio_0-suédois__VO_+VF-STF-audio_0-français'
    assert [s.key for s in sel.subtitles] == ['fr', 'fr-forced']
    assert sel.default_subtitle == 0  # VO audio -> full subs
    assert not sel.warnings


def test_codec_and_height_preferences():
    cfg = Config()
    cfg.video.codecs = ['avc', 'hevc']
    assert select(INFO, cfg).video['format_id'] == 'VF-STF-2312'
    cfg.video.max_height = 720
    assert select(INFO, cfg).video['format_id'] == 'VF-STF-2314'


def test_french_first_gets_forced_subs():
    cfg = Config()
    cfg.audio.tracks = ['fr', 'original']
    sel = select(INFO, cfg)
    assert [a.lang for a in sel.audio] == ['fr', 'sv']
    assert sel.subtitles[sel.default_subtitle].key == 'fr-forced'


def test_missing_and_duplicate_tracks():
    cfg = Config()
    cfg.audio.tracks = ['original', 'en', 'fr-ad']
    cfg.subtitles.tracks = ['fr', 'sv', 'de-forced']
    cfg.subtitles.default = 'none'
    sel = select(INFO, cfg)
    assert [a.title for a in sel.audio] == ['Suédois (VO)', 'Français (audiodescription)']
    assert [s.key for s in sel.subtitles] == ['fr']  # "sv" is the same file
    assert sel.default_subtitle is None
    assert sel.warnings == ['audio "en" not available', 'subtitles "de-forced" not available']


def test_fallback_audio_when_nothing_matches():
    cfg = Config()
    cfg.audio.tracks = ['it']
    sel = select(INFO, cfg)
    assert len(sel.audio) == 1 and 'falling back' in sel.warnings[-1]


def test_invalid_audio_spec():
    cfg = Config()
    cfg.audio.tracks = ['fr-xyz']
    with pytest.raises(SelectionError):
        select(INFO, cfg)
