# Filter-CAD-files-in-Fusion
A Fusion 360 Python script that scans a folder of 3D models (STEP, IGES, SAT, SMT, F3D), analyzes each one inside Fusion, and sorts them into **`medium_complexity`**, **`high_complexity`**, or **`rejected`** buckets based on file size and geometry rules.

It is designed for building/curating a clean CAD dataset: resumable across runs,
batch-friendly, and fully logged to CSV.

---

## Features

- **Multi-format import** via Fusion's `importManager`:
  `.step .stp .ste .sat .sab .smt .smb .iges .igs .ige .f3d`
- **Two-tier sorting**: every accepted model is classified as either *medium* or *high*
  complexity (anything outside the eligibility window is rejected).
- **Per-run prompts** for input folder, output folder, log folder, batch size, and
  max batches — defaults are remembered between runs in
  `~/.fusion_dataset_filter_last_paths.json`.
- **Batch processing with cooldowns** to give Fusion breathing room.
- **Resume-safe CSV log** (`filter_log.csv`): any file already listed there is
  skipped on the next run, so you can stop/restart at any time.
- **Human-readable run log** (`run_log.txt`) plus live progress in Fusion's Text
  Commands panel.
- **Non-destructive by default**: files are *copied* into output buckets, not moved.

---

## Requirements

