import json

import pytest

from arte_dl import metadata
from arte_dl.arte_api import Episode, Season, Series
from arte_dl.config import MetadataConfig
from arte_dl.metadata import Metadata, align, find_show, merge_titles


def arte_ep(season, n, total, minutes, title, pid=None):
    return Episode(pid or f'{season:03d}{n:03d}-000-A', 'u', 'Meurtres à Sandhamn', season, n, title,
                   description=f'arte {season}/{n}', duration=minutes * 60, total=total)


def tmdb_ep(n, name, runtime=45, overview=''):
    return {'episode_number': n, 'name': name, 'runtime': runtime, 'overview': overview}


# As seen on TMDB (show 55270) and Arte (RC-022391)
S01_TMDB = [tmdb_ep(i, n) for i, n in enumerate(
    ['La Reine de la Baltique (1)', 'La Reine de la Baltique (2)', 'La Reine de la Baltique (3)'], 1)]
S06_TMDB = [tmdb_ep(i, f'{name} part {p}') for i, (name, p) in enumerate(
    [(t, p) for t in ['Le prix à payer', 'Au nom de la vérité', 'À la vie, à la mort', 'Un goût amer']
     for p in (1, 2)], 1)]


def s06_arte():
    return [arte_ep(6, i, 4, 88, f'Enquête {5 + i}') for i in range(1, 5)]


def test_align_one_to_one():
    eps = [arte_ep(1, i, 3, 43, 'Enquête 1 : La reine de la Baltique') for i in range(1, 4)]
    mapping, how = align(eps, S01_TMDB)
    assert how == '1:1' and [mapping[e.id] for e in eps] == [[1], [2], [3]]


def test_align_arte_merges_two_parts():
    eps = s06_arte()
    mapping, how = align(eps, S06_TMDB)
    assert how == '1 Arte episode = 2 TMDB episodes'
    assert [mapping[e.id] for e in eps] == [[1, 2], [3, 4], [5, 6], [7, 8]]


def test_align_partial_season_uses_arte_total():
    eps = s06_arte()[2:3]  # only Arte episode 3/4 still online
    mapping, _ = align(eps, S06_TMDB)
    assert mapping[eps[0].id] == [5, 6]


def test_align_uneven_by_duration():
    eps = [arte_ep(2, 1, 2, 88, 'a'), arte_ep(2, 2, 2, 45, 'b')]
    tmdb = [tmdb_ep(1, 'x'), tmdb_ep(2, 'y'), tmdb_ep(3, 'z')]
    mapping, how = align(eps, tmdb)
    assert how == 'aligned by duration' and [mapping[e.id] for e in eps] == [[1, 2], [3]]


def test_align_refuses_inconsistent():
    eps = [arte_ep(2, i, 3, 45, 'a') for i in range(1, 4)]
    mapping, how = align(eps, [tmdb_ep(i, 'x') for i in range(1, 6)])
    assert mapping is None and 'no reliable mapping' in how


def test_merge_titles():
    assert merge_titles(['Le prix à payer part 1', 'Le prix à payer part 2']) == 'Le prix à payer'
    assert merge_titles(['Au nom de la vérité (part1)', 'Au nom de la vérité (part 2)']) == 'Au nom de la vérité'
    assert merge_titles(['A', 'B']) == 'A / B'
    assert merge_titles(['Mensonges bleus (1)', 'Mensonges bleus (2)']) == 'Mensonges bleus'
    assert merge_titles(['Madeleine', 'Madeleine (2)']) == 'Madeleine'
    assert merge_titles(['Chapitre 12', 'Chapitre 13']) == 'Chapitre 12 / Chapitre 13'
    assert merge_titles(['Solo (part 1)']) == 'Solo (part 1)'  # a single episode keeps its title


class FakeClient:
    def __init__(self, search=(), shows=None, seasons=None):
        self._search, self._shows, self._seasons = search, shows or {}, seasons or {}
        self.calls = []

    def search(self, query, language):
        self.calls.append(('search', query))
        return tuple(r for r in self._search if query.lower() in json.dumps(r, ensure_ascii=False).lower())

    def show(self, show_id):
        return self._shows[show_id]

    def season(self, show_id, number, language):
        self.calls.append(('season', number, language))
        eps = self._seasons.get((number, language))
        return {'episodes': eps} if eps is not None else None


def test_find_show_prefers_year_and_language():
    series = Series('RC-027900', 'The Hack : sur écoute', 'fr', original_title='The Hack',
                    original_language='en', year=2025)
    client = FakeClient(search=[
        {'id': 1, 'name': 'The Hack', 'original_name': 'The Hack', 'first_air_date': '2012-01-01',
         'original_language': 'en'},
        {'id': 2, 'name': 'The Hack', 'original_name': 'The Hack', 'first_air_date': '2025-09-24',
         'original_language': 'en'},
        {'id': 3, 'name': 'Hacks', 'original_name': 'Hacks', 'first_air_date': '2021-05-13',
         'original_language': 'en'},
    ])
    assert find_show(client, series, 'fr-FR')[0] == 2


