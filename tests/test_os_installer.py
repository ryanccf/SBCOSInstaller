"""Tests for os_installer.py - Release fetching, downloading, extraction."""

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

import pytest

from lib.os_installer import (
    _find_zip_asset,
    _github_get,
    fetch_releases,
    download_release,
    download_multipart_release,
    decompress_image,
    get_downloaded_releases,
    get_required_space,
    extract_to_sd,
    verify_extraction,
)
from lib.os_profiles import OS_PROFILES


# ---------------------------------------------------------------------------
# _find_zip_asset
# ---------------------------------------------------------------------------

class TestFindZipAsset:
    def test_finds_zip(self):
        assets = [
            {"name": "README.md", "browser_download_url": "..."},
            {"name": "release.zip", "browser_download_url": "http://example.com/release.zip"},
        ]
        result = _find_zip_asset(assets)
        assert result is not None
        assert result["name"] == "release.zip"

    def test_case_insensitive(self):
        assets = [{"name": "Release.ZIP", "browser_download_url": "..."}]
        result = _find_zip_asset(assets)
        assert result is not None

    def test_no_zip(self):
        assets = [{"name": "release.tar.gz"}]
        assert _find_zip_asset(assets) is None

    def test_empty_assets(self):
        assert _find_zip_asset([]) is None


# ---------------------------------------------------------------------------
# _github_get (mocked)
# ---------------------------------------------------------------------------

class TestGithubGet:
    @patch("lib.os_installer.urlopen")
    def test_successful_request(self, mock_urlopen):
        data = [{"tag_name": "v1.0"}]
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = _github_get("https://api.github.com/repos/test/test/releases")
        assert result == data

    @patch("lib.os_installer.urlopen")
    def test_http_error_raises_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            url="http://example.com", code=403, msg="Forbidden",
            hdrs=None, fp=None,
        )
        with pytest.raises(ConnectionError, match="403"):
            _github_get("https://api.github.com/repos/test/test/releases")

    @patch("lib.os_installer.urlopen")
    def test_url_error_raises_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("DNS failure")
        with pytest.raises(ConnectionError, match="Unable to reach"):
            _github_get("https://api.github.com/repos/test/test/releases")

    @patch("lib.os_installer.urlopen")
    def test_timeout_raises_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError()
        with pytest.raises(ConnectionError, match="timed out"):
            _github_get("https://api.github.com/repos/test/test/releases")


# ---------------------------------------------------------------------------
# fetch_releases (mocked)
# ---------------------------------------------------------------------------

def _make_release(tag, name, assets, prerelease=False, published="2024-01-01T00:00:00Z"):
    return {
        "tag_name": tag,
        "name": name,
        "prerelease": prerelease,
        "published_at": published,
        "assets": assets,
    }


def _make_asset(name, url=None, size=1024):
    return {
        "name": name,
        "browser_download_url": url or f"https://example.com/{name}",
        "size": size,
    }


