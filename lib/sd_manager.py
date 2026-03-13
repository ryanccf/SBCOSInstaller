"""
sd_manager.py - SD card operations for OS installer.

Manages SD card detection, mounting, formatting, and validation
for supported retro handheld devices using native Linux tools.
"""

import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _require_linux(operation: str = "This operation"):
    """Raise OSError if running on Windows."""
    if IS_WINDOWS:
        raise OSError(
            f"{operation} requires Linux.\n"
            f"On Windows, use a tool like Rufus or balenaEtcher for SD card operations."
        )

_SAFE_DEVICE_RE = re.compile(r"^/dev/[a-zA-Z0-9_]+$")
_SAFE_LABEL_RE = re.compile(r"^[A-Z0-9_ ]{0,11}$")


def _validate_device(device: str) -> None:
    """Raise ValueError if device path looks unsafe for shell interpolation."""
    if not _SAFE_DEVICE_RE.match(device):
        raise ValueError(f"Invalid device path: {device!r}")


def _validate_label(label: str) -> None:
    """Raise ValueError if label contains shell-unsafe characters."""
    if not _SAFE_LABEL_RE.match(label):
        raise ValueError(f"Invalid volume label: {label!r}")

_TOOL_PATHS = {
    "parted": "/sbin/parted",
    "mkfs.vfat": "/sbin/mkfs.vfat",
    "fsck.vfat": "/sbin/fsck.vfat",
    "partprobe": "/sbin/partprobe",
}


def _tool(name: str) -> str:
    path = _TOOL_PATHS.get(name, name)
    if os.path.isfile(path):
        return path
    return shutil.which(name) or name


def _is_root() -> bool:
    if IS_WINDOWS:
        return False
    return os.geteuid() == 0


def _run(cmd: list[str], *, check: bool = False,
         timeout: int = 120) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        logger.debug("stderr: %s", result.stderr.strip())
    if check:
        result.check_returncode()
    return result


def _privileged_run(cmd: list[str], *, check: bool = False,
                    timeout: int = 120) -> subprocess.CompletedProcess:
    if _is_root():
        return _run(cmd, check=check, timeout=timeout)
    return _run(["pkexec"] + cmd, check=check, timeout=timeout)


def _device_basename(device: str) -> str:
    return os.path.basename(device)


def _ensure_block_device(device: str) -> str:
    if not device.startswith("/dev/"):
        device = f"/dev/{device}"
    return device


def _card_size_bytes(device: str) -> int:
    name = _device_basename(device)
    try:
        with open(f"/sys/block/{name}/size") as fh:
            sectors = int(fh.read().strip())
        return sectors * 512
    except (FileNotFoundError, ValueError, OSError):
        return 0


