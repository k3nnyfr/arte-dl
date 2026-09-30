from pathlib import Path

from arte_dl import metadata
from arte_dl.arte_api import ArteClient, Episode, Movie, kind_of, merge
from arte_dl.config import Config, MetadataConfig
from arte_dl.download import _tags, destination
from arte_dl.metadata import find_movie, parse_ref


# OPA programs, trimmed from the API (September 2026)
FILM = {'programId': '051404-000-A', 'catalogType': 'MOVIE', 'genre': {'code': 2, 'label': 'Cinéma'},
        'title': 'Les vieux espions vous saluent bien', 'originalTitle': '', 'productionYear': 2017,
        'originalLanguage': {'iso6391Code': 'de'}, 'durationSeconds': 5132, 'shortDescription': 'film',
        'collections': [{'collectionId': 'RC-027882', 'catalogType': 'TOPIC', 'title': 'Comédie',
                         'url': 'https://www.arte.tv/fr/videos/RC-027882/comedie/'}]}
GODFATHER = {'programId': '045559-000-A', 'catalogType': 'MINI_EPISODE',
             'genre': {'code': 2, 'label': 'Cinéma'}, 'title': 'Le parrain',
             'originalTitle': '(The Godfather)', 'productionYear': 1972,
             'originalLanguage': {'iso6391Code': 'en'},
             'collections': [{'collectionId': 'RC-028368', 'catalogType': 'TOPIC'}]}
LVMH = {'programId': 'RC-028069', 'catalogType': 'MINI_SERIES',
        'genre': {'code': 1, 'label': 'Documentaires et reportages'}, 'title': "L'empire LVMH",
        'originalTitle': 'LVMH – das Imperium der Luxusmarken', 'productionYear': 2026,
        'shortDescription': 'saga'}
LVMH_PART2 = {'programId': '122704-002-A', 'catalogType': 'MINI_EPISODE',
              'genre': {'code': 1}, 'title': "L'empire LVMH (2/2)",
              'collections': [{'collectionId': 'RC-028069', 'catalogType': 'MINI_SERIES',
                               'url': 'https://www.arte.tv/fr/videos/RC-028069/l-empire-lvmh/'}]}
LVMH_ITEMS = [
    {'providerId': '122704-001-A', 'title': "L'empire LVMH (1/2)", 'subtitle': 'Un morceau du rêve',
     'duration': {'seconds': 3596}},
    {'providerId': '122704-002-A', 'title': "L'empire LVMH (2/2)", 'subtitle': "L’État dans l'État",
     'duration': {'seconds': 3680}},
]
TRILOGY = {'programId': 'RC-028368', 'catalogType': 'TOPIC', 'title': 'Le parrain - La trilogie'}


class FakeArte(ArteClient):
    programs = {p['programId']: p for p in (FILM, GODFATHER, LVMH, LVMH_PART2, TRILOGY,
                                            {**GODFATHER, 'programId': '045560-000-A',
                                             'title': 'Le parrain II', 'productionYear': 1975})}
    playlists = {'RC-028069': LVMH_ITEMS,
                 'RC-028368': [{'providerId': '045559-000-A'}, {'providerId': '045560-000-A'}]}

    def program(self, pid):
        return self.programs[pid]

    def playlist(self, cid):
        return {'items': self.playlists.get(cid, []), 'metadata': {}}


def test_kind_of():
    assert kind_of(FILM) == kind_of(GODFATHER) == 'film'
    assert kind_of(LVMH) == 'documentary'
    assert kind_of({'catalogType': 'SERIES', 'genre': {'code': 3}}) is None


def test_film_is_not_an_episode_of_its_topic():
    [movie] = FakeArte('fr').resolve('https://www.arte.tv/fr/videos/051404-000-A/x/')
    assert isinstance(movie, Movie) and movie.kind == 'film' and not movie.multipart
    assert (movie.title, movie.year, movie.original_language) == (FILM['title'], 2017, 'de')


def test_trilogy_gives_each_film():
    movies = FakeArte('fr').resolve('https://www.arte.tv/fr/videos/RC-028368/le-parrain/')
    assert [(m.title, m.year, m.original_title) for m in movies] == [
        ('Le parrain', 1972, 'The Godfather'), ('Le parrain II', 1975, 'The Godfather')]


def test_documentary_in_parts():
    client = FakeArte('fr')
    [doc] = client.resolve('https://www.arte.tv/fr/videos/RC-028069/l-empire-lvmh/')
    assert (doc.kind, doc.total_parts, doc.multipart) == ('documentary', 2, True)
    assert [(p.number, p.title) for p in doc.parts] == [(1, 'Un morceau du rêve'), (2, "L’État dans l'État")]
    # A single part's URL: that part of the whole documentary
    [doc] = client.resolve('https://www.arte.tv/fr/videos/122704-002-A/x/')
    assert (doc.id, [p.number for p in doc.parts], doc.multipart) == ('RC-028069', [2], True)
    # Or as a mini-series
    [series] = client.resolve('https://www.arte.tv/fr/videos/RC-028069/l-empire-lvmh/', as_series=True)
    assert [e.label for e in series.episodes] == ['S01E01', 'S01E02']