class TestFetchReleases:
    @patch("lib.os_installer._github_get")
    def test_stable_release_with_zip(self, mock_get):
        mock_get.return_value = [
            _make_release("v1.0", "Release 1.0", [_make_asset("release.zip")]),
        ]
        profile = {"releases_url": "https://api.github.com/repos/test/test/releases", "asset_filter": None}
        result = fetch_releases(profile)
        assert len(result["stable"]) == 1
        assert len(result["beta"]) == 0
        assert result["stable"][0]["tag_name"] == "v1.0"

    @patch("lib.os_installer._github_get")
    def test_prerelease_goes_to_beta(self, mock_get):
        mock_get.return_value = [
            _make_release("v2.0-beta", "Beta", [_make_asset("release.zip")], prerelease=True),
        ]
        profile = {"releases_url": "...", "asset_filter": None}
        result = fetch_releases(profile)
        assert len(result["stable"]) == 0
        assert len(result["beta"]) == 1

    @patch("lib.os_installer._github_get")
    def test_asset_filter(self, mock_get):
        mock_get.return_value = [
            _make_release("v1.0", "Release", [
                _make_asset("image-device1.img.gz"),
                _make_asset("image-device2.img.gz"),
                _make_asset("checksums.txt"),
            ]),
        ]
        profile = {"releases_url": "...", "asset_filter": r"\.img\.gz$"}
        result = fetch_releases(profile)
        assert len(result["stable"]) == 2

    @patch("lib.os_installer._github_get")
    def test_multipart_archive_detection(self, mock_get):
        mock_get.return_value = [
            _make_release("v1.0", "Release", [
                _make_asset("image.img.7z.001", size=500),
                _make_asset("image.img.7z.002", size=300),
            ]),
        ]
        profile = {"releases_url": "...", "asset_filter": r"\.img\.7z\.001$"}
        result = fetch_releases(profile)
        assert len(result["stable"]) == 1
        entry = result["stable"][0]
        assert "companion_urls" in entry
        assert len(entry["companion_urls"]) == 1
        assert entry["size"] == 800

    @patch("lib.os_installer._github_get")
    def test_no_matching_assets(self, mock_get):
        mock_get.return_value = [
            _make_release("v1.0", "Release", [_make_asset("README.md")]),
        ]
        profile = {"releases_url": "...", "asset_filter": None}
        result = fetch_releases(profile)
        assert len(result["stable"]) == 0

    @patch("lib.os_installer._github_get")
    def test_non_list_response_raises(self, mock_get):
        mock_get.return_value = {"message": "Not Found"}
        profile = {"releases_url": "...", "asset_filter": None}
        with pytest.raises(ValueError, match="expected a JSON array"):
            fetch_releases(profile)

    @pytest.mark.parametrize("profile_key", list(OS_PROFILES.keys()))
    def test_all_profiles_have_valid_asset_filter(self, profile_key):
        """Ensure asset_filter compiles and can be used with re.search."""
        import re
        filt = OS_PROFILES[profile_key]["asset_filter"]
        if filt is not None:
            pattern = re.compile(filt)
            # Just verify it doesn't crash when matching
            pattern.search("test.zip")


# ---------------------------------------------------------------------------
# download_release (mocked)
# ---------------------------------------------------------------------------

class TestDownloadMultipartRelease:
    def test_empty_urls_raises(self, tmp_path):
        with pytest.raises(ValueError, match="No URLs"):
            download_multipart_release([], tmp_path)


class TestDownloadRelease:
    @patch("lib.os_installer.urlopen")
    def test_successful_download(self, mock_urlopen, tmp_path):
        content = b"zip contents"
        mock_response = MagicMock()
        mock_response.read.side_effect = [content, b""]
        mock_response.headers = {"Content-Length": str(len(content))}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = download_release("https://example.com/release.zip", tmp_path)
        assert result.name == "release.zip"
        assert result.read_bytes() == content

    @patch("lib.os_installer.urlopen")
    def test_creates_dest_dir(self, mock_urlopen, tmp_path):
        content = b"data"
        mock_response = MagicMock()
        mock_response.read.side_effect = [content, b""]
        mock_response.headers = {"Content-Length": str(len(content))}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        dest = tmp_path / "subdir" / "downloads"
        result = download_release("https://example.com/file.zip", dest)
        assert dest.is_dir()

    @patch("lib.os_installer.urlopen")
    def test_http_error(self, mock_urlopen, tmp_path):
        mock_urlopen.side_effect = HTTPError(
            url="http://example.com", code=404, msg="Not Found",
            hdrs=None, fp=None,
        )
        with pytest.raises(ConnectionError, match="404"):
            download_release("https://example.com/missing.zip", tmp_path)

    @patch("lib.os_installer.urlopen")
    def test_partial_download_cleaned_up_on_error(self, mock_urlopen, tmp_path):
        """Partial file should be deleted when download fails."""
        mock_urlopen.side_effect = URLError("Connection reset")
        with pytest.raises(ConnectionError):
            download_release("https://example.com/partial.zip", tmp_path)
        assert not (tmp_path / "partial.zip").exists()

    @patch("lib.os_installer.urlopen")
    def test_progress_callback(self, mock_urlopen, tmp_path):
        content = b"data"
        mock_response = MagicMock()
        mock_response.read.side_effect = [content, b""]
        mock_response.headers = {"Content-Length": str(len(content))}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        cb = MagicMock()
        download_release("https://example.com/file.zip", tmp_path, progress_callback=cb)
        cb.assert_called()


