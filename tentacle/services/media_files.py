"""Safe removal of the media files Tentacle itself wrote.

Tentacle only ever creates ``.strm`` and ``.nfo`` files. In the merged-folder
setup the docs encourage (VOD folder and the *arr downloads folder mounted at
the same physical path), a recursive delete of a title's directory also takes
out downloaded ``.mkv`` files, subtitles and artwork that Tentacle never
created — bypassing Sonarr/Radarr's recycle bin. So every delete path removes
files by extension and only prunes directories that are already empty.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions Tentacle writes, and is therefore allowed to delete.
OWNED_SUFFIXES = {".strm", ".nfo"}

# Real media. Their presence means another tool owns files in this folder, so
# shared metadata (tvshow.nfo) must be left alone.
MEDIA_SUFFIXES = {
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv", ".mpg", ".mpeg", ".m2ts",
}


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
        # The NFO sits beside the .strm with the same stem.
        nfo = strm.with_suffix(".nfo")
        if nfo.exists():
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
    NFO sharing its stem with a `.strm` we are removing is ours.

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

        for strm in list(root.rglob("*.strm")):
            try:
                nfo = strm.with_suffix(".nfo")
                strm.unlink()
                deleted += 1
                if nfo.exists():
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


def _has_media_files(root: Path) -> bool:
    """True when any real video file remains under root (someone else's content)."""
    try:
        return any(
            f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES
            for f in root.rglob("*")
        )
    except OSError:
        return True  # can't tell — assume occupied and leave shared files alone