- Autodesk Fusion 360 (Windows; the script uses Win32 paths and Fusion's Python API).
- The script must live under Fusion's *Scripts* folder so it shows up in the
  **Scripts and Add-Ins** dialog. The expected install location is:

  ```
  %APPDATA%\Autodesk\Autodesk Fusion 360\API\Scripts\filter step files\
  ```

  with these files alongside this README:

  - `filter step files.py`
  - `filter step files.manifest`
  - `ScriptIcon.svg`

No external Python packages are needed — the script only uses Fusion's bundled
Python and the standard library (`os`, `json`, `csv`, `shutil`, `time`,
`traceback`).

---

## Installation

1. Copy this entire folder to:

   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\Scripts\
   ```

2. In Fusion, open **Utilities → Add-Ins → Scripts** (shortcut `Shift+S`).
3. Select **filter step files** in the list and click **Run**.

   If you don't see it, click the green **+** next to *My Scripts* and point it
   at the folder you just copied.

---

## Usage

When you run the script, Fusion will ask you for, in order:

1. **Input folder** — folder containing the models to filter. Subfolders are *not*
   walked; only files directly inside this folder are considered.
2. **Output folder** — three subfolders are created here:
   - `medium_complexity/`
   - `high_complexity/`
   - `rejected/`
3. **Log folder** — where `filter_log.csv` and `run_log.txt` are written. This can
   be the same as the output folder.
4. **Batch size** — how many files to process before a short cooldown pause.
5. **Max batches** — `0` means *keep going* until every file is either processed
   or already listed in the CSV.

Defaults for the first three prompts come from the last successful run (stored in
`~/.fusion_dataset_filter_last_paths.json`), falling back to:

| Setting | Default |
| --- | --- |
| Input folder | `C:\FusionDataset\input` |
| Output folder | `C:\FusionDataset\filtered` |

When the run finishes, a summary dialog reports session totals and paths to all
output folders and logs.

---

## How a file is classified

For each importable file the script:

1. **Checks file size** — files outside `[MIN_FILE_KB, MAX_FILE_KB]` are
   immediately moved to `rejected/` without being imported.
2. **Imports the file** into a fresh Fusion design and recursively collects every
   solid body (including bodies inside sub-occurrences).
3. **Computes per-design stats**:
   - solid body count
   - total face count across all solid bodies
   - max bounding-box diagonal (cm)
   - average max / min bounding-box dimension
4. **Applies base eligibility rules** — if any of the following limits are
   violated, the file is rejected:

   | Rule | Min | Max |
   | --- | --- | --- |
   | Solid bodies | `1` | `10` |
   | Total faces | `10` | `1200` |
   | Max diagonal (cm) | `0.5` | `150.0` |

5. **Splits eligible models into two buckets**:

   - **`medium_complexity`** — *all three* of the following hold:
     - `total_faces ≤ 250`
     - `solid_body_count ≤ 5`
     - `max_diagonal ≤ 80.0 cm`
   - **`high_complexity`** — eligible models where any of those medium limits
     is exceeded.

6. **Copies the source file** into the appropriate bucket and appends a row to
   `filter_log.csv`.

If anything throws during analysis (corrupt file, unsupported feature, import
error, …) the file is sent to `rejected/` with the exception message recorded in
the `message` column.

All thresholds are constants near the top of `filter step files.py` — see the
[Configuration](#configuration) section to tune them.

---

## Outputs

### Folder layout

```
<output folder>/
├── medium_complexity/   # accepted, simpler models
├── high_complexity/     # accepted, more complex models
└── rejected/            # too small/large, out-of-range geometry, or errored
```

### CSV log — `filter_log.csv`

One row per processed file. Columns:

| Column | Meaning |
| --- | --- |
| `timestamp` | When the row was written |
| `file_name` | Source file name |
| `file_path` | Absolute source path (used to skip on re-runs) |
| `file_ext` | Lowercased extension |
| `file_size_kb` | Source file size in KB |
| `status` | `ok` or `rejected` |
| `bucket` | `medium_complexity`, `high_complexity`, or `rejected` |
| `complexity` | `medium` / `high` (blank for rejected) |
| `solid_body_count` | Solid bodies found in the imported design |
| `total_faces` | Sum of `body.faces.count` across solid bodies |
| `max_diagonal` | Largest bounding-box diagonal across solid bodies (cm) |
| `avg_max_dim` | Mean of each body's largest bbox edge |
| `avg_min_dim` | Mean of each body's smallest bbox edge |
| `message` | `accepted_medium`, `accepted_high`, `rejected_by_file_size`, `rejected_by_geometry`, or `exception: …` |

> **Resume behavior:** any file whose absolute path already appears in
> `filter_log.csv` is skipped on subsequent runs. To re-process a file, delete
> its row from the CSV (or delete the whole CSV to start over).

> **Legacy CSVs:** older CSVs that used the `good`/`bad` bucket scheme are
> auto-migrated to the new `medium_complexity`/`high_complexity`/`rejected`
> scheme the first time you run this version.

### Run log — `run_log.txt`

Plain-text, timestamped progress lines (`BEGIN`, `MEDIUM`, `HIGH`, `REJECTED`,
`ERROR`, batch boundaries). The same lines are also printed to Fusion's *Text
Commands* panel while the script is running.

---

## Configuration

All tunable knobs live as module-level constants at the top of
`filter step files.py`. The most useful ones:

```python
DEFAULT_SOURCE_MODEL_DIR = r'C:\FusionDataset\input'
DEFAULT_OUTPUT_ROOT      = r'C:\FusionDataset\filtered'

COPY_INSTEAD_OF_MOVE     = True   # set False to move source files instead of copying
BATCH_SIZE               = 50
MAX_BATCHES_PER_RUN      = 0      # 0 = unlimited
PAUSE_BETWEEN_BATCHES_SEC = 2.0
PAUSE_EVERY_N_FILES      = 10
PAUSE_SECONDS            = 1.5

MIN_FILE_KB              = 5
MAX_FILE_KB              = 10240   # 10 MB

MIN_SOLID_BODIES         = 1
MAX_SOLID_BODIES         = 10
MIN_TOTAL_FACES          = 10
MAX_TOTAL_FACES          = 1200
MIN_DIAGONAL_CM          = 0.5
MAX_DIAGONAL_CM          = 150.0

MEDIUM_MAX_TOTAL_FACES   = 250
MEDIUM_MAX_SOLID_BODIES  = 5
MEDIUM_MAX_DIAGONAL_CM   = 80.0
```

`BATCH_SIZE` and `MAX_BATCHES_PER_RUN` are only the *defaults* shown in the
prompts; the actual values used per run come from what you type in the dialogs.

---

## Tips & troubleshooting

- **Nothing happens / "No importable model files"** — check that your input
  folder contains files with one of the supported extensions and that the path
  is correct (the script does not recurse into subfolders).
- **"Nothing to do."** — every file in the input folder is already logged in
  `filter_log.csv`. Either point at a different log folder, or delete the rows
  you want to re-process.
- **Fusion becomes unresponsive** — lower `BATCH_SIZE` and/or reduce
  `PAUSE_EVERY_N_FILES`, and make sure you have no other heavy documents open.
- **Want to move instead of copy?** Set `COPY_INSTEAD_OF_MOVE = False`. The
  source folder will be drained as files are sorted.
- **Re-running after a crash** — just launch the script again with the same
  three folders. The CSV-based skip list makes the workflow fully resumable.

---

## Files in this folder

| File | Purpose |
| --- | --- |
| `filter step files.py` | The Fusion script (entry point is `run(context)`) |
| `filter step files.manifest` | Fusion script manifest |
| `ScriptIcon.svg` | Icon shown in Fusion's Scripts dialog |
| `README.md` | This document |
