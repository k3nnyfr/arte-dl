# arte-dl

Wrapper autour de [yt-dlp](https://github.com/yt-dlp/yt-dlp) pour télécharger une série
arte.tv complète (toutes ses saisons) en MKV, selon des préférences de qualité, de pistes
audio et de sous-titres, et la ranger dans une arborescence de vidéothèque (Plex / Jellyfin / Kodi) :

```
Meurtres à Sandhamn (2010)/
├── Season 01/
│   ├── Meurtres à Sandhamn - S01E01 - Enquête 1 - La reine de la Baltique.mkv
│   └── …
└── Season 06/
    └── Meurtres à Sandhamn - S06E01-E02 - Le prix à payer.mkv
```

Avec une clé [TMDB](https://www.themoviedb.org/), le nom de la série, l'année, la numérotation
et les titres d'épisodes sont alignés sur TMDB (la référence de Jellyfin, et une source de Plex).

## Installation

Nécessite Python ≥ 3.10 et `ffmpeg` dans le `PATH`.

```sh
uv tool install .          # ou : pipx install .
arte-dl --version
```

Pour mettre à jour yt-dlp (Arte change régulièrement son API) : `uv tool upgrade arte-dl`.

## Utilisation

```sh
# Toute la série
arte-dl https://www.arte.tv/fr/videos/RC-027900/the-hack-sur-ecoute/

# Voir l'arborescence prévue sans rien télécharger
arte-dl --list https://www.arte.tv/fr/videos/RC-022391/meurtres-a-sandhamn/

# Voir aussi les pistes retenues pour chaque épisode
arte-dl --dry-run -s 4 https://www.arte.tv/fr/videos/RC-022391/meurtres-a-sandhamn/

# Certaines saisons / certains épisodes, dans un autre dossier
arte-dl -s 1,3-5 -e 1-2 -o ~/Vidéos/Séries https://www.arte.tv/fr/videos/RC-022391/meurtres-a-sandhamn/

# Corriger l'identification TMDB (mémorisée pour les fois suivantes)
arte-dl --list --tmdb-id 55270 https://www.arte.tv/fr/videos/RC-022391/meurtres-a-sandhamn/
```

`-s` / `-e` portent sur la numérotation finale (celle de TMDB quand elle est utilisée),
celle qu'affiche `--list`.

Types d'URL acceptés :

| URL                                   | Téléchargé                          |
|---------------------------------------|-------------------------------------|
| série `…/videos/RC-xxxxxx/…`          | toutes les saisons disponibles      |
| saison `…/videos/RC-xxxxxx/…`         | cette saison                        |
| épisode `…/videos/059534-001-A/…`     | cet épisode, bien numéroté/rangé    |

Les fichiers déjà présents sont ignorés (`--force` pour les retélécharger) : relancer la
commande reprend simplement là où elle s'était arrêtée, ou récupère les nouveaux épisodes.

## Configuration

Fichier TOML : `~/.config/arte-dl/config.toml` par défaut, ou `-c fichier.toml`.
Voir [`config.example.toml`](config.example.toml) pour toutes les options. Exemple :

```toml
[output]
directory = "~/Vidéos/Séries"

[video]
max_height = 1080
codecs = ["hevc", "avc"]

[audio]
tracks = ["original", "fr"]       # VO par défaut + VF

[subtitles]
tracks = ["fr", "fr-forced"]
default = "auto"

[metadata]
api_key = "…"                           # ou variable d'environnement TMDB_API_KEY
languages = ["fr-FR", "arte", "en-US"]  # priorité pour les noms, titres et résumés
```

## Métadonnées TMDB

Clé gratuite : créer un compte sur themoviedb.org, puis *Paramètres → API*. La clé API (v3)
comme le jeton d'accès en lecture (v4) conviennent.

- **Identification de la série** : recherche par titre original et titre Arte, départagée
  par l'année de production et la langue originale fournies par Arte. En cas de doute
  (score faible ou deux candidats proches), rien n'est deviné : les données Arte sont gardées
  et `--tmdb-id` permet de trancher. L'association est mémorisée dans
  `~/.local/share/arte-dl/tmdb-ids.json`.
- **Numérotation** : saison par saison, les épisodes Arte sont associés aux épisodes TMDB :
  - même nombre d'épisodes : correspondance directe ;
  - Arte fusionne des épisodes (ex. Meurtres à Sandhamn saison 6 : 4 × 88 min sur Arte,
    8 × 45 min sur TMDB) : le fichier est nommé `S06E01-E02`, format multi-épisode reconnu
    par Plex et Jellyfin. Les durées sont vérifiées ;
  - sinon, alignement par durée si toute la saison est disponible ;
  - à défaut, ou si la saison n'existe pas sur TMDB, la numérotation Arte est conservée
    (et signalée).
- **Titres et résumés** : pris dans l'ordre de `languages`. Une langue qui n'a pas le titre
  (ou seulement « Épisode 3 ») passe à la suivante. Pour deux épisodes fusionnés,
  « X (part 1) » + « X (part 2) » donnent « X ».

## Fonctionnement

1. **Structure** : la série, ses saisons et épisodes sont lus via l'API Arte (celle
   qu'utilise yt-dlp). Le numéro de saison vient du titre (« Saison 4 »), le numéro d'épisode
   du « (1/3) », le titre de l'épisode du sous-titre Arte (à défaut « Épisode N »), puis
   tout cela est corrigé par TMDB si une clé est configurée.
2. **Sélection** : pour chaque épisode, yt-dlp extrait les formats ; arte-dl choisit la vidéo
   et les pistes audio d'après la configuration. Les identifiants de format Arte
   (`VF-STF-audio_0-suédois__VO_`…) varient d'un épisode à l'autre, d'où la sélection
   par langue et par type plutôt que par identifiant.
3. **Téléchargement** : yt-dlp télécharge et fusionne vidéo + audios, et récupère les
   sous-titres WebVTT.
4. **Remux final** (ffmpeg) : sous-titres convertis en SRT, langue et titre de chaque piste,
   pistes par défaut / forcées / SDH, tags série / saison / épisode. Le fichier est écrit en
   `.part.mkv` puis renommé, donc un fichier `.mkv` présent est toujours complet.

La conversion VTT → SRT est faite en Python : les VTT d'Arte (fins de ligne CRLF) donnent
des sous-titres **vides** avec ffmpeg 4.4 (celui d'Ubuntu 22.04), y compris via `yt-dlp --convert-subs srt`.

## Tests

```sh
uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```