def test_find_show_rejects_weak_or_ambiguous():
    series = Series('RC-1', 'Inconnue', 'fr', year=2020)
    client = FakeClient(search=[{'id': 9, 'name': 'Inconnue au bataillon', 'first_air_date': '2020-01-01'}])
    assert find_show(client, series, 'fr-FR')[0] is None
    series = Series('RC-2', 'Twin', 'fr')  # no year: two identical names
    client = FakeClient(search=[{'id': 1, 'name': 'Twin'}, {'id': 2, 'name': 'Twin'}])
    show_id, why = find_show(client, series, 'fr-FR')
    assert show_id is None and why.startswith('ambiguous')


SHOW = {
    'id': 55270, 'name': 'Meurtres à Sandhamn', 'original_name': 'Morden i Sandhamn',
    'original_language': 'sv', 'first_air_date': '2010-01-09', 'external_ids': {'tvdb_id': 158851},
    'seasons': [{'season_number': 0, 'episode_count': 2}, {'season_number': 1, 'episode_count': 3},
                {'season_number': 6, 'episode_count': 8}],
    'translations': {'translations': [
        {'iso_639_1': 'fr', 'iso_3166_1': 'FR', 'data': {'name': 'Meurtres à Sandhamn'}},
        {'iso_639_1': 'en', 'iso_3166_1': 'US', 'data': {'name': 'The Sandhamn Murders'}},
        {'iso_639_1': 'sv', 'iso_3166_1': 'SE', 'data': {'name': ''}},
    ]},
}


def test_series_name_language_priority():
    meta = Metadata(FakeClient(), SHOW, ['en-US', 'fr-FR'])
    assert meta.series_name('Arte') == 'The Sandhamn Murders'
    assert Metadata(FakeClient(), SHOW, ['sv', 'fr-FR']).series_name('Arte') == 'Morden i Sandhamn'
    assert Metadata(FakeClient(), SHOW, ['original']).series_name('Arte') == 'Morden i Sandhamn'
    assert Metadata(FakeClient(), SHOW, ['arte', 'fr-FR']).series_name('Arte') == 'Arte'
    # No French translation (The Hack): TMDB shows the original name, not Arte's
    untranslated = {**SHOW, 'original_name': 'The Hack', 'translations': {'translations': [
        {'iso_639_1': 'fr', 'iso_3166_1': 'FR', 'data': {'name': ''}}]}}
    assert Metadata(FakeClient(), untranslated, ['fr-FR', 'arte']).series_name('Arte') == 'The Hack'


def test_localize_falls_back_across_languages():
    client = FakeClient(seasons={
        (1, 'fr-FR'): [tmdb_ep(1, 'Épisode 1'), tmdb_ep(2, 'Le retour', overview='fr')],
        (1, 'en-US'): [tmdb_ep(1, 'Pilot', overview='en'), tmdb_ep(2, 'Return')],
    })
    meta = Metadata(client, {**SHOW, 'id': 1}, ['fr-FR', 'arte', 'en-US'])
    generic = Episode('x-1', 'u', 's', 1, 1, 'Épisode 1', description='arte')
    meta.localize(generic)
    assert (generic.title, generic.description) == ('Pilot', 'arte')  # fr title is a placeholder
    titled = Episode('x-2', 'u', 's', 1, 2, 'Titre Arte')
    meta.localize(titled)
    assert (titled.title, titled.description) == ('Le retour', 'fr')


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    return tmp_path


def test_apply_end_to_end(data_home, monkeypatch):
    seasons = {(6, 'fr-FR'): S06_TMDB, (1, 'fr-FR'): S01_TMDB}
    client = FakeClient(search=[{'id': 55270, 'name': 'Meurtres à Sandhamn',
                                 'original_name': 'Morden i Sandhamn', 'first_air_date': '2010-01-09',
                                 'original_language': 'sv'}],
                        shows={55270: SHOW}, seasons=seasons)
    monkeypatch.setattr(metadata, 'TMDBClient', lambda key: client)

    series = Series('RC-022391', 'Meurtres à Sandhamn', 'fr', original_title='Morden I Sandhamn',
                    original_language='sv', year=2010)
    series.seasons = [Season('RC-022393', 6, 'Saison 6', s06_arte()),
                      Season('RC-X', 7, 'Saison 7', [arte_ep(7, 1, 4, 88, 'Enquête 10')])]
    logs = []
    metadata.apply(series, MetadataConfig(api_key='k'), log=logs.append)

    s6 = series.seasons[0].episodes
    assert [(e.label, e.title) for e in s6[:2]] == [('S06E01-E02', 'Le prix à payer'),
                                                     ('S06E03-E04', 'Au nom de la vérité')]
    assert s6[0].arte_number == 1 and s6[0].description == 'arte 6/1'  # no TMDB overview
    s7 = series.seasons[1].episodes[0]
    assert (s7.label, s7.title) == ('S07E01', 'Enquête 10')  # unknown on TMDB: Arte kept
    assert (series.tmdb_id, series.tvdb_id, series.year) == (55270, 158851, 2010)
    assert any('season 7 not found' in line for line in logs)
    assert metadata.load_ids() == {'RC-022391': 55270}

    # Second run: id remembered, no search
    client.calls.clear()
    metadata.apply(series, MetadataConfig(api_key='k'), log=logs.append)
    assert not [c for c in client.calls if c[0] == 'search']
