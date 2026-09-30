"""Configuration: TOML file merged over built-in defaults."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    pass


@dataclass
class OutputConfig:
    directory: str = '.'
    series_dir: str = '{series} ({year})'
    season_dir: str = 'Season {season:02d}'
    filename: str = '{series} - S{season:02d}E{episode:02d} - {title}'


@dataclass
class VideoConfig:
    max_height: int = 1080
    # Preference order when several codecs exist at the same height: "hevc" (H.265), "avc" (H.264)
    codecs: list[str] = field(default_factory=lambda: ['hevc', 'avc'])


@dataclass
class AudioConfig:
    # Tracks to include, in order; the first one found becomes the default track.
    #   "original"   -> original version (VO), whatever its language
    #   "fr", "de"   -> that language (excluding audio description / "confort audio")
    #   "fr-ad"      -> audio description,  "fr-comfort" -> "confort audio" (clearer dialogue)
    tracks: list[str] = field(default_factory=lambda: ['original', 'fr'])


@dataclass
class SubtitlesConfig:
    # "fr" -> full subtitles, "fr-forced" -> forced (foreign lines only), "fr-acc" -> SDH
    tracks: list[str] = field(default_factory=lambda: ['fr', 'fr-forced'])
    # Default subtitle track: a key from `tracks`, "none", or "auto"
    # ("auto": forced subs when the default audio is in the subtitle language, full subs otherwise)
    default: str = 'auto'


@dataclass
class MetadataConfig:
    # "tmdb": series name / year / numbering / titles from themoviedb.org; "none": Arte data only
    provider: str = 'tmdb'
    # TMDB API key (v3) or read access token (v4); the TMDB_API_KEY environment variable also works
    api_key: str = ''
    # Priority order for series name, episode titles and synopses. TMDB languages ("fr-FR",
    # "en-US", "fr"...), "arte" (Arte's own titles) and "original" (the show's original language)
    languages: list[str] = field(default_factory=lambda: ['fr-FR', 'arte', 'en-US'])

    @property
    def key(self) -> str:
        return self.api_key or os.environ.get('TMDB_API_KEY', '')


@dataclass
class Config:
    output: OutputConfig = field(default_factory=OutputConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    subtitles: SubtitlesConfig = field(default_factory=SubtitlesConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)

    @property
    def output_dir(self) -> Path:
        return Path(os.path.expandvars(self.output.directory)).expanduser()


def default_config_path() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(base) / 'arte-dl' / 'config.toml'


def _apply(section_obj, data: dict, section: str) -> None:
    known = {f.name: f for f in fields(section_obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f'Unknown option [{section}] {key}')
        default = getattr(section_obj, key)
        if isinstance(default, list) and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ConfigError(f'[{section}] {key} must be a list of strings')
        if isinstance(default, (str, int)) and type(value) is not type(default):
            raise ConfigError(f'[{section}] {key} must be a {type(default).__name__}')
        setattr(section_obj, key, value)


def load_config(path: Path | None) -> Config:
    """Load `path`, or the default location if it exists, or built-in defaults."""
    cfg = Config()
    if path is None:
        path = default_config_path()
        if not path.exists():
            return cfg
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f'Config file not found: {path}') from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{path}: {e}') from None

    for section, values in data.items():
        if not hasattr(cfg, section) or not isinstance(values, dict):
            raise ConfigError(f'Unknown section [{section}]')
        _apply(getattr(cfg, section), values, section)
    _validate(cfg)
    return cfg


LANGUAGE_RE = re.compile(r'^(?:[a-z]{2}(?:-[A-Z]{2})?|arte|original)$')


def _validate(cfg: Config) -> None:
    if cfg.metadata.provider not in ('tmdb', 'none'):
        raise ConfigError('[metadata] provider must be "tmdb" or "none"')
    bad = [lang for lang in cfg.metadata.languages if not LANGUAGE_RE.match(lang)]
    if bad:
        raise ConfigError(f'[metadata] languages: invalid {bad} (expected "fr-FR", "fr", "arte", "original")')
