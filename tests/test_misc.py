from pathlib import Path

import pytest

from arte_dl.arte_api import ArteClient, parse_url
from arte_dl.cli import parse_ranges
from arte_dl.config import Config, ConfigError, load_config
from arte_dl.download import destination, sanitize
from arte_dl.subtitles import vtt_to_srt

VTT = (
    'WEBVTT\r\n\r\nSTYLE\r\n::cue(.red) {\r\n color: red;\r\n}\r\n\r\n'
    'NOTE a comment\r\n\r\n'
    'cue-1\r\n00:00:15.320 --> 00:00:16.880 line:91% align:center\r\n'
    '<c.white.bg_black>Je la tiens</c> &amp; <i>vite</i> !\r\n\r\n'
    '01:02.000 --> 01:03.500\r\n<v Mia>Bonjour,</v>\r\nMaria.\r\n\r\n'
    '00:01:04.000 --> 00:01:05.000\r\n<c.red></c>\r\n'
)


def test_vtt_to_srt():
    assert vtt_to_srt(VTT) == (
        '1\n00:00:15,320 --> 00:00:16,880\nJe la tiens & <i>vite</i> !\n\n'
        '2\n00:01:02,000 --> 00:01:03,500\nBonjour,\nMaria.\n\n'
    )


def test_parse_url():
    assert parse_url('https://www.arte.tv/fr/videos/RC-027900/the-hack-sur-ecoute/') == ('fr', 'RC-027900')
    assert parse_url('https://www.arte.tv/de/videos/059534-001-A/x/') == ('de', '059534-001-A')


def test_parse_ranges():
    assert parse_ranges('1,3-5, 8') == {1, 3, 4, 5, 8}


def test_episode_numbering_and_titles():
    items = [
        {'providerId': '044639-009-A', 'title': 'Twin Peaks - Saison 2 (1/22)', 'subtitle': 'Le géant'},
        {'providerId': 'RC-000000', 'title': 'bonus collection'},
        {'providerId': '125066-002-A', 'title': 'The Hack (2/7)', 'subtitle': None},
        {'providerId': '125066-009-A', 'title': 'Making-of'},
    ]
    eps = ArteClient('fr')._episodes(items, 'Series', 2, None)
    assert [(e.number, e.title) for e in eps] == [(1, 'Le géant'), (2, 'Épisode 2'), (3, 'Making-of')]
    assert eps[0].url == 'https://www.arte.tv/fr/videos/044639-009-A/'


def test_destination():
    from arte_dl.arte_api import Episode, Series
    cfg = Config()
    cfg.output.directory = '/media/series'
    series = Series('RC-027900', 'The Hack : sur écoute', 'fr')
    ep = Episode('125066-001-A', 'u', 'The Hack : sur écoute', 1, 1, 'Qui ? Quoi / où')
    # no year known: the empty "()" disappears
    assert destination(series, ep, cfg) == Path(
        '/media/series/The Hack - sur écoute/Season 01/The Hack - sur écoute - S01E01 - Qui Quoi - où.mkv')

    series.year, series.tmdb_id = 2025, 12345
    ep.series, ep.season, ep.number, ep.last_number = 'Meurtres à Sandhamn', 6, 1, 2
    cfg.output.series_dir = '{series} ({year}) [tmdbid-{tmdb_id}] {{tvdb-{tvdb_id}}}'
    assert destination(series, ep, cfg) == Path(
        '/media/series/Meurtres à Sandhamn (2025) [tmdbid-12345]/Season 06/'
        'Meurtres à Sandhamn - S06E01-E02 - Qui Quoi - où.mkv')
    assert sanitize('a: b.') == 'a - b'


def test_config(tmp_path):
    path = tmp_path / 'c.toml'
    path.write_text('[video]\nmax_height = 720\n[audio]\ntracks = ["fr"]\n')
    cfg = load_config(path)
    assert (cfg.video.max_height, cfg.audio.tracks, cfg.video.codecs) == (720, ['fr'], ['hevc', 'avc'])
    path.write_text('[video]\nmax_heigth = 720\n')
    with pytest.raises(ConfigError, match='max_heigth'):
        load_config(path)
    path.write_text('[audio]\ntracks = "fr"\n')
    with pytest.raises(ConfigError, match='list'):
        load_config(path)
