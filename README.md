# AFD3 Archive Tool

This folder contains `afd3tool.py`, a Python 3 tool for the three AirForce Delta:
Blue Wing Knights archives:

- `AFD3.000`: custom AFD3/KFS virtual filesystem
- `AFD3_JP.001`: AFS archive
- `AFD3_JP.002`: custom AFD3/KFS virtual filesystem

The tool supports listing, unpacking, and repacking the same file set/order. For
the custom KFS archives, packing uses the original archive as a metadata template
and patches the primary file records with new offsets and sizes.

## GUI

Double-click `afd3tool_gui.pyw` to open the graphical version without using CMD.
You can also double-click `afd3tool.py`; if Windows opens `.py` files with
Python, it will show the same GUI when no command-line arguments are supplied.

The GUI has three tabs:

- `List`: choose an archive and view its entries.
- `Unpack`: choose an archive and an output folder.
- `Pack`: choose an unpacked folder, output archive, and template archive.

For `AFD3.000` and `AFD3_JP.002`, use the original archive as the template when
packing. `AFD3_JP.001` is AFS and does not need a template.

## List

```powershell
python .\afd3tool.py list .\AFD3.000
python .\afd3tool.py list .\AFD3_JP.001
python .\afd3tool.py list .\AFD3_JP.002
```

Use `--all` to print every entry.

## Unpack

```powershell
python .\afd3tool.py unpack .\AFD3.000 .\unpacked\AFD3.000
python .\afd3tool.py unpack .\AFD3_JP.001 .\unpacked\AFD3_JP.001
python .\afd3tool.py unpack .\AFD3_JP.002 .\unpacked\AFD3_JP.002
```

Each output directory gets a `manifest.json`. Keep it with the extracted files;
the pack command uses it to rebuild the archive.

## Pack

```powershell
python .\afd3tool.py pack .\unpacked\AFD3.000 .\rebuilt\AFD3.000 --template .\AFD3.000
python .\afd3tool.py pack .\unpacked\AFD3_JP.001 .\rebuilt\AFD3_JP.001
python .\afd3tool.py pack .\unpacked\AFD3_JP.002 .\rebuilt\AFD3_JP.002 --template .\AFD3_JP.002
```

By default, rebuilt archives are padded back to the original archive size when
possible. This is useful if you are replacing the files inside an existing PS2
ISO layout. If your edits make an archive larger than the original, packing will
stop unless you pass `--allow-grow`.

Use `--force` to replace an existing output file.

## Notes

- The KFS packer is intended for replacing existing files, not adding/removing
  paths.
- `AFD3_JP.001` is a nameless AFS archive, so entries are extracted as
  `0000.bin`, `0001.bin`, and so on.
- If you grow an archive with `--allow-grow`, you will need an ISO rebuild flow
  that updates file sizes/locations instead of only overwriting bytes in place.

## Mission Text Editing

Mission/dialogue text from `AFD3.000` is mostly in:

```text
unpacked\AFD3.000\usr13972\dsc
```

Use `dsc_text_gui.pyw` to export/import editable `.txt` files without CMD. The
current editable export is in:

```text
MODULES\editable_dsc_text
```

Edit only the `TEXT=` lines. Keep `[segment ...]` headers intact. For English
files, the exporter converts the game's DS glyph codes into normal readable
letters. Japanese `jp` scripts may use DS glyph IDs; unknown glyphs are kept as
tags like `<04A1>` so they can be round-tripped safely. If a file contains real
Shift-JIS text, the tool preserves it as Shift-JIS on import.

CLI equivalents:

```powershell
python .\dsc_text_tool.py export .\unpacked\AFD3.000\usr13972\dsc .\MODULES\editable_dsc_text
python .\dsc_text_tool.py import .\MODULES\editable_dsc_text .\unpacked\AFD3.000\usr13972\dsc
```