# ---------------------------------------------------------------------------
# get_downloaded_releases
# ---------------------------------------------------------------------------

class TestGetDownloadedReleases:
    def test_empty_directory(self, tmp_path):
        result = get_downloaded_releases(tmp_path)
        assert result == []

    def test_nonexistent_directory(self, tmp_path):
        result = get_downloaded_releases(tmp_path / "nope")
        assert result == []

    def test_finds_zip_files(self, tmp_path):
        (tmp_path / "release.zip").write_bytes(b"data")
        result = get_downloaded_releases(tmp_path)
        assert len(result) == 1
        assert result[0]["filename"] == "release.zip"

    def test_finds_image_archives(self, tmp_path):
        (tmp_path / "image.img.gz").write_bytes(b"data")
        (tmp_path / "image.img.xz").write_bytes(b"data")
        result = get_downloaded_releases(tmp_path)
        assert len(result) == 2

    def test_ignores_non_archive_files(self, tmp_path):
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / "notes.txt").write_text("hello")
        result = get_downloaded_releases(tmp_path)
        assert result == []

    def test_sorted_by_modified_desc(self, tmp_path):
        import time
        (tmp_path / "old.zip").write_bytes(b"old")
        time.sleep(0.05)
        (tmp_path / "new.zip").write_bytes(b"new")
        result = get_downloaded_releases(tmp_path)
        assert result[0]["filename"] == "new.zip"


# ---------------------------------------------------------------------------
# get_required_space
# ---------------------------------------------------------------------------

class TestGetRequiredSpace:
    def test_correct_uncompressed_size(self, tmp_path):
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file1.txt", "a" * 100)
            zf.writestr("file2.txt", "b" * 200)
        size = get_required_space(zip_path)
        assert size == 300

    def test_empty_zip(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass
        assert get_required_space(zip_path) == 0


# ---------------------------------------------------------------------------
# extract_to_sd
# ---------------------------------------------------------------------------

class TestExtractToSd:
    def test_basic_extraction(self, tmp_path):
        zip_path = tmp_path / "release.zip"
        sd_mount = tmp_path / "sd"
        sd_mount.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("System/config.txt", "key=value")
            zf.writestr("Roms/.gitkeep", "")

        ok, msg = extract_to_sd(zip_path, sd_mount)
        assert ok is True
        assert (sd_mount / "System" / "config.txt").is_file()
        assert (sd_mount / "Roms" / ".gitkeep").is_file()

    def test_missing_zip_file(self, tmp_path):
        sd_mount = tmp_path / "sd"
        sd_mount.mkdir()
        ok, msg = extract_to_sd(tmp_path / "missing.zip", sd_mount)
        assert ok is False
        assert "not found" in msg.lower()

    def test_missing_mount_point(self, tmp_path):
        zip_path = tmp_path / "release.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "data")
        ok, msg = extract_to_sd(zip_path, tmp_path / "nonexistent")
        assert ok is False

    def test_progress_callback(self, tmp_path):
        zip_path = tmp_path / "release.zip"
        sd_mount = tmp_path / "sd"
        sd_mount.mkdir()
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "aaa")
            zf.writestr("b.txt", "bbb")

        cb = MagicMock()
        extract_to_sd(zip_path, sd_mount, progress_callback=cb)
        assert cb.call_count >= 2

    def test_path_traversal_protection(self, tmp_path):
        """Ensure extraction skips files that would escape the mount point."""
        zip_path = tmp_path / "evil.zip"
        sd_mount = tmp_path / "sd"
        sd_mount.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("normal.txt", "safe")
            # Manually create an entry with path traversal
            info = zipfile.ZipInfo("../../../etc/passwd")
            zf.writestr(info, "evil")

        ok, msg = extract_to_sd(zip_path, sd_mount)
        assert ok is True
        assert (sd_mount / "normal.txt").is_file()
        # The traversal file should NOT be extracted outside the mount
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_invalid_zip(self, tmp_path):
        zip_path = tmp_path / "bad.zip"
        zip_path.write_bytes(b"not a zip file")
        sd_mount = tmp_path / "sd"
        sd_mount.mkdir()
        ok, msg = extract_to_sd(zip_path, sd_mount)
        assert ok is False
        assert "invalid" in msg.lower() or "bad" in msg.lower()


