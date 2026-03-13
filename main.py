#!/usr/bin/env python3
"""
OS Installer - Install retro handheld OS images to SD cards.
Supports multiple OSes and devices via pluggable profiles.
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango

import os
import platform
import sys
import threading
import shutil
import subprocess
from pathlib import Path

from lib.os_profiles import OS_PROFILES
from lib.sd_manager import (
    list_removable_drives, detect_sd_state, get_os_version,
    format_sd_card, check_disk, eject_drive, mount_partition,
    unmount_partition, get_free_space, get_drive_partitions,
    unmount_all_partitions, write_image_to_device,
)
from lib.os_installer import (
    fetch_releases, download_release, download_multipart_release,
    extract_to_sd, decompress_image,
    verify_extraction, get_required_space, get_downloaded_releases
)
from lib.bios_manager import (
    download_all_bios, install_bios_to_sd,
    scan_sd_bios, scan_cached_bios,
)

APP_NAME = "SBCOSInstaller"
APP_VERSION = "0.2.0"
APP_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = APP_DIR / "downloads"
BIOS_CACHE_DIR = APP_DIR / "bios_cache"

DOWNLOADS_DIR.mkdir(exist_ok=True)
BIOS_CACHE_DIR.mkdir(exist_ok=True)

# PyInstaller puts bundled data in sys._MEIPASS; fall back to APP_DIR for dev.
RESOURCES_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR)) / "resources"


class DriveSelector(Gtk.Dialog):
    """Dialog to select a removable drive."""

    def __init__(self, parent):
        super().__init__(
            title="Select SD Card",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(450, 300)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        self.selected_drive = None

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(15)
        content.set_margin_end(15)
        content.set_margin_top(15)
        content.set_margin_bottom(15)

        label = Gtk.Label(label="Select the SD card to use:")
        label.set_halign(Gtk.Align.START)
        content.pack_start(label, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(200)
        content.pack_start(scrolled, True, True, 0)

        self.radio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolled.add(self.radio_box)

        self._populate_drives()
        self.show_all()

    def _populate_drives(self):
        drives = list_removable_drives()
        if not drives:
            label = Gtk.Label(label="No removable drives detected.\nInsert an SD card and try again.")
            label.set_halign(Gtk.Align.START)
            self.radio_box.pack_start(label, False, False, 0)
            return

        first_radio = None
        for drive in drives:
            text = f"/dev/{drive['name']} - {drive['size']} - {drive.get('model', 'Unknown')}"
            if drive.get('label'):
                text += f" [{drive['label']}]"
            if first_radio is None:
                radio = Gtk.RadioButton.new_with_label(None, text)
                first_radio = radio
            else:
                radio = Gtk.RadioButton.new_with_label_from_widget(first_radio, text)
            radio.drive_info = drive
            radio.connect("toggled", self._on_radio_toggled)
            self.radio_box.pack_start(radio, False, False, 0)

        if first_radio:
            first_radio.set_active(True)
            self.selected_drive = first_radio.drive_info

    def _on_radio_toggled(self, button):
        if button.get_active():
            self.selected_drive = button.drive_info


class ProgressDialog(Gtk.Dialog):
    """Progress dialog with a progress bar and status label."""

    def __init__(self, parent, title="Working..."):
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(400, 120)
        self.set_deletable(False)

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_top(20)
        content.set_margin_bottom(20)

        self.status_label = Gtk.Label(label="Starting...")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        content.pack_start(self.status_label, False, False, 0)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        content.pack_start(self.progress_bar, False, False, 0)

        self.show_all()

    def set_progress(self, fraction, text=None):
        GLib.idle_add(self._update_progress, fraction, text)

    def _update_progress(self, fraction, text):
        self.progress_bar.set_fraction(min(fraction, 1.0))
        if text:
            self.status_label.set_text(text)
        return False


class OSInstaller(Gtk.Window):
    """Main application window."""

    def __init__(self):
        super().__init__(title=f"{APP_NAME} v{APP_VERSION}")
        self.set_default_size(550, 420)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", Gtk.main_quit)

        icon_path = RESOURCES_DIR / "icon_48.png"
        if icon_path.exists():
            self.set_icon_from_file(str(icon_path))

        # Current OS profile
        self.profile_key = "crossmix"
        self.profile = OS_PROFILES[self.profile_key]

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(main_box)

        # OS Selector bar at top
        os_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        os_bar.set_margin_start(10)
        os_bar.set_margin_end(10)
        os_bar.set_margin_top(8)
        os_bar.set_margin_bottom(4)
        main_box.pack_start(os_bar, False, False, 0)

        os_label = Gtk.Label()
        os_label.set_markup("<b>Target OS:</b>")
        os_bar.pack_start(os_label, False, False, 0)

        self.os_combo = Gtk.ComboBoxText()
        for key, prof in OS_PROFILES.items():
            self.os_combo.append(key, prof['name'])
        self.os_combo.set_active_id(self.profile_key)
        self.os_combo.connect("changed", self._on_os_changed)
        os_bar.pack_start(self.os_combo, False, False, 0)

        self.device_label = Gtk.Label()
        self.device_label.set_markup(f"  <i>{self.profile['device']}</i>")
        os_bar.pack_start(self.device_label, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_start(5)
        sep.set_margin_end(5)
        main_box.pack_start(sep, False, False, 0)

        # Notebook
        self.notebook = Gtk.Notebook()
        self.notebook.set_margin_start(5)
        self.notebook.set_margin_end(5)
        self.notebook.set_margin_top(5)
        main_box.pack_start(self.notebook, True, True, 0)

        self._build_install_tab()
        self._build_bios_tab()
        self._build_sdtools_tab()
        self._build_about_tab()

        # Bottom bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom_bar.set_margin_start(10)
        bottom_bar.set_margin_end(10)
        bottom_bar.set_margin_top(5)
        bottom_bar.set_margin_bottom(10)
        main_box.pack_end(bottom_bar, False, False, 0)

        self.ok_button = Gtk.Button(label="OK")
        self.ok_button.set_size_request(90, 32)
        self.ok_button.connect("clicked", self._on_ok_clicked)
        bottom_bar.pack_end(self.ok_button, False, False, 0)

        eject_button = Gtk.Button(label="Eject SD")
        eject_button.set_size_request(90, 32)
        eject_button.connect("clicked", self._on_eject_clicked)
        bottom_bar.pack_end(eject_button, False, False, 0)

        self.notebook.connect("switch-page", self._on_tab_changed)

    def _on_os_changed(self, combo):
        self.profile_key = combo.get_active_id()
        self.profile = OS_PROFILES[self.profile_key]
        self.device_label.set_markup(f"  <i>{self.profile['device']}</i>")
        self._update_install_labels()
        self._update_bios_status()
        self._update_about_page()

    # -- Tab 1: Install or Update --

    def _build_install_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        box.set_margin_bottom(15)

        self.install_frame = Gtk.Frame()
        self.install_frame.set_margin_bottom(10)
        box.pack_start(self.install_frame, True, True, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(15)
        inner.set_margin_bottom(15)
        self.install_frame.add(inner)

        self.install_desc = Gtk.Label()
        self.install_desc.set_halign(Gtk.Align.START)
        self.install_desc.set_line_wrap(True)
        inner.pack_start(self.install_desc, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        inner.pack_start(sep, False, False, 5)

        self.install_radios = []

        self.r1 = Gtk.RadioButton.new_with_label(None, "")
        self.r1.action = "install_no_format"
        self.install_radios.append(self.r1)
        inner.pack_start(self.r1, False, False, 0)

        self.r2 = Gtk.RadioButton.new_with_label_from_widget(self.r1, "")
        self.r2.action = "format_and_install"
        self.install_radios.append(self.r2)
        inner.pack_start(self.r2, False, False, 0)

        self._update_install_labels()
        self.notebook.append_page(box, Gtk.Label(label="Install / Update"))

    def _update_install_labels(self):
        name = self.profile["name"]
        device = self.profile["device"]
        is_raw = self.profile.get("install_method") == "raw_image"

        if is_raw:
            self.install_frame.set_label(f"Flash {name}")
            self.install_desc.set_text(
                f"Flash {name} disk image to an SD card for {device}.\n"
                f"WARNING: This will erase ALL data on the selected SD card."
            )
            self.r1.set_label(f"Flash {name} to SD card")
            self.r1.set_active(True)
            self.r2.set_visible(False)
        else:
            self.install_frame.set_label(f"Install or Update {name}")
            if self.profile_key == "minui":
                self.install_desc.set_text(
                    f"Install {name} on an SD card for {device}.\n"
                    f"Install the BASE zip first (required), then optionally "
                    f"install EXTRAS on top for more emulators and tools."
                )
            else:
                self.install_desc.set_text(
                    f"Install {name} on an SD card for {device}.\n"
                    f"Download the latest release from GitHub and extract to SD."
                )
            self.r1.set_label(f"Install / Upgrade / Reinstall {name} (without formatting)")
            self.r2.set_label(f"Format SD card and install {name}")
            self.r2.set_visible(True)

    # -- Tab 2: BIOS Manager --

    def _build_bios_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        box.set_margin_bottom(15)

        frame = Gtk.Frame(label="BIOS Manager")
        frame.set_margin_bottom(10)
        box.pack_start(frame, True, True, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(15)
        inner.set_margin_bottom(15)
        frame.add(inner)

        desc = Gtk.Label(
            label="Download and install BIOS files required by emulators.\n"
                  "Files are cached locally and can be installed to any SD card."
        )
        desc.set_halign(Gtk.Align.START)
        desc.set_line_wrap(True)
        inner.pack_start(desc, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        inner.pack_start(sep, False, False, 5)

        self.bios_status_label = Gtk.Label(label="Scanning...")
        self.bios_status_label.set_halign(Gtk.Align.START)
        inner.pack_start(self.bios_status_label, False, False, 0)

        self.bios_required_only = Gtk.CheckButton(label="Required BIOS files only")
        self.bios_required_only.set_active(False)
        inner.pack_start(self.bios_required_only, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_margin_top(10)
        inner.pack_start(btn_box, False, False, 0)

        dl_btn = Gtk.Button(label="Download All to Cache")
        dl_btn.connect("clicked", self._on_bios_download)
        btn_box.pack_start(dl_btn, False, False, 0)

        inst_btn = Gtk.Button(label="Install to SD Card")
        inst_btn.connect("clicked", self._on_bios_install)
        btn_box.pack_start(inst_btn, False, False, 0)

        self.notebook.append_page(box, Gtk.Label(label="BIOS Manager"))
        GLib.idle_add(self._update_bios_status)

    def _update_bios_status(self):
        bios_files = self.profile["bios_files"]
        cached = scan_cached_bios(BIOS_CACHE_DIR, bios_files)
        total_files = len(bios_files)
        cached_count = sum(1 for v in cached.values() if v)
        required_files = [e for e in bios_files if e["required"]]
        required_total = len(required_files)
        required_cached = sum(1 for e in required_files if cached.get(e["filename"], False))
        self.bios_status_label.set_text(
            f"[{self.profile['name']}] Cached: {cached_count}/{total_files} files "
            f"({required_cached}/{required_total} required)"
        )
        return False

    def _on_bios_download(self, button):
        bios_files = self.profile["bios_files"]
        required_only = self.bios_required_only.get_active()
        os_name = self.profile["name"]
        progress = ProgressDialog(self, f"Downloading BIOS Files ({os_name})")

        def worker():
            try:
                def cb(fraction, text):
                    GLib.idle_add(progress.set_progress, fraction, text)

                ok, succeeded, failed = download_all_bios(
                    BIOS_CACHE_DIR, bios_files,
                    progress_cb=cb, skip_cached=True, required_only=required_only,
                )

                GLib.idle_add(progress.set_progress, 1.0, "Done!")
                GLib.idle_add(self._update_bios_status)

                if ok:
                    GLib.idle_add(
                        self._show_success_and_close_progress, progress,
                        f"Downloaded {len(succeeded)} BIOS files successfully."
                    )
                else:
                    summary = f"Downloaded {len(succeeded)} files.\n\nFailed ({len(failed)}):\n"
                    summary += "\n".join(failed[:10])
                    GLib.idle_add(
                        self._show_error_and_close_progress, progress, summary
                    )
            except Exception as e:
                GLib.idle_add(self._show_error_and_close_progress, progress, str(e))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_bios_install(self, button):
        bios_files = self.profile["bios_files"]
        cached = scan_cached_bios(BIOS_CACHE_DIR, bios_files)
        if not any(cached.values()):
            self._show_message(
                "No BIOS Files",
                "No BIOS files found in cache.\nDownload them first using 'Download All to Cache'.",
                Gtk.MessageType.WARNING,
            )
            return

        device, mount_point = self._select_drive_for_bios()
        if not device:
            return
        if not mount_point:
            label_hint = self.profile.get("bios_partition_label", "")
            hint = f"\nLook for a partition labeled '{label_hint}'." if label_hint else ""
            self._show_message(
                "Error",
                f"Could not mount the BIOS partition.{hint}",
                Gtk.MessageType.ERROR,
            )
            return

        required_only = self.bios_required_only.get_active()
        os_name = self.profile["name"]
        progress = ProgressDialog(self, f"Installing BIOS Files ({os_name})")

        def worker():
            try:
                def cb(fraction, text):
                    GLib.idle_add(progress.set_progress, fraction, text)

                bios_dir_name = self.profile.get("bios_dir", "BIOS")
                ok, succeeded, failed = install_bios_to_sd(
                    BIOS_CACHE_DIR, Path(mount_point), bios_files,
                    progress_cb=cb, required_only=required_only,
                    bios_dir=bios_dir_name,
                )

                GLib.idle_add(progress.set_progress, 1.0, "Done!")

                if ok:
                    GLib.idle_add(
                        self._show_success_and_close_progress, progress,
                        f"Installed {len(succeeded)} BIOS files to SD card."
                    )
                else:
                    summary = f"Installed {len(succeeded)} files.\n\nFailed ({len(failed)}):\n"
                    summary += "\n".join(failed[:10])
                    GLib.idle_add(
                        self._show_error_and_close_progress, progress, summary
                    )
            except Exception as e:
                GLib.idle_add(self._show_error_and_close_progress, progress, str(e))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # -- Tab 3: SD Card Tools --

    def _build_sdtools_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        box.set_margin_bottom(15)

        frame = Gtk.Frame(label="SD Card Tools")
        frame.set_margin_bottom(10)
        box.pack_start(frame, True, True, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(15)
        inner.set_margin_bottom(15)
        frame.add(inner)

        self.sdtools_radios = []

        r1 = Gtk.RadioButton.new_with_label(None, "Format SD card in FAT32")
        r1.action = "format_fat32"
        self.sdtools_radios.append(r1)
        inner.pack_start(r1, False, False, 0)

        r2 = Gtk.RadioButton.new_with_label_from_widget(r1, "Check for errors (fsck)")
        r2.action = "check_disk"
        self.sdtools_radios.append(r2)
        inner.pack_start(r2, False, False, 0)

        self.notebook.append_page(box, Gtk.Label(label="SD Card Tools"))

    # -- Tab 4: About --

    def _build_about_tab(self):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        box.set_margin_bottom(15)
        scrolled.add(box)

        # App logo
        logo_path = RESOURCES_DIR / "icon_128.png"
        if logo_path.exists():
            logo = Gtk.Image.new_from_file(str(logo_path))
            logo.set_margin_bottom(10)
            box.pack_start(logo, False, False, 0)

        # OS name and description
        self.about_os_title = Gtk.Label()
        self.about_os_title.set_halign(Gtk.Align.START)
        box.pack_start(self.about_os_title, False, False, 0)

        self.about_os_desc = Gtk.Label()
        self.about_os_desc.set_halign(Gtk.Align.START)
        self.about_os_desc.set_line_wrap(True)
        self.about_os_desc.set_max_width_chars(65)
        self.about_os_desc.set_margin_top(6)
        box.pack_start(self.about_os_desc, False, False, 0)

        # Compatible devices
        sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep1.set_margin_top(10)
        sep1.set_margin_bottom(6)
        box.pack_start(sep1, False, False, 0)

        devices_header = Gtk.Label()
        devices_header.set_markup("<b>Compatible Devices</b>")
        devices_header.set_halign(Gtk.Align.START)
        box.pack_start(devices_header, False, False, 0)

        self.about_devices = Gtk.Label()
        self.about_devices.set_halign(Gtk.Align.START)
        self.about_devices.set_line_wrap(True)
        self.about_devices.set_max_width_chars(65)
        self.about_devices.set_margin_top(4)
        self.about_devices.set_margin_start(10)
        box.pack_start(self.about_devices, False, False, 0)

        # Installation info
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep2.set_margin_top(10)
        sep2.set_margin_bottom(6)
        box.pack_start(sep2, False, False, 0)

        install_header = Gtk.Label()
        install_header.set_markup("<b>Installation</b>")
        install_header.set_halign(Gtk.Align.START)
        box.pack_start(install_header, False, False, 0)

        self.about_install_method = Gtk.Label()
        self.about_install_method.set_halign(Gtk.Align.START)
        self.about_install_method.set_margin_top(4)
        self.about_install_method.set_margin_start(10)
        box.pack_start(self.about_install_method, False, False, 0)

        self.about_install_notes = Gtk.Label()
        self.about_install_notes.set_halign(Gtk.Align.START)
        self.about_install_notes.set_line_wrap(True)
        self.about_install_notes.set_max_width_chars(65)
        self.about_install_notes.set_margin_top(4)
        self.about_install_notes.set_margin_start(10)
        box.pack_start(self.about_install_notes, False, False, 0)

        # Links
        sep3 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep3.set_margin_top(10)
        sep3.set_margin_bottom(6)
        box.pack_start(sep3, False, False, 0)

        links_header = Gtk.Label()
        links_header.set_markup("<b>Links</b>")
        links_header.set_halign(Gtk.Align.START)
        box.pack_start(links_header, False, False, 0)

        self.about_links = Gtk.Label()
        self.about_links.set_halign(Gtk.Align.START)
        self.about_links.set_line_wrap(True)
        self.about_links.set_margin_top(4)
        self.about_links.set_margin_start(10)
        box.pack_start(self.about_links, False, False, 0)

        # App credit at bottom
        app_label = Gtk.Label()
        app_label.set_markup(
            f"\n<small>{APP_NAME} v{APP_VERSION}</small>"
        )
        app_label.set_halign(Gtk.Align.START)
        app_label.set_margin_top(10)
        box.pack_start(app_label, False, False, 0)

        self._update_about_page()
        self.notebook.append_page(scrolled, Gtk.Label(label="About"))

    def _update_about_page(self):
        p = self.profile

        self.about_os_title.set_markup(f"<big><b>{p['name']}</b></big>")

        desc = p.get("description", "")
        source = p.get("description_source", "")
        if source:
            self.about_os_desc.set_markup(
                f"<i>{GLib.markup_escape_text(desc)}</i>\n"
                f'<small><a href="{GLib.markup_escape_text(source)}">'
                f'\u2014 {GLib.markup_escape_text(source)}</a></small>'
            )
        else:
            self.about_os_desc.set_text(desc)

        devices = p.get("compatible_devices", [])
        device_text = "\n".join(f"\u2022 {d}" for d in devices) if devices else "See project documentation."
        self.about_devices.set_text(device_text)

        method = p.get("install_method", "zip_extract")
        if method == "raw_image":
            method_text = "Method: Raw disk image (flashes entire SD card)"
        else:
            method_text = "Method: ZIP extraction to FAT32 SD card"
        bios_dir = p.get("bios_dir", "BIOS")
        method_text += f"\nBIOS directory: {bios_dir}/"
        bios_label = p.get("bios_partition_label", "")
        if bios_label:
            method_text += f"\nBIOS partition: {bios_label}"
        self.about_install_method.set_text(method_text)

        self.about_install_notes.set_text(p.get("install_notes", ""))

        self.about_links.set_markup(
            f'<a href="{p["project_url"]}">{p["name"]} on GitHub</a>\n'
            f'<a href="{p["wiki_url"]}">{p["name"]} Wiki / Documentation</a>'
        )

    # -- Event Handlers --

    def _on_tab_changed(self, notebook, page, page_num):
        self.ok_button.set_visible(page_num not in (1, 3))

    def _on_ok_clicked(self, button):
        page = self.notebook.get_current_page()
        if page == 0:
            self._handle_install_action()
        elif page == 2:
            self._handle_sdtools_action()

    def _on_eject_clicked(self, button):
        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return

        device = f"/dev/{drive['name']}"
        success, msg = eject_drive(device)
        self._show_message(
            "Eject SD Card", msg,
            Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR
        )

    # -- Install Actions --

    def _get_selected_radio(self, radios):
        for r in radios:
            if r.get_active():
                return r.action
        return None

    def _select_drive(self):
        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return None, None

        device = f"/dev/{drive['name']}"
        partitions = get_drive_partitions(drive['name'])
        if partitions:
            part_dev = f"/dev/{partitions[0]['name']}"
            mount_point = partitions[0].get('mountpoint')
            if not mount_point:
                mount_point = mount_partition(part_dev)
            return device, mount_point
        return device, None

    def _select_drive_for_bios(self):
        """Select a drive and mount the appropriate partition for BIOS install.

        If the profile has bios_partition_label, looks for that partition.
        Otherwise falls back to the first partition.
        """
        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return None, None

        device = f"/dev/{drive['name']}"
        partitions = get_drive_partitions(drive['name'])

        # If profile specifies a partition label for BIOS, find it
        bios_label = self.profile.get("bios_partition_label")
        if bios_label and partitions:
            for part in partitions:
                if part.get("label") == bios_label:
                    mount_point = part.get("mountpoint")
                    if not mount_point:
                        mount_point = mount_partition(part["device"])
                    return device, mount_point

        # Fall back to first partition
        if partitions:
            part_dev = f"/dev/{partitions[0]['name']}"
            mount_point = partitions[0].get('mountpoint')
            if not mount_point:
                mount_point = mount_partition(part_dev)
            return device, mount_point

        return device, None

    def _handle_install_action(self):
        if self.profile.get("install_method") == "raw_image":
            self._do_raw_install()
            return
        action = self._get_selected_radio(self.install_radios)
        if action == "install_no_format":
            self._do_install(format_first=False)
        elif action == "format_and_install":
            self._do_install(format_first=True)

    def _do_install(self, format_first=False):
        profile = self.profile
        os_name = profile["name"]
        device_name = profile["device"]
        sd_label = profile["sd_label"]
        cluster_fn = profile["cluster_sectors"]

        device, mount_point = self._select_drive()
        if not device:
            return

        if format_first:
            confirm = self._confirm(
                "Format SD Card",
                f"This will ERASE ALL DATA on {device}.\n"
                f"Are you sure you want to format and install {os_name}?"
            )
            if not confirm:
                return

        release_dialog = ReleasePicker(self, profile)
        response = release_dialog.run()
        release = release_dialog.selected_release
        release_dialog.destroy()

        if response != Gtk.ResponseType.OK or not release:
            return

        def worker():
            try:
                if format_first:
                    GLib.idle_add(progress.set_progress, 0.05, "Formatting SD card...")
                    success, msg = format_sd_card(device, label=sd_label,
                                                  cluster_sectors_fn=cluster_fn)
                    if not success:
                        GLib.idle_add(self._show_error_and_close_progress, progress,
                                      f"Format failed: {msg}")
                        return
                    import time
                    GLib.idle_add(progress.set_progress, 0.08, "Waiting for drive to settle...")
                    time.sleep(3)
                    nonlocal mount_point
                    mount_point = None
                    dev_name = device.replace('/dev/', '')
                    for attempt in range(5):
                        partitions = get_drive_partitions(dev_name)
                        if partitions:
                            part_dev = f"/dev/{partitions[0]['name']}"
                            mount_point = mount_partition(part_dev)
                            if mount_point:
                                break
                        time.sleep(2)

                if not mount_point:
                    GLib.idle_add(self._show_error_and_close_progress, progress,
                                  "Could not mount SD card.")
                    return

                zip_path = None
                if release.get('local_path'):
                    zip_path = release['local_path']
                else:
                    GLib.idle_add(progress.set_progress, 0.1, f"Downloading {os_name}...")
                    def dl_progress(downloaded, total):
                        if total > 0:
                            frac = 0.1 + 0.5 * (downloaded / total)
                            size_mb = downloaded / (1024 * 1024)
                            total_mb = total / (1024 * 1024)
                            GLib.idle_add(progress.set_progress, frac,
                                          f"Downloading: {size_mb:.1f} / {total_mb:.1f} MB")
                    zip_path = download_release(release['url'], str(DOWNLOADS_DIR), dl_progress)

                GLib.idle_add(progress.set_progress, 0.6, f"Extracting {os_name} to SD card...")
                def ext_progress(current_file, idx, total):
                    frac = 0.6 + 0.35 * (idx / max(total, 1))
                    GLib.idle_add(progress.set_progress, frac, f"Extracting: {current_file}")

                success, msg = extract_to_sd(zip_path, mount_point, ext_progress)
                if not success:
                    GLib.idle_add(self._show_error_and_close_progress, progress,
                                  f"Extract failed: {msg}")
                    return

                GLib.idle_add(progress.set_progress, 0.97, "Verifying installation...")
                success, missing = verify_extraction(mount_point, profile)

                GLib.idle_add(progress.set_progress, 1.0, "Done!")
                if success:
                    GLib.idle_add(self._show_success_and_close_progress, progress,
                                  f"{os_name} installed successfully!\n\n"
                                  f"You can now eject the SD card and insert it into your {device_name}.")
                else:
                    GLib.idle_add(self._show_error_and_close_progress, progress,
                                  f"Installation completed but some directories are missing:\n"
                                  f"{', '.join(missing)}")

            except Exception as e:
                GLib.idle_add(self._show_error_and_close_progress, progress, str(e))

        progress = ProgressDialog(self, f"Installing {os_name}")
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # -- Raw Image Install --

    def _do_raw_install(self):
        profile = self.profile
        os_name = profile["name"]
        device_name = profile["device"]

        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return

        device = f"/dev/{drive['name']}"
        confirm = self._confirm(
            f"Flash {os_name}",
            f"This will ERASE ALL DATA on {device} ({drive['size']}).\n"
            f"A raw disk image will be written to the entire device.\n\n"
            f"Are you sure you want to continue?"
        )
        if not confirm:
            return

        release_dialog = ReleasePicker(self, profile)
        response = release_dialog.run()
        release = release_dialog.selected_release
        release_dialog.destroy()

        if response != Gtk.ResponseType.OK or not release:
            return

        def worker():
            try:
                archive_path = None
                companion_urls = release.get('companion_urls', [])

                if release.get('local_path'):
                    archive_path = Path(release['local_path'])
                elif companion_urls:
                    # Multi-part download
                    all_urls = [release['url']] + companion_urls
                    GLib.idle_add(progress.set_progress, 0.0,
                                  f"Downloading {os_name} ({len(all_urls)} parts)...")

                    def dl_progress(downloaded, total):
                        if total > 0:
                            frac = 0.5 * (downloaded / total)
                            size_mb = downloaded / (1024 * 1024)
                            total_mb = total / (1024 * 1024)
                            GLib.idle_add(progress.set_progress, frac,
                                          f"Downloading: {size_mb:.0f} / {total_mb:.0f} MB")

                    archive_path = download_multipart_release(
                        all_urls, str(DOWNLOADS_DIR), dl_progress)
                else:
                    GLib.idle_add(progress.set_progress, 0.0,
                                  f"Downloading {os_name}...")

                    def dl_progress(downloaded, total):
                        if total > 0:
                            frac = 0.5 * (downloaded / total)
                            size_mb = downloaded / (1024 * 1024)
                            total_mb = total / (1024 * 1024)
                            GLib.idle_add(progress.set_progress, frac,
                                          f"Downloading: {size_mb:.0f} / {total_mb:.0f} MB")

                    archive_path = download_release(
                        release['url'], str(DOWNLOADS_DIR), dl_progress)

                # Decompress
                GLib.idle_add(progress.set_progress, 0.5,
                              "Decompressing image (this may take a while)...")
                img_path = decompress_image(archive_path, DOWNLOADS_DIR)

                # Unmount and write
                GLib.idle_add(progress.set_progress, 0.7,
                              f"Writing image to {device}...")
                unmount_all_partitions(device)
                success, msg = write_image_to_device(str(img_path), device)

                # Clean up decompressed image (keep the archive)
                try:
                    img_path.unlink()
                except OSError:
                    pass

                GLib.idle_add(progress.set_progress, 1.0, "Done!")
                if success:
                    GLib.idle_add(
                        self._show_success_and_close_progress, progress,
                        f"{os_name} flashed successfully to {device}!\n\n"
                        f"Insert the SD card into your {device_name} and boot.\n"
                        f"First boot will take a few minutes to set up.")
                else:
                    GLib.idle_add(
                        self._show_error_and_close_progress, progress, msg)

            except Exception as e:
                GLib.idle_add(self._show_error_and_close_progress, progress, str(e))

        progress = ProgressDialog(self, f"Flashing {os_name}")
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # -- SD Card Tools Actions --

    def _handle_sdtools_action(self):
        action = self._get_selected_radio(self.sdtools_radios)
        if action == "format_fat32":
            self._do_format()
        elif action == "check_disk":
            self._do_check_disk()

    def _do_format(self):
        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return

        device = f"/dev/{drive['name']}"
        sd_label = self.profile["sd_label"]
        cluster_fn = self.profile["cluster_sectors"]
        confirm = self._confirm(
            "Format SD Card",
            f"This will ERASE ALL DATA on {device} ({drive['size']}).\n"
            f"Label: {sd_label}\n\nAre you sure?"
        )
        if not confirm:
            return

        progress = ProgressDialog(self, "Formatting SD Card")

        def worker():
            GLib.idle_add(progress.set_progress, 0.2, f"Formatting {device}...")
            success, msg = format_sd_card(device, label=sd_label,
                                          cluster_sectors_fn=cluster_fn)
            GLib.idle_add(progress.set_progress, 1.0, "Done!")
            if success:
                GLib.idle_add(self._show_success_and_close_progress, progress, msg)
            else:
                GLib.idle_add(self._show_error_and_close_progress, progress, msg)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _do_check_disk(self):
        dialog = DriveSelector(self)
        response = dialog.run()
        drive = dialog.selected_drive
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not drive:
            return

        device = f"/dev/{drive['name']}"
        partitions = get_drive_partitions(drive['name'])
        if not partitions:
            self._show_message("Error", "No partitions found on this drive.",
                               Gtk.MessageType.ERROR)
            return

        part_dev = f"/dev/{partitions[0]['name']}"
        result = check_disk(part_dev)
        self._show_message("Disk Check Results", result, Gtk.MessageType.INFO)

    # -- Helper Methods --

    def _show_message(self, title, message, msg_type=Gtk.MessageType.INFO):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=msg_type, buttons=Gtk.ButtonsType.OK, text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def _confirm(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO, text=title,
        )
        dialog.format_secondary_text(message)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def _show_error_and_close_progress(self, progress, message):
        progress.destroy()
        self._show_message("Error", message, Gtk.MessageType.ERROR)
        return False

    def _show_success_and_close_progress(self, progress, message):
        progress.destroy()
        self._show_message("Success", message, Gtk.MessageType.INFO)
        return False


class ReleasePicker(Gtk.Dialog):
    """Dialog to pick an OS release to download/use."""

    def __init__(self, parent, profile):
        os_name = profile["name"]
        super().__init__(
            title=f"Select {os_name} Release",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(500, 400)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        self.selected_release = None
        self.profile = profile

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(15)
        content.set_margin_end(15)
        content.set_margin_top(15)
        content.set_margin_bottom(15)

        downloaded = get_downloaded_releases(str(DOWNLOADS_DIR))
        if downloaded:
            local_frame = Gtk.Frame(label="Already Downloaded")
            local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            local_box.set_margin_start(10)
            local_box.set_margin_end(10)
            local_box.set_margin_top(10)
            local_box.set_margin_bottom(10)
            local_frame.add(local_box)
            content.pack_start(local_frame, False, False, 0)

            self.first_radio = None
            for dl in downloaded:
                size_mb = dl['size'] / (1024 * 1024)
                text = f"{dl['filename']} ({size_mb:.1f} MB)"
                if self.first_radio is None:
                    radio = Gtk.RadioButton.new_with_label(None, text)
                    self.first_radio = radio
                else:
                    radio = Gtk.RadioButton.new_with_label_from_widget(self.first_radio, text)
                radio.release_info = {'local_path': dl['path'], 'name': dl['filename']}
                radio.connect("toggled", self._on_release_toggled)
                local_box.pack_start(radio, False, False, 0)
        else:
            self.first_radio = None

        online_frame = Gtk.Frame(label="Download from GitHub")
        online_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        online_box.set_margin_start(10)
        online_box.set_margin_end(10)
        online_box.set_margin_top(10)
        online_box.set_margin_bottom(10)
        online_frame.add(online_box)
        content.pack_start(online_frame, True, True, 0)

        loading_label = Gtk.Label(label=f"Fetching {os_name} releases from GitHub...")
        online_box.pack_start(loading_label, False, False, 0)

        self.online_box = online_box
        self.loading_label = loading_label

        thread = threading.Thread(target=self._fetch_releases, daemon=True)
        thread.start()

        self.show_all()

    def _fetch_releases(self):
        try:
            release_data = fetch_releases(self.profile)
            releases = release_data.get('stable', []) + release_data.get('beta', [])
            GLib.idle_add(self._populate_releases, releases)
        except Exception as e:
            GLib.idle_add(self._show_fetch_error, str(e))

    def _populate_releases(self, releases):
        self.loading_label.destroy()
        if not releases:
            label = Gtk.Label(label="No releases found.")
            self.online_box.pack_start(label, False, False, 0)
            label.show()
            return

        for rel in releases[:10]:
            size_mb = rel.get('size', 0) / (1024 * 1024)
            pre = " [BETA]" if rel.get('prerelease') else ""
            text = f"{rel['name']}{pre} ({size_mb:.1f} MB)"

            if self.first_radio is None:
                radio = Gtk.RadioButton.new_with_label(None, text)
                self.first_radio = radio
            else:
                radio = Gtk.RadioButton.new_with_label_from_widget(self.first_radio, text)

            radio.release_info = {
                'url': rel['browser_download_url'],
                'name': rel['name'],
                'companion_urls': rel.get('companion_urls', []),
            }
            radio.connect("toggled", self._on_release_toggled)
            self.online_box.pack_start(radio, False, False, 0)
            radio.show()

        if self.first_radio:
            self.first_radio.set_active(True)
            self.selected_release = self.first_radio.release_info

    def _show_fetch_error(self, error):
        self.loading_label.set_text(f"Failed to fetch releases: {error}")

    def _on_release_toggled(self, button):
        if button.get_active():
            self.selected_release = button.release_info


def check_dependencies():
    """Check for required system tools and offer to install missing ones."""
    if platform.system() == "Windows":
        return True

    REQUIRED_TOOLS = {
        "parted":    "parted",
        "mkfs.vfat": "dosfstools",
        "fsck.vfat": "dosfstools",
        "partprobe": "parted",
        "udisksctl": "udisks2",
        "eject":     "eject",
        "udevadm":   "udev",
        "lsblk":     "util-linux",
        "7z":        "p7zip-full",
        "xz":        "xz-utils",
    }

    missing_pkgs = set()
    for cmd, pkg in REQUIRED_TOOLS.items():
        if not (shutil.which(cmd)
                or os.path.isfile(f"/sbin/{cmd}")
                or os.path.isfile(f"/usr/sbin/{cmd}")):
            missing_pkgs.add(pkg)

    if not missing_pkgs:
        return True

    pkg_list = " ".join(sorted(missing_pkgs))
    print(f"Missing packages: {pkg_list}")
    print("Attempting to install...")

    result = subprocess.run(
        ["pkexec", "apt-get", "install", "-y"] + sorted(missing_pkgs),
        capture_output=False,
    )
    return result.returncode == 0


def main():
    if not check_dependencies():
        print("Some dependencies could not be installed. The app may not work correctly.")

    css = b"""
    window {
        font-size: 10pt;
    }
    """
    style_provider = Gtk.CssProvider()
    style_provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        style_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    # Splash screen
    splash = None
    splash_path = RESOURCES_DIR / "icon_256.png"
    if splash_path.exists():
        splash = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        splash.set_decorated(False)
        splash.set_position(Gtk.WindowPosition.CENTER)
        splash.set_resizable(False)
        splash.set_keep_above(True)

        splash_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        splash_box.set_margin_start(20)
        splash_box.set_margin_end(20)
        splash_box.set_margin_top(20)
        splash_box.set_margin_bottom(20)
        splash.add(splash_box)

        splash_img = Gtk.Image.new_from_file(str(splash_path))
        splash_box.pack_start(splash_img, False, False, 0)

        splash_label = Gtk.Label()
        splash_label.set_markup(
            f"<big><b>{APP_NAME}</b></big>\n"
            f"<small>v{APP_VERSION} — Loading...</small>"
        )
        splash_label.set_justify(Gtk.Justification.CENTER)
        splash_box.pack_start(splash_label, False, False, 0)

        splash.show_all()
        # Process events so splash renders before building the main window
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    win = OSInstaller()
    win.show_all()

    if splash:
        splash.destroy()

    Gtk.main()


if __name__ == "__main__":
    main()
