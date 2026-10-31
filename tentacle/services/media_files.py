"""Safe removal of the media files Tentacle itself wrote.

Tentacle only ever creates ``.strm`` and ``.nfo`` files. In the merged-folder
setup the docs encourage (VOD folder and the *arr downloads folder mounted at
the same physical path), a recursive delete of a title's directory also takes
out downloaded ``.mkv`` files, subtitles and artwork that Tentacle never
created — bypassing Sonarr/Radarr's recycle bin. So every delete path removes
files by extension and only prunes directories that are already empty.
"""
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Where Tentacle sees Radarr's and Sonarr's downloads (Settings → Library
# Paths). Read as media_files.RADARR_MEDIA_ROOT when used, never imported by
# name: the tests point them at a temp dir.
RADARR_MEDIA_ROOT = Path("/media/movies")
SONARR_MEDIA_ROOT = Path("/media/shows")

# Extensions Tentacle writes, and is therefore allowed to delete.
OWNED_SUFFIXES = {".strm", ".nfo"}

# Real media. Their presence means another tool owns files in this folder, so
# shared metadata (tvshow.nfo) must be left alone. Every video extension
# Radarr/Sonarr import (their MediaFileExtensions, less .strm and playlist or
# generic data formats), compared lower-case: a download named ".MKV" or an
# ".iso" rip is someone else's content too (#282).
MEDIA_SUFFIXES = {
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv", ".mpg", ".mpeg", ".m2ts",
    ".mts", ".mk3d", ".m2v", ".iso", ".img", ".vob", ".flv", ".rm", ".rmvb", ".divx", ".xvid",
    ".ogm", ".ogv", ".asf", ".3gp", ".qt", ".dvr-ms", ".wtv", ".nrg", ".pva", ".nsv", ".ty",
    ".avc", ".vp3", ".svq3", ".nuv", ".viv", ".dv", ".fli", ".bivx",
}


def arr_path_parts(arr_path: Optional[str]) -> list:
    """The components of a path as Radarr/Sonarr report it, split on both
    slashes: a Windows Radarr's look like D:\\Movies\\Heat (1995)\\Heat (1995).mkv,
    and Path() on Linux reads that as one file name in the current directory."""
    return [p for p in re.split(r"[\\/]", arr_path or "") if p]


def arr_parent(arr_path: Optional[str]) -> str:
    """The folder of a file Radarr/Sonarr reports, in their own path syntax
    ("" when there is none)."""
    m = re.match(r"^(.*)[\\/][^\\/]*$", (arr_path or "").rstrip("\\/"))
    return m.group(1) if m else ""


def is_vod_arr_path(arr_path: Optional[str]) -> bool:
    """A Radarr/Sonarr folder inside a VOD folder (hybrid series, the merged
    layout): the VOD sync's, not a download Tentacle must find under its mount."""
    return any(p.lower() == "vod" for p in arr_path_parts(arr_path))


def local_arr_folder(arr_folder: Optional[str], root: Path, arr_root: str) -> Optional[Path]:
    """Tentacle's own path of a folder Radarr/Sonarr calls `arr_folder`, or
    None when Tentacle can't see it.

    First the documented mapping (Radarr's `arr_root`, /data/movies, is
    Tentacle's `root`, /media/movies; a path outside `arr_root` as is, for a
    mount at the same path). Then the folder by NAME under `root`, as the VOD
    sync finds titles: Radarr's root folder can be called anything
    (/data/media/movies, D:\\Movies) and Tentacle only mounts the same host
    folder (#649). Not for a VOD folder: a same-named folder under `root`
    would be another one.
    """
    parts = arr_path_parts(arr_folder)
    if not parts:
        return None
    candidates = []
    if arr_folder.startswith("/"):
        arr_root = arr_root.rstrip("/")
        if arr_folder == arr_root or arr_folder.startswith(arr_root + "/"):
            candidates.append(Path(str(root) + arr_folder[len(arr_root):]))
        else:
            candidates.append(Path(arr_folder))
    if not is_vod_arr_path(arr_folder):
        candidates.append(Path(root) / parts[-1])
    return next((c for c in candidates if c.is_dir()), None)


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty directories under (and including) root, deepest first."""
    try:
        for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def delete_movie_files(strm_path) -> int:
    """Delete a movie's .strm + .nfo and its folder if that leaves it empty.

    ``strm_path`` is the path of the .strm file itself. Returns the number of
    files deleted.
    """
    if not strm_path:
        return 0
    deleted = 0
    try:
        strm = Path(strm_path)
        if strm.suffix == ".strm" and strm.exists():
            strm.unlink()
            deleted += 1
        # The NFO sits beside the .strm with the same stem. In a merged folder a
        # downloaded copy can share that stem ("Heat (1995).mkv"), and Radarr's
        # NFO for it has the same name — then it describes the download, not us.
        nfo = strm.with_suffix(".nfo")
        has_download = _has_same_stem_media(strm)
        if nfo.exists() and not has_download:
            nfo.unlink()
            deleted += 1
        parent = strm.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as e:
        logger.warning(f"Failed to delete movie files at {strm_path}: {e}")
    return deleted


def delete_series_files(show_dir) -> int:
    """Delete the .strm files under a show directory, and only their own NFOs.

    Downloaded episodes, subtitles and artwork in the same folder (merged
    setups) are untouched. Crucially, so are *other tools'* NFOs: Sonarr's
    metadata option and Jellyfin's NFO saver write per-episode `SxxEyy.nfo` and
    `season.nfo` next to the downloaded `.mkv`s, and deleting every `.nfo` in
    the tree stripped the metadata off episodes Tentacle never touched. Only an
    NFO sharing its stem with a `.strm` we are removing is ours, unless a
    download with that stem sits beside it: then it describes the download (#614).

    `tvshow.nfo` is shared by both sources (Tentacle writes it for VOD and for
    Sonarr series), so it is removed only when no real media is left anywhere in
    the tree. Empty folders are pruned last. Returns the number of files
    deleted.
    """
    if not show_dir:
        return 0
    deleted = 0
    try:
        root = Path(show_dir)
        if not (root.exists() and root.is_dir()):
            return 0

        # Parked copies too (".strm.parked" beside a download, services.one_series)
        for strm in list(root.rglob("*.strm")) + list(root.rglob("*.strm.parked")):
            try:
                nfo = strm.with_suffix(".nfo") if strm.suffix == ".strm" else strm.with_name(
                    strm.name[:-len(".strm.parked")] + ".nfo")
                has_download = _has_same_stem_media(strm)
                strm.unlink()
                deleted += 1
                if nfo.exists() and not has_download:
                    nfo.unlink()
                    deleted += 1
            except OSError as e:
                logger.warning(f"Failed to delete {strm}: {e}")

        # Shared show-level metadata: only ours to remove once nothing else lives here.
        if not _has_media_files(root):
            for shared in (root / "tvshow.nfo", root / "season.nfo"):
                try:
                    if shared.exists():
                        shared.unlink()
                        deleted += 1
                except OSError as e:
                    logger.warning(f"Failed to delete {shared}: {e}")

        _prune_empty_dirs(root)
    except OSError as e:
        logger.warning(f"Failed to delete series files at {show_dir}: {e}")
    return deleted


def _has_same_stem_media(strm: Path) -> bool:
    """True when a download with the .strm's name sits beside it, in any case
    ("Heat (1995).MKV") and any importable format ("Heat (1995).iso")."""
    try:
        return any(f.stem == strm.stem and f.suffix.lower() in MEDIA_SUFFIXES and f.is_file()
                   for f in strm.parent.iterdir())
    except OSError:
        return True  # can't tell: keep the NFO


def _has_media_files(root: Path) -> bool:
    """True when any real video file remains under root (someone else's content)."""
    try:
        return any(
            f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES
            for f in root.rglob("*")
        )
    except OSError:
        return True  # can't tell — assume occupied and leave shared files alone