# ---------------------------------------------------------------------------
# verify_extraction
# ---------------------------------------------------------------------------

class TestVerifyExtraction:
    def test_all_dirs_present(self, tmp_path):
        (tmp_path / "System").mkdir()
        (tmp_path / "Emus").mkdir()
        profile = {"expected_dirs": ["System", "Emus"]}
        ok, missing = verify_extraction(tmp_path, profile)
        assert ok is True
        assert missing == []

    def test_missing_dirs(self, tmp_path):
        (tmp_path / "System").mkdir()
        profile = {"expected_dirs": ["System", "Emus", "Apps"]}
        ok, missing = verify_extraction(tmp_path, profile)
        assert ok is False
        assert set(missing) == {"Emus", "Apps"}

    def test_empty_expected_dirs(self, tmp_path):
        profile = {"expected_dirs": []}
        ok, missing = verify_extraction(tmp_path, profile)
        assert ok is True

    @pytest.mark.parametrize("profile_key", list(OS_PROFILES.keys()))
    def test_verify_extraction_doesnt_crash_on_any_profile(self, tmp_path, profile_key):
        """verify_extraction should handle every profile without crashing."""
        ok, missing = verify_extraction(tmp_path, OS_PROFILES[profile_key])
        # For raw_image profiles, expected_dirs is empty so it should succeed
        if OS_PROFILES[profile_key]["install_method"] == "raw_image":
            assert ok is True


# ---------------------------------------------------------------------------
# decompress_image
# ---------------------------------------------------------------------------

class TestDecompressImage:
    def test_unsupported_format(self, tmp_path):
        bad_file = tmp_path / "archive.tar.bz2"
        bad_file.write_bytes(b"data")
        with pytest.raises(ValueError, match="Unsupported"):
            decompress_image(bad_file, tmp_path)

    def test_gz_decompression(self, tmp_path):
        import gzip
        compressed = tmp_path / "image.img.gz"
        img_data = b"\x00" * 1024
        with gzip.open(compressed, 'wb') as f:
            f.write(img_data)

        result = decompress_image(compressed, tmp_path)
        assert result == tmp_path / "image.img"
        assert result.read_bytes() == img_data

    def test_xz_decompression(self, tmp_path):
        import lzma
        compressed = tmp_path / "image.img.xz"
        img_data = b"\x00" * 1024
        with lzma.open(compressed, 'wb') as f:
            f.write(img_data)

        result = decompress_image(compressed, tmp_path)
        assert result == tmp_path / "image.img"
        assert result.read_bytes() == img_data

    @patch("subprocess.run")
    def test_7z_decompression(self, mock_run, tmp_path):
        compressed = tmp_path / "image.img.7z.001"
        compressed.write_bytes(b"fake 7z")
        # Simulate 7z extracting an .img file
        mock_run.return_value = MagicMock(returncode=0)
        # Create the expected output file
        (tmp_path / "image.img").write_bytes(b"extracted img")

        result = decompress_image(compressed, tmp_path)
        assert result == tmp_path / "image.img"

    def test_gz_failure(self, tmp_path):
        compressed = tmp_path / "image.img.gz"
        compressed.write_bytes(b"not a real gzip file")
        with pytest.raises(Exception):
            decompress_image(compressed, tmp_path)