def test_merge_parts():
    a = Movie('RC-1', 'Doc', 'fr', parts=[Episode('p2', 'u', 'Doc', 1, 2, 'b')], total_parts=2)
    b = Movie('RC-1', 'Doc', 'fr', parts=[Episode('p1', 'u', 'Doc', 1, 1, 'a')], total_parts=2)
    [m] = merge([a, b])
    assert [p.id for p in m.parts] == ['p1', 'p2']


def test_movie_destination():
    cfg = Config()
    cfg.output.directory = '/media/series'
    cfg.output.movies_directory = '/media/films'
    film = Movie('051404-000-A', 'Les vieux espions : saluts', 'fr', year=2017,
                 parts=[Episode('051404-000-A', 'u', 'x', 1, 1, 'x')])
    assert destination(film, film.parts[0], cfg) == Path(
        '/media/films/Les vieux espions - saluts (2017)/Les vieux espions - saluts (2017).mkv')
    doc = Movie('RC-028069', "L'empire LVMH", 'fr', kind='documentary', total_parts=2,
                parts=[Episode('122704-002-A', 'u', 'x', 1, 2, 'Deux')])
    # documentaries_directory unset: movies_directory
    assert destination(doc, doc.parts[0], cfg) == Path(
        "/media/films/L'empire LVMH/L'empire LVMH - part2.mkv")
    cfg.output.documentaries_directory = '/media/docs'
    doc.tmdb_id, doc.year = 42, 2026
    cfg.output.movie_dir = '{title} ({year}) [tmdbid-{tmdb_id}]'
    assert destination(doc, doc.parts[0], cfg) == Path(
        "/media/docs/L'empire LVMH (2026) [tmdbid-42]/L'empire LVMH (2026) - part2.mkv")
    tags = _tags(doc, doc.parts[0])
    assert (tags['title'], tags['part_number'], tags['tmdb']) == ("L'empire LVMH (2/2) - Deux", '2', 'movie/42')


class FakeTMDB:
    def __init__(self, results, details):
        self.results, self._details, self.searches = results, details, []

    def search(self, query, language, kind='tv'):
        self.searches.append((query, kind))
        return tuple(r for r in self.results.get(kind, ()) if query.lower() in str(r).lower())

    def details(self, kind, tmdb_id):
        return self._details[(kind, tmdb_id)]


GODFATHER_TMDB = {'id': 238, 'title': 'Le Parrain', 'original_title': 'The Godfather',
                  'release_date': '1972-03-14', 'original_language': 'en'}


def test_find_movie():
    movie = Movie('045559-000-A', 'Le parrain', 'fr', original_title='The Godfather',
                  original_language='en', year=1972)
    client = FakeTMDB({'movie': [GODFATHER_TMDB, {'id': 240, 'title': 'Le Parrain, 2e partie',
                                                  'original_title': 'The Godfather Part II',
                                                  'release_date': '1974-12-20'}]}, {})
    assert find_movie(client, movie, 'fr-FR')[0] == ('movie', 238)
    # A documentary is also looked for among TV shows
    doc = Movie('RC-1', 'Tchernobyl', 'fr', kind='documentary', year=2026)
    client = FakeTMDB({'tv': [{'id': 7, 'name': 'Tchernobyl', 'first_air_date': '2026-01-01'}]}, {})
    assert find_movie(client, doc, 'fr-FR', ('movie', 'tv'))[0] == ('tv', 7)


def test_apply_movie(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    details = {**GODFATHER_TMDB, 'external_ids': {'imdb_id': 'tt0068646'}, 'translations': {'translations': [
        {'iso_639_1': 'fr', 'iso_3166_1': 'FR', 'data': {'title': 'Le Parrain', 'overview': 'Corleone'}}]}}
    client = FakeTMDB({'movie': [GODFATHER_TMDB]}, {('movie', 238): details})
    monkeypatch.setattr(metadata, 'TMDBClient', lambda key: client)
    movie = Movie('045559-000-A', 'Le parrain', 'fr', original_title='The Godfather', year=1972,
                  description='arte', parts=[Episode('045559-000-A', 'u', 'Le parrain', 1, 1, 'Le parrain')])
    metadata.apply_movie(movie, MetadataConfig(api_key='k'), log=lambda _: None)
    assert (movie.title, movie.description, movie.tmdb_id, movie.imdb_id) == (
        'Le Parrain', 'Corleone', 238, 'tt0068646')
    assert movie.parts[0].title == 'Le Parrain'
    assert metadata.load_ids() == {'045559-000-A': 'movie/238'}
    client.searches.clear()
    metadata.apply_movie(movie, MetadataConfig(api_key='k'), log=lambda _: None)
    assert not client.searches


def test_parse_ref():
    assert parse_ref('123') == ('movie', 123)
    assert parse_ref('tv/5') == ('tv', 5)
    assert parse_ref(55270, 'tv') == ('tv', 55270)
    assert parse_ref(None) is None
