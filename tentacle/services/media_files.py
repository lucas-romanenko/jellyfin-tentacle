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
    """Delete every .strm/.nfo under a show directory, leaving anything else.

    Downloaded episodes, subtitles and artwork in the same folder (merged
    setups) are untouched. Empty season folders — and the show folder itself —
    are pruned only once nothing else is left in them. Returns the number of
    files deleted.
    """
    if not show_dir:
        return 0
    deleted = 0
    try:
        root = Path(show_dir)
        if not (root.exists() and root.is_dir()):
            return 0
        for f in list(root.rglob("*")):
            try:
                if f.is_file() and f.suffix.lower() in OWNED_SUFFIXES:
                    f.unlink()
                    deleted += 1
            except OSError as e:
                logger.warning(f"Failed to delete {f}: {e}")
        _prune_empty_dirs(root)
    except OSError as e:
        logger.warning(f"Failed to delete series files at {show_dir}: {e}")
    return deleted
