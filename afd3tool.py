#!/usr/bin/env python3
"""Unpack and repack AirForce Delta: Blue Wing Knights PS2 AFD3 archives."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import struct
import sys
import threading
from pathlib import Path, PurePosixPath


TOOL_VERSION = 1
KFS_MAGIC = b"\xaeG\xe1="
AFS_MAGIC = b"AFS\x00"
KFS_RECORD_SIZE = 0x40
KFS_RECORD_START = 0x20
COPY_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    pass


def read_u32le(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def write_u32le(f, offset: int, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ArchiveError(f"value does not fit in 32 bits: {value}")
    f.seek(offset)
    f.write(struct.pack("<I", value))


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def detect_alignment(offsets: list[int], default: int = 0x10) -> int:
    if not offsets:
        return default
    for alignment in (0x800, 0x80, 0x10, 4):
        if all(offset % alignment == 0 for offset in offsets):
            return alignment
    return 1


def decode_name(raw: bytes) -> str | None:
    raw = raw.split(b"\0", 1)[0]
    if not raw:
        return None
    if any(byte < 0x20 or byte >= 0x7F for byte in raw):
        return None
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None


def safe_member_path(root: Path, member_name: str) -> Path:
    member = PurePosixPath(member_name)
    if member.is_absolute() or ".." in member.parts:
        raise ArchiveError(f"unsafe archive path: {member_name}")
    out_path = root
    for part in member.parts:
        if not part or part in (".", ".."):
            raise ArchiveError(f"unsafe archive path: {member_name}")
        out_path = out_path / part
    return out_path


def copy_range(src, dst, size: int) -> None:
    remaining = size
    while remaining:
        chunk = src.read(min(COPY_CHUNK, remaining))
        if not chunk:
            raise ArchiveError("unexpected end of file while copying data")
        dst.write(chunk)
        remaining -= len(chunk)


def copy_file_to_stream(src_path: Path, dst) -> int:
    total = 0
    with src_path.open("rb") as src:
        while True:
            chunk = src.read(COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            total += len(chunk)
    return total


def read_prefix(path: Path, size: int) -> bytes:
    with path.open("rb") as f:
        data = f.read(size)
    if len(data) != size:
        raise ArchiveError(f"could not read {size} prefix bytes from {path}")
    return data


def detect_archive(path: Path) -> str:
    with path.open("rb") as f:
        magic = f.read(4)
    if magic == AFS_MAGIC:
        return "afs"
    if magic == KFS_MAGIC:
        return "kfs"
    raise ArchiveError(f"unsupported archive magic in {path}")


def parse_afs(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as f:
        header = f.read(8)
        if len(header) != 8 or header[:4] != AFS_MAGIC:
            raise ArchiveError("not an AFS archive")
        count = read_u32le(header, 4)
        table = f.read(count * 8)
    if len(table) != count * 8:
        raise ArchiveError("truncated AFS table")

    entries = []
    offsets = []
    for index in range(count):
        entry_offset = index * 8
        data_offset = read_u32le(table, entry_offset)
        data_size = read_u32le(table, entry_offset + 4)
        if data_offset + data_size > size:
            raise ArchiveError(
                f"AFS entry {index} points past end: "
                f"offset=0x{data_offset:X} size=0x{data_size:X}"
            )
        offsets.append(data_offset)
        entries.append(
            {
                "index": index,
                "name": f"{index:04d}.bin",
                "offset": data_offset,
                "size": data_size,
                "table_offset": 8 + entry_offset,
            }
        )

    first_data = min(offsets) if offsets else align_up(8 + count * 8, 0x800)
    prefix = read_prefix(path, first_data)
    return {
        "tool": "afd3tool",
        "version": TOOL_VERSION,
        "archive_format": "afs",
        "archive_name": path.name,
        "original_size": size,
        "count": count,
        "first_data_offset": first_data,
        "alignment": detect_alignment(offsets, 0x800),
        "prefix_hex": prefix.hex(),
        "entries": entries,
    }


def valid_kfs_record(record: bytes, archive_size: int, previous_offset: int | None) -> bool:
    name = decode_name(record[8:56])
    if name is None:
        return False
    data_size = read_u32le(record, 56)
    data_offset = read_u32le(record, 60)
    if data_offset == 0 or data_offset >= archive_size:
        return False
    if data_offset + data_size > archive_size:
        return False
    if previous_offset is not None and data_offset < previous_offset:
        return False
    return True


def parse_kfs(path: Path) -> dict:
    archive_size = path.stat().st_size
    entries = []
    offsets = []
    previous_offset: int | None = None

    with path.open("rb") as f:
        header = f.read(KFS_RECORD_START)
        if len(header) != KFS_RECORD_START or header[:4] != KFS_MAGIC:
            raise ArchiveError("not an AFD3/KFS archive")

        record_offset = KFS_RECORD_START
        while True:
            f.seek(record_offset)
            record = f.read(KFS_RECORD_SIZE)
            if len(record) != KFS_RECORD_SIZE:
                break
            if not valid_kfs_record(record, archive_size, previous_offset):
                break

            name = decode_name(record[8:56])
            assert name is not None
            data_size = read_u32le(record, 56)
            data_offset = read_u32le(record, 60)
            offsets.append(data_offset)
            entries.append(
                {
                    "index": len(entries),
                    "path": name,
                    "offset": data_offset,
                    "size": data_size,
                    "record_offset": record_offset,
                    "metadata0": read_u32le(record, 0),
                    "metadata1": read_u32le(record, 4),
                }
            )
            previous_offset = data_offset
            record_offset += KFS_RECORD_SIZE

    if not entries:
        raise ArchiveError("no KFS file records found")

    first_data = min(offsets)
    header_size = read_u32le(header, 4)
    return {
        "tool": "afd3tool",
        "version": TOOL_VERSION,
        "archive_format": "kfs",
        "archive_name": path.name,
        "original_size": archive_size,
        "header_archive_size": header_size,
        "first_data_offset": first_data,
        "alignment": detect_alignment(offsets),
        "entries": entries,
    }


def parse_archive(path: Path) -> dict:
    kind = detect_archive(path)
    if kind == "afs":
        return parse_afs(path)
    if kind == "kfs":
        return parse_kfs(path)
    raise ArchiveError(f"unsupported archive type: {kind}")


def write_manifest(out_dir: Path, manifest: dict) -> None:
    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def unpack_archive(archive: Path, out_dir: Path, overwrite: bool) -> None:
    manifest = parse_archive(archive)
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise ArchiveError(f"{out_dir} already exists and is not empty; use --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    with archive.open("rb") as src:
        for entry in manifest["entries"]:
            member_name = entry.get("path", entry.get("name"))
            out_path = safe_member_path(out_dir, member_name)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            src.seek(entry["offset"])
            with out_path.open("wb") as dst:
                copy_range(src, dst, entry["size"])

    write_manifest(out_dir, manifest)
    print(
        f"unpacked {len(manifest['entries'])} {manifest['archive_format'].upper()} "
        f"entries to {out_dir}"
    )


def load_manifest(unpacked_dir: Path) -> dict:
    manifest_path = unpacked_dir / "manifest.json"
    if not manifest_path.exists():
        raise ArchiveError(f"missing manifest: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("tool") != "afd3tool":
        raise ArchiveError("manifest was not created by afd3tool")
    if manifest.get("version") != TOOL_VERSION:
        raise ArchiveError(
            f"unsupported manifest version {manifest.get('version')}; "
            f"expected {TOOL_VERSION}"
        )
    return manifest


def ensure_output_path(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise ArchiveError(f"{path} already exists; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)


def pad_stream(dst, target_pos: int) -> None:
    current = dst.tell()
    if current > target_pos:
        raise ArchiveError(f"stream position 0x{current:X} passed target 0x{target_pos:X}")
    if current < target_pos:
        dst.write(b"\0" * (target_pos - current))


def finalize_temp(temp_path: Path, out_path: Path, force: bool) -> None:
    if out_path.exists():
        if not force:
            raise ArchiveError(f"{out_path} already exists; use --force to replace it")
        out_path.unlink()
    os.replace(temp_path, out_path)


def pack_afs(
    unpacked_dir: Path,
    manifest: dict,
    out_path: Path,
    preserve_size: bool,
    allow_grow: bool,
    force: bool,
) -> None:
    entries = manifest["entries"]
    count = manifest["count"]
    first_data = manifest["first_data_offset"]
    alignment = manifest.get("alignment", 0x800)
    original_size = manifest["original_size"]

    if len(entries) != count:
        raise ArchiveError("AFS manifest entry count mismatch")

    ensure_output_path(out_path, force)
    temp_path = out_path.with_name(out_path.name + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    new_table: list[tuple[int, int]] = []
    try:
        with temp_path.open("w+b") as dst:
            table_size = 8 + count * 8
            if table_size > first_data:
                raise ArchiveError("AFS table is larger than original data start")
            prefix_hex = manifest.get("prefix_hex")
            if prefix_hex:
                prefix = bytes.fromhex(prefix_hex)
                if len(prefix) != first_data:
                    raise ArchiveError("AFS manifest prefix size does not match first data offset")
                dst.write(prefix)
            else:
                dst.write(AFS_MAGIC)
                dst.write(struct.pack("<I", count))
                dst.write(b"\0" * (first_data - table_size))

            for entry in entries:
                target = align_up(dst.tell(), alignment)
                pad_stream(dst, target)
                member = unpacked_dir / entry["name"]
                if not member.exists():
                    raise ArchiveError(f"missing extracted file: {member}")
                data_offset = dst.tell()
                data_size = copy_file_to_stream(member, dst)
                new_table.append((data_offset, data_size))

            rebuilt_size = dst.tell()
            final_size = original_size if preserve_size and rebuilt_size <= original_size else rebuilt_size
            if final_size > original_size and not allow_grow:
                raise ArchiveError(
                    f"rebuilt archive is larger than original "
                    f"({final_size} > {original_size}); use --allow-grow"
                )
            pad_stream(dst, final_size)

            dst.seek(0)
            dst.write(AFS_MAGIC)
            dst.write(struct.pack("<I", count))
            dst.seek(8)
            for data_offset, data_size in new_table:
                dst.write(struct.pack("<II", data_offset, data_size))
        finalize_temp(temp_path, out_path, force)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    print(f"packed AFS archive with {len(entries)} entries: {out_path}")


def pack_kfs(
    unpacked_dir: Path,
    manifest: dict,
    template_path: Path,
    out_path: Path,
    preserve_size: bool,
    allow_grow: bool,
    force: bool,
) -> None:
    if detect_archive(template_path) != "kfs":
        raise ArchiveError("template archive is not an AFD3/KFS archive")

    template_manifest = parse_kfs(template_path)
    if len(template_manifest["entries"]) != len(manifest["entries"]):
        raise ArchiveError("template entry count does not match manifest")

    first_data = manifest["first_data_offset"]
    alignment = manifest.get("alignment", 0x10)
    original_size = manifest["original_size"]

    ensure_output_path(out_path, force)
    temp_path = out_path.with_name(out_path.name + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    new_table: list[tuple[int, int, int]] = []
    try:
        prefix = read_prefix(template_path, first_data)
        with temp_path.open("w+b") as dst:
            dst.write(prefix)
            for entry in manifest["entries"]:
                target = align_up(dst.tell(), alignment)
                pad_stream(dst, target)
                member = safe_member_path(unpacked_dir, entry["path"])
                if not member.exists():
                    raise ArchiveError(f"missing extracted file: {member}")
                data_offset = dst.tell()
                data_size = copy_file_to_stream(member, dst)
                new_table.append((entry["record_offset"], data_offset, data_size))

            rebuilt_size = dst.tell()
            final_size = original_size if preserve_size and rebuilt_size <= original_size else rebuilt_size
            if final_size > original_size and not allow_grow:
                raise ArchiveError(
                    f"rebuilt archive is larger than original "
                    f"({final_size} > {original_size}); use --allow-grow"
                )
            pad_stream(dst, final_size)

            write_u32le(dst, 4, final_size)
            for record_offset, data_offset, data_size in new_table:
                write_u32le(dst, record_offset + 56, data_size)
                write_u32le(dst, record_offset + 60, data_offset)
        finalize_temp(temp_path, out_path, force)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    print(f"packed KFS archive with {len(manifest['entries'])} entries: {out_path}")


def pack_archive(
    unpacked_dir: Path,
    template_archive: Path | None,
    out_path: Path,
    preserve_size: bool,
    allow_grow: bool,
    force: bool,
) -> None:
    manifest = load_manifest(unpacked_dir)
    fmt = manifest["archive_format"]
    if fmt == "afs":
        pack_afs(unpacked_dir, manifest, out_path, preserve_size, allow_grow, force)
    elif fmt == "kfs":
        if template_archive is None:
            raise ArchiveError("KFS packing requires a template archive")
        pack_kfs(
            unpacked_dir,
            manifest,
            template_archive,
            out_path,
            preserve_size,
            allow_grow,
            force,
        )
    else:
        raise ArchiveError(f"unsupported manifest archive format: {fmt}")


def list_archive(archive: Path, limit: int | None) -> None:
    manifest = parse_archive(archive)
    entries = manifest["entries"]
    print(f"{archive}")
    print(f"  format: {manifest['archive_format'].upper()}")
    print(f"  entries: {len(entries)}")
    print(f"  size: {manifest['original_size']} bytes")
    print(f"  first data: 0x{manifest['first_data_offset']:X}")
    print(f"  alignment: 0x{manifest['alignment']:X}")

    shown = entries if limit is None else entries[:limit]
    for entry in shown:
        name = entry.get("path", entry.get("name"))
        print(
            f"  {entry['index']:04d}  "
            f"off=0x{entry['offset']:08X}  "
            f"size=0x{entry['size']:08X}  "
            f"{name}"
        )
    if limit is not None and len(entries) > limit:
        print(f"  ... {len(entries) - limit} more entries; use --all to show everything")


def launch_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        print(f"error: could not start GUI: {exc}", file=sys.stderr)
        return 1

    base_dir = Path(__file__).resolve().parent

    class Afd3Gui(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("AFD3 Archive Tool")
            self.geometry("920x640")
            self.minsize(820, 560)
            self._busy_count = 0

            self.columnconfigure(0, weight=1)
            self.rowconfigure(0, weight=1)
            self.rowconfigure(1, weight=0)

            self.tabs = ttk.Notebook(self)
            self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))

            self.log_text = tk.Text(self, height=8, wrap="word", state="disabled")
            self.log_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

            self.status_var = tk.StringVar(value="Ready")
            status = ttk.Label(self, textvariable=self.status_var, anchor="w")
            status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

            self._build_list_tab()
            self._build_unpack_tab()
            self._build_pack_tab()

        def _build_list_tab(self) -> None:
            tab = ttk.Frame(self.tabs, padding=10)
            tab.columnconfigure(1, weight=1)
            tab.rowconfigure(2, weight=1)
            self.tabs.add(tab, text="List")

            self.list_archive_var = tk.StringVar(value=str(base_dir / "AFD3.000"))
            ttk.Label(tab, text="Archive").grid(row=0, column=0, sticky="w")
            ttk.Entry(tab, textvariable=self.list_archive_var).grid(
                row=0, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_list_archive).grid(
                row=0, column=2
            )

            button_row = ttk.Frame(tab)
            button_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=8)
            ttk.Button(button_row, text="List Archive", command=self._list_selected).pack(
                side="left"
            )
            ttk.Button(button_row, text="Clear", command=self._clear_list).pack(
                side="left", padx=8
            )

            columns = ("index", "offset", "size", "name")
            self.list_tree = ttk.Treeview(tab, columns=columns, show="headings")
            self.list_tree.heading("index", text="#")
            self.list_tree.heading("offset", text="Offset")
            self.list_tree.heading("size", text="Size")
            self.list_tree.heading("name", text="Name")
            self.list_tree.column("index", width=70, anchor="e", stretch=False)
            self.list_tree.column("offset", width=120, anchor="e", stretch=False)
            self.list_tree.column("size", width=120, anchor="e", stretch=False)
            self.list_tree.column("name", width=520, stretch=True)
            self.list_tree.grid(row=2, column=0, columnspan=3, sticky="nsew")

            scroll = ttk.Scrollbar(tab, orient="vertical", command=self.list_tree.yview)
            scroll.grid(row=2, column=3, sticky="ns")
            self.list_tree.configure(yscrollcommand=scroll.set)

        def _build_unpack_tab(self) -> None:
            tab = ttk.Frame(self.tabs, padding=10)
            tab.columnconfigure(1, weight=1)
            self.tabs.add(tab, text="Unpack")

            self.unpack_archive_var = tk.StringVar(value=str(base_dir / "AFD3.000"))
            self.unpack_out_var = tk.StringVar(value=str(base_dir / "unpacked" / "AFD3.000"))
            self.unpack_overwrite_var = tk.BooleanVar(value=False)

            ttk.Label(tab, text="Archive").grid(row=0, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=self.unpack_archive_var).grid(
                row=0, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_unpack_archive).grid(
                row=0, column=2
            )

            ttk.Label(tab, text="Output folder").grid(row=1, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=self.unpack_out_var).grid(
                row=1, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_unpack_out).grid(
                row=1, column=2
            )

            ttk.Checkbutton(
                tab,
                text="Overwrite if folder is not empty",
                variable=self.unpack_overwrite_var,
            ).grid(row=2, column=1, sticky="w", pady=8)

            ttk.Button(tab, text="Unpack", command=self._unpack_selected).grid(
                row=3, column=1, sticky="w"
            )

        def _build_pack_tab(self) -> None:
            tab = ttk.Frame(self.tabs, padding=10)
            tab.columnconfigure(1, weight=1)
            self.tabs.add(tab, text="Pack")

            self.pack_dir_var = tk.StringVar(value=str(base_dir / "unpacked" / "AFD3.000"))
            self.pack_out_var = tk.StringVar(value=str(base_dir / "rebuilt" / "AFD3.000"))
            self.pack_template_var = tk.StringVar(value=str(base_dir / "AFD3.000"))
            self.pack_force_var = tk.BooleanVar(value=False)
            self.pack_allow_grow_var = tk.BooleanVar(value=False)
            self.pack_preserve_size_var = tk.BooleanVar(value=True)

            ttk.Label(tab, text="Unpacked folder").grid(row=0, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=self.pack_dir_var).grid(
                row=0, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_pack_dir).grid(
                row=0, column=2
            )

            ttk.Label(tab, text="Output archive").grid(row=1, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=self.pack_out_var).grid(
                row=1, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_pack_out).grid(
                row=1, column=2
            )

            ttk.Label(tab, text="Template archive").grid(row=2, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=self.pack_template_var).grid(
                row=2, column=1, sticky="ew", padx=8
            )
            ttk.Button(tab, text="Browse", command=self._browse_pack_template).grid(
                row=2, column=2
            )

            options = ttk.Frame(tab)
            options.grid(row=3, column=1, sticky="w", pady=8)
            ttk.Checkbutton(options, text="Replace output", variable=self.pack_force_var).pack(
                side="left"
            )
            ttk.Checkbutton(
                options,
                text="Preserve original size",
                variable=self.pack_preserve_size_var,
            ).pack(side="left", padx=12)
            ttk.Checkbutton(
                options,
                text="Allow larger archive",
                variable=self.pack_allow_grow_var,
            ).pack(side="left")

            ttk.Button(tab, text="Pack", command=self._pack_selected).grid(
                row=4, column=1, sticky="w"
            )

        def _archive_filetypes(self):
            return (
                ("AFD3 archives", "AFD3*"),
                ("All files", "*.*"),
            )

        def _browse_list_archive(self) -> None:
            self._browse_file_into(self.list_archive_var, open_file=True)

        def _browse_unpack_archive(self) -> None:
            self._browse_file_into(self.unpack_archive_var, open_file=True)

        def _browse_unpack_out(self) -> None:
            self._browse_dir_into(self.unpack_out_var)

        def _browse_pack_dir(self) -> None:
            self._browse_dir_into(self.pack_dir_var)

        def _browse_pack_out(self) -> None:
            self._browse_file_into(self.pack_out_var, open_file=False)

        def _browse_pack_template(self) -> None:
            self._browse_file_into(self.pack_template_var, open_file=True)

        def _browse_file_into(self, var: tk.StringVar, open_file: bool) -> None:
            initial = Path(var.get()).expanduser()
            initial_dir = initial.parent if initial.parent.exists() else base_dir
            if open_file:
                chosen = filedialog.askopenfilename(
                    parent=self,
                    initialdir=str(initial_dir),
                    filetypes=self._archive_filetypes(),
                )
            else:
                chosen = filedialog.asksaveasfilename(
                    parent=self,
                    initialdir=str(initial_dir),
                    initialfile=initial.name,
                    filetypes=self._archive_filetypes(),
                )
            if chosen:
                var.set(chosen)

        def _browse_dir_into(self, var: tk.StringVar) -> None:
            initial = Path(var.get()).expanduser()
            initial_dir = initial if initial.exists() else initial.parent
            if not initial_dir.exists():
                initial_dir = base_dir
            chosen = filedialog.askdirectory(parent=self, initialdir=str(initial_dir))
            if chosen:
                var.set(chosen)

        def _clear_list(self) -> None:
            for item in self.list_tree.get_children():
                self.list_tree.delete(item)

        def _list_selected(self) -> None:
            archive = Path(self.list_archive_var.get())

            def work():
                manifest = parse_archive(archive)
                return manifest

            def done(manifest):
                self._clear_list()
                for entry in manifest["entries"]:
                    name = entry.get("path", entry.get("name"))
                    self.list_tree.insert(
                        "",
                        "end",
                        values=(
                            f"{entry['index']:04d}",
                            f"0x{entry['offset']:08X}",
                            f"0x{entry['size']:08X}",
                            name,
                        ),
                    )
                self._log(
                    f"Listed {len(manifest['entries'])} "
                    f"{manifest['archive_format'].upper()} entries from {archive}"
                )

            self._run_task("Listing archive...", work, done)

        def _unpack_selected(self) -> None:
            archive = Path(self.unpack_archive_var.get())
            out_dir = Path(self.unpack_out_var.get())
            overwrite = self.unpack_overwrite_var.get()

            def work():
                return self._capture_stdout(unpack_archive, archive, out_dir, overwrite)

            def done(output):
                self._log(output.strip() or f"Unpacked {archive} to {out_dir}")
                messagebox.showinfo("Unpack complete", f"Finished unpacking:\n{archive}")

            self._run_task("Unpacking archive...", work, done)

        def _pack_selected(self) -> None:
            unpacked_dir = Path(self.pack_dir_var.get())
            out_archive = Path(self.pack_out_var.get())
            template_text = self.pack_template_var.get().strip()
            template = Path(template_text) if template_text else None
            preserve_size = self.pack_preserve_size_var.get()
            allow_grow = self.pack_allow_grow_var.get()
            force = self.pack_force_var.get()

            def work():
                return self._capture_stdout(
                    pack_archive,
                    unpacked_dir,
                    template,
                    out_archive,
                    preserve_size,
                    allow_grow,
                    force,
                )

            def done(output):
                self._log(output.strip() or f"Packed {out_archive}")
                messagebox.showinfo("Pack complete", f"Finished packing:\n{out_archive}")

            self._run_task("Packing archive...", work, done)

        def _capture_stdout(self, func, *args):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                func(*args)
            return buf.getvalue()

        def _run_task(self, busy_message: str, work, done) -> None:
            self._set_busy(True, busy_message)

            def runner() -> None:
                try:
                    result = work()
                except Exception as exc:
                    self.after(0, lambda exc=exc: self._task_failed(exc))
                else:
                    self.after(0, lambda result=result: self._task_done(done, result))

            threading.Thread(target=runner, daemon=True).start()

        def _task_done(self, done, result) -> None:
            self._set_busy(False, "Ready")
            done(result)

        def _task_failed(self, exc: Exception) -> None:
            self._set_busy(False, "Ready")
            self._log(f"Error: {exc}")
            messagebox.showerror("AFD3 Tool Error", str(exc))

        def _set_busy(self, busy: bool, message: str) -> None:
            if busy:
                self._busy_count += 1
                self.config(cursor="watch")
            else:
                self._busy_count = max(0, self._busy_count - 1)
                if self._busy_count == 0:
                    self.config(cursor="")
            self.status_var.set(message)
            self.update_idletasks()

        def _log(self, text: str) -> None:
            if not text:
                return
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text.rstrip() + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    app = Afd3Gui()
    app.mainloop()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List, unpack, and repack AirForce Delta AFD3 archives."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="show archive contents")
    list_parser.add_argument("archive", type=Path)
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--all", action="store_true", help="show every entry")

    unpack_parser = subparsers.add_parser("unpack", help="extract an archive")
    unpack_parser.add_argument("archive", type=Path)
    unpack_parser.add_argument("out_dir", type=Path)
    unpack_parser.add_argument("--overwrite", action="store_true")

    pack_parser = subparsers.add_parser("pack", help="rebuild an unpacked archive")
    pack_parser.add_argument("unpacked_dir", type=Path)
    pack_parser.add_argument("out_archive", type=Path)
    pack_parser.add_argument(
        "--template",
        type=Path,
        help="original archive to use as a metadata template; required for AFD3.000/AFD3_JP.002",
    )
    pack_parser.add_argument(
        "--no-preserve-size",
        action="store_true",
        help="do not pad rebuilt archives back to the original byte size",
    )
    pack_parser.add_argument(
        "--allow-grow",
        action="store_true",
        help="allow output larger than the original archive",
    )
    pack_parser.add_argument("--force", action="store_true", help="replace output if it exists")

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return launch_gui()

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            list_archive(args.archive, None if args.all else args.limit)
        elif args.command == "unpack":
            unpack_archive(args.archive, args.out_dir, args.overwrite)
        elif args.command == "pack":
            pack_archive(
                args.unpacked_dir,
                args.template,
                args.out_archive,
                preserve_size=not args.no_preserve_size,
                allow_grow=args.allow_grow,
                force=args.force,
            )
        else:
            parser.error(f"unknown command: {args.command}")
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
