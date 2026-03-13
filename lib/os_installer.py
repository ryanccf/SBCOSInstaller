"""
os_installer.py - Download and install OS releases.

Generic installer that works with any OS profile defined in os_profiles.py.
Supports both zip-extract and raw-image install methods.
"""

import json
import logging
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

NETWORK_TIMEOUT = 60
CHUNK_SIZE = 64 * 1024
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}

_ARCHIVE_SUFFIXES = ('.zip', '.img.gz', '.img.xz', '.img.7z.001')


def _github_get(url: str) -> Any:
    request = Request(url, headers=_GITHUB_HEADERS)
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            data = response.read()
            return json.loads(data)
    except HTTPError as exc:
        raise ConnectionError(
            f"GitHub API returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except URLError as exc:
        raise ConnectionError(
            f"Unable to reach {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ConnectionError(
            f"Request to {url} timed out after {NETWORK_TIMEOUT}s"
        ) from exc


def _find_zip_asset(assets: list[dict]) -> Optional[dict]:
    for asset in assets:
        if asset.get("name", "").lower().endswith(".zip"):
            return asset
    return None


def fetch_releases(profile: dict) -> dict[str, list[dict[str, Any]]]:
    """Query GitHub releases for the given OS profile.

    Returns {"stable": [...], "beta": [...]}.
    When profile has an "asset_filter" regex, all matching assets are listed
    individually (useful for multi-platform or base/extras releases).
    Multi-part archives (.001/.002/...) are detected and companion URLs
    are included in the entry.
    """
    raw_releases: list[dict] = _github_get(profile["releases_url"])

    if not isinstance(raw_releases, list):
        raise ValueError("Unexpected GitHub API response: expected a JSON array")

    asset_filter = profile.get("asset_filter")
    stable: list[dict[str, Any]] = []
    beta: list[dict[str, Any]] = []

    for release in raw_releases:
        assets = release.get("assets", [])

        if asset_filter:
            matching = [a for a in assets if re.search(asset_filter, a.get("name", ""))]
        else:
            zip_asset = _find_zip_asset(assets)
            matching = [zip_asset] if zip_asset else []

        for asset in matching:
            asset_name = asset.get("name", "")
            display_name = asset_name if asset_filter else release.get("name", "")
            total_size = asset.get("size", 0)

            entry: dict[str, Any] = {
                "tag_name": release.get("tag_name", ""),
                "name": display_name,
                "prerelease": bool(release.get("prerelease", False)),
                "published_at": release.get("published_at", ""),
                "browser_download_url": asset.get("browser_download_url", ""),
                "size": total_size,
            }

            # Detect multi-part archive companions (.001 -> .002, .003, ...)
            if re.search(r'\.\d{3}$', asset_name) and asset_name.endswith('.001'):
                base_prefix = asset_name[:-3]  # e.g. "foo.img.7z."
                companions = []
                for other in assets:
                    other_name = other.get("name", "")
                    if (other_name != asset_name
                            and other_name.startswith(base_prefix)
                            and re.search(r'\.\d{3}$', other_name)):
                        companions.append(other.get("browser_download_url", ""))
                        total_size += other.get("size", 0)
                if companions:
                    entry["companion_urls"] = sorted(companions)
                    entry["size"] = total_size
                # Show base archive name (without .001) for display
                entry["name"] = base_prefix.rstrip(".")

            if entry["prerelease"]:
                beta.append(entry)
            else:
                stable.append(entry)

    return {"stable": stable, "beta": beta}


def download_release(
    url: str,
    dest_dir: str | Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download a release file into dest_dir. Returns path to the file."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = url.rsplit("/", 1)[-1] or "release.zip"
    dest_path = dest_dir / filename

    request = Request(url, headers=_GITHUB_HEADERS)

    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            total_bytes = int(response.headers.get("Content-Length", 0))
            bytes_downloaded = 0

            with open(dest_path, "wb") as fh:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    bytes_downloaded += len(chunk)
                    if progress_callback is not None:
                        progress_callback(bytes_downloaded, total_bytes)

    except HTTPError as exc:
        dest_path.unlink(missing_ok=True)
        raise ConnectionError(f"Download failed with HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        dest_path.unlink(missing_ok=True)
        raise ConnectionError(f"Unable to reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        dest_path.unlink(missing_ok=True)
        raise ConnectionError(f"Download timed out after {NETWORK_TIMEOUT}s") from exc

    logger.info("Downloaded %s (%d bytes)", dest_path, bytes_downloaded)
    return dest_path.resolve()


def download_multipart_release(
    urls: list[str],
    dest_dir: str | Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download all parts of a multi-part archive. Returns path to the .001 file."""
    if not urls:
        raise ValueError("No URLs provided for multipart download")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Calculate total size across all parts
    total_bytes = 0
    part_sizes = []
    for url in urls:
        request = Request(url, headers=_GITHUB_HEADERS, method="HEAD")
        try:
            with urlopen(request, timeout=NETWORK_TIMEOUT) as response:
                size = int(response.headers.get("Content-Length", 0))
                part_sizes.append(size)
                total_bytes += size
        except (HTTPError, URLError, TimeoutError):
            part_sizes.append(0)

    bytes_so_far = 0
    first_path = None

    for url, part_size in zip(urls, part_sizes):
        offset = bytes_so_far

        def part_progress(downloaded, total, _offset=offset):
            if progress_callback is not None:
                progress_callback(_offset + downloaded, total_bytes)

        path = download_release(url, dest_dir, part_progress)
        if first_path is None:
            first_path = path
        bytes_so_far += part_size

    return first_path


def decompress_image(
    compressed_path: str | Path,
    dest_dir: str | Path,
) -> Path:
    """Decompress a .img.gz, .img.xz, or .img.7z archive.

    For .7z multi-part archives, pass the .001 file.
    Returns path to the decompressed .img file.
    """
    compressed_path = Path(compressed_path)
    dest_dir = Path(dest_dir)
    name = compressed_path.name

    if name.endswith('.img.7z.001') or name.endswith('.img.7z'):
        result = subprocess.run(
            ["7z", "x", f"-o{dest_dir}", "-y", str(compressed_path)],
            capture_output=True, text=True, timeout=3600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"7z extraction failed: {result.stderr}")
        # Find the extracted .img file
        for f in dest_dir.iterdir():
            if f.name.endswith('.img') and f.is_file():
                return f
        raise RuntimeError("No .img file found after 7z extraction")

    elif name.endswith('.img.xz'):
        import lzma
        img_name = name[:-3]  # strip .xz
        img_path = dest_dir / img_name
        with lzma.open(compressed_path, 'rb') as src, open(img_path, 'wb') as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        return img_path

    elif name.endswith('.img.gz'):
        import gzip
        img_name = name[:-3]  # strip .gz
        img_path = dest_dir / img_name
        with gzip.open(compressed_path, 'rb') as src, open(img_path, 'wb') as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        return img_path

    else:
        raise ValueError(f"Unsupported archive format: {name}")


def get_downloaded_releases(downloads_dir: str | Path) -> list[dict[str, Any]]:
    """List already-downloaded release archives in downloads_dir."""
    downloads_dir = Path(downloads_dir)
    if not downloads_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for entry in downloads_dir.iterdir():
        if not entry.is_file():
            continue
        if any(entry.name.lower().endswith(s) for s in _ARCHIVE_SUFFIXES):
            stat = entry.stat()
            results.append({
                "filename": entry.name,
                "path": str(entry.resolve()),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })

    results.sort(key=lambda r: r["modified"], reverse=True)
    return results


def get_required_space(zip_path: str | Path) -> int:
    """Return the total uncompressed size in bytes of a zip archive."""
    with zipfile.ZipFile(Path(zip_path), "r") as zf:
        return sum(info.file_size for info in zf.infolist())


def extract_to_sd(
    zip_path: str | Path,
    sd_mount_point: str | Path,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> tuple[bool, str]:
    """Extract a release zip to an SD card mount point.

    Returns (success, message).
    """
    zip_path = Path(zip_path)
    sd_mount_point = Path(sd_mount_point)

    if not zip_path.is_file():
        return False, f"Zip file not found: {zip_path}"
    if not sd_mount_point.is_dir():
        return False, f"SD card mount point does not exist: {sd_mount_point}"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            total_files = len(members)

            for index, member in enumerate(members):
                target = (sd_mount_point / member.filename).resolve()
                if not str(target).startswith(str(sd_mount_point.resolve())):
                    logger.warning("Skipping potentially unsafe path: %s", member.filename)
                    continue

                if progress_callback is not None:
                    progress_callback(member.filename, index, total_files)

                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            dst.write(chunk)

                    if member.external_attr > 0:
                        unix_mode = member.external_attr >> 16
                        if unix_mode:
                            try:
                                os.chmod(target, unix_mode)
                            except OSError:
                                pass

    except zipfile.BadZipFile as exc:
        return False, f"Invalid zip file: {exc}"
    except OSError as exc:
        return False, f"Extraction error: {exc}"
    except Exception as exc:
        return False, f"Unexpected error during extraction: {exc}"

    return True, "Extraction completed successfully."


def verify_extraction(sd_mount_point: str | Path, profile: dict) -> tuple[bool, list[str]]:
    """Check that expected directories exist on the SD card.

    Returns (success, missing_dirs).
    """
    sd_mount_point = Path(sd_mount_point)
    missing = [d for d in profile["expected_dirs"] if not (sd_mount_point / d).is_dir()]
    return (len(missing) == 0, missing)