def list_removable_drives() -> list[dict]:
    """Enumerate removable drives visible to the system."""
    if IS_WINDOWS:
        logger.info("SD card detection is not supported on Windows")
        return []

    result = _run([
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,RM,MODEL,TRAN,LABEL",
    ])
    if result.returncode != 0:
        logger.error("lsblk failed: %s", result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse lsblk JSON output")
        return []

    drives: list[dict] = []
    for dev in data.get("blockdevices", []):
        rm = dev.get("rm")
        if isinstance(rm, str):
            rm = rm.strip() == "1"
        elif isinstance(rm, (int, float)):
            rm = bool(rm)
        else:
            rm = False

        if not rm:
            continue
        if dev.get("type") != "disk":
            continue

        drive_info = {
            "name": dev.get("name", ""),
            "device": f"/dev/{dev.get('name', '')}",
            "size": dev.get("size", ""),
            "type": dev.get("type", ""),
            "mountpoint": dev.get("mountpoint"),
            "fstype": dev.get("fstype"),
            "rm": True,
            "model": (dev.get("model") or "").strip(),
            "tran": dev.get("tran"),
            "label": dev.get("label"),
            "children": dev.get("children", []),
        }
        drives.append(drive_info)

    return drives


def get_drive_partitions(device: str) -> list[dict]:
    """Return a list of partitions for a device."""
    if IS_WINDOWS:
        return []
    device = _ensure_block_device(device)

    result = _run([
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL",
        device,
    ])
    if result.returncode != 0:
        logger.error("lsblk failed for %s: %s", device, result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse lsblk JSON output for %s", device)
        return []

    partitions: list[dict] = []
    for dev in data.get("blockdevices", []):
        for child in dev.get("children", []):
            if child.get("type") == "part":
                partitions.append({
                    "name": child.get("name", ""),
                    "device": f"/dev/{child.get('name', '')}",
                    "size": child.get("size", ""),
                    "mountpoint": child.get("mountpoint"),
                    "fstype": child.get("fstype"),
                    "label": child.get("label"),
                })
    return partitions


def detect_sd_state(mount_point: str) -> str:
    """Determine what is currently on the SD card.

    Checks detect_markers from all OS profiles, then falls back to
    stock/empty/unknown. Returns a profile key (e.g. "onion", "crossmix",
    "minui", "koriki") or "stock", "empty", "unknown".
    """
    from lib.os_profiles import OS_PROFILES

    if not os.path.isdir(mount_point):
        return "unknown"

    try:
        entries = os.listdir(mount_point)
    except OSError:
        return "unknown"

    meaningful = [
        e for e in entries
        if e not in {"System Volume Information", ".Trash-1000",
                     "$RECYCLE.BIN", ".fseventsd", ".Spotlight-V100"}
    ]

    if not meaningful:
        return "empty"

    entries_set = set(entries)

    # Check each profile's detect_markers (most specific first)
    for key, profile in OS_PROFILES.items():
        markers = profile.get("detect_markers", [])
        if markers and all(m in entries_set for m in markers):
            return key

    if "miyoo" in entries_set:
        return "stock"

    return "unknown"


def get_os_version(mount_point: str, profile: dict) -> str | None:
    """Read the installed OS version from the SD card using the profile's version paths."""
    for rel_path in profile.get("version_paths", []):
        version_file = os.path.join(mount_point, rel_path)
        try:
            with open(version_file) as fh:
                return fh.read().strip()
        except (FileNotFoundError, OSError):
            continue
    return None


def _partition_device_for(device: str) -> str:
    base = _device_basename(device)
    if base[-1].isdigit():
        return f"{device}p1"
    return f"{device}1"


def format_sd_card(device: str, label: str = "SDCARD",
                   cluster_sectors_fn=None) -> tuple[bool, str]:
    """Format device as FAT32 with an MBR partition table.

    Parameters
    ----------
    label : str
        Volume label (max 11 ASCII characters).
    cluster_sectors_fn : callable, optional
        Function that takes size_bytes and returns cluster sectors string.
        Defaults to "64" if not provided.
    """
    _require_linux("Formatting SD cards")
    device = _ensure_block_device(device)
    _validate_device(device)
    label = (label or "SDCARD")[:11].upper()
    _validate_label(label)

    partition_device = _partition_device_for(device)

    size_bytes = _card_size_bytes(device)
    if cluster_sectors_fn:
        cluster_sectors = cluster_sectors_fn(size_bytes)
    else:
        cluster_sectors = "64"

    # Unmount via udisksctl first
    partitions = get_drive_partitions(device)
    for part in partitions:
        if part.get("mountpoint"):
            _run(["udisksctl", "unmount", "-b", part["device"]])

    script = f"""#!/bin/sh
set -e

for p in {device}*; do
    umount "$p" 2>/dev/null || true
done

{_tool("parted")} -s {device} mklabel msdos
{_tool("parted")} -s -a optimal {device} mkpart primary fat32 1MiB 100%
{_tool("partprobe")} {device}
udevadm settle --timeout=5
sleep 1

{_tool("mkfs.vfat")} -F32 -s {cluster_sectors} -n {label} {partition_device}

udevadm settle --timeout=5
"""

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        os.chmod(script_path, 0o755)
        res = _privileged_run([script_path], timeout=300)
        if res.returncode != 0:
            error = res.stderr.strip() or res.stdout.strip()
            return False, f"Format failed: {error}"
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    return True, f"Successfully formatted {device} as FAT32 (label={label})"


def check_disk(partition: str) -> str:
    """Run a non-destructive filesystem check on a partition."""
    _require_linux("Disk checking")
    partition = _ensure_block_device(partition)

    # Unmount if currently mounted
    info = _run(["lsblk", "-n", "-o", "MOUNTPOINT", partition])
    if info.stdout.strip():
        _run(["udisksctl", "unmount", "-b", partition])

    res = _privileged_run([_tool("fsck.vfat"), "-n", partition], timeout=300)
    output = (res.stdout + "\n" + res.stderr).strip()
    return output


def eject_drive(device: str) -> tuple[bool, str]:
    """Safely eject a device (unmount + power-off)."""
    _require_linux("Ejecting drives")
    device = _ensure_block_device(device)

    partitions = get_drive_partitions(device)
    for part in partitions:
        if part.get("mountpoint"):
            res = _run(["udisksctl", "unmount", "-b", part["device"]])
            if res.returncode != 0:
                res = _privileged_run(["umount", part["device"]])
                if res.returncode != 0:
                    return False, f"Failed to unmount {part['device']}: {res.stderr.strip()}"

    res = _run(["udisksctl", "power-off", "-b", device])
    if res.returncode == 0:
        return True, f"Drive {device} has been safely ejected."

    if shutil.which("eject"):
        res = _privileged_run(["eject", device])
        if res.returncode == 0:
            return True, f"Drive {device} has been ejected (via eject)."
        return False, f"Failed to eject {device}: {res.stderr.strip()}"

    return False, f"Failed to power-off {device}: {res.stderr.strip()}"


def mount_partition(partition: str) -> str | None:
    """Mount a partition via udisksctl and return the mount point."""
    _require_linux("Mounting partitions")
    partition = _ensure_block_device(partition)

    res = _run(["udisksctl", "mount", "-b", partition])
    if res.returncode != 0:
        logger.error("mount failed for %s: %s", partition, res.stderr.strip())
        return None

    stdout = res.stdout.strip()
    if " at " in stdout:
        mount_point = stdout.split(" at ", 1)[1].rstrip(".")
        return mount_point

    info = _run(["lsblk", "-n", "-o", "MOUNTPOINT", partition])
    mp = info.stdout.strip()
    return mp if mp else None


def unmount_partition(partition: str) -> tuple[bool, str]:
    """Unmount a partition."""
    _require_linux("Unmounting partitions")
    partition = _ensure_block_device(partition)

    res = _run(["udisksctl", "unmount", "-b", partition])
    if res.returncode == 0:
        return True, f"Unmounted {partition}."

    res = _privileged_run(["umount", partition])
    if res.returncode == 0:
        return True, f"Unmounted {partition} (via umount)."

    return False, f"Failed to unmount {partition}: {res.stderr.strip()}"


def get_free_space(path: str) -> int:
    """Return the free space in bytes available at a path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free
    except OSError:
        return 0


def unmount_all_partitions(device: str) -> tuple[bool, str]:
    """Unmount all mounted partitions on a device."""
    _require_linux("Unmounting partitions")
    device = _ensure_block_device(device)
    partitions = get_drive_partitions(_device_basename(device))
    failed = []
    for part in partitions:
        if part.get("mountpoint"):
            res = _run(["udisksctl", "unmount", "-b", part["device"]])
            if res.returncode != 0:
                res = _privileged_run(["umount", part["device"]])
                if res.returncode != 0:
                    failed.append(part["device"])
    if failed:
        return False, f"Failed to unmount: {', '.join(failed)}"
    return True, "All partitions unmounted."


def write_image_to_device(
    img_path: str,
    device: str,
    timeout: int = 3600,
) -> tuple[bool, str]:
    """Write a raw .img file to a block device using dd.

    Unmounts all partitions first, then writes with dd via pkexec.
    Returns (success, message).
    """
    _require_linux("Writing disk images")
    device = _ensure_block_device(device)
    _validate_device(device)

    if not os.path.isfile(img_path):
        return False, f"Image file not found: {img_path}"

    import tempfile
    script = f"""#!/bin/sh
set -e

# Unmount all partitions
for p in {device}*; do
    umount "$p" 2>/dev/null || true
done

# Write image
dd if={shlex.quote(img_path)} of={device} bs=4M conv=fsync status=progress 2>&1

sync
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        os.chmod(script_path, 0o755)
        res = _privileged_run([script_path], timeout=timeout)
        if res.returncode != 0:
            error = res.stderr.strip() or res.stdout.strip()
            return False, f"Image write failed: {error}"
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    return True, f"Image written successfully to {device}."
