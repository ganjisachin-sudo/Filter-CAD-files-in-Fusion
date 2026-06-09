import adsk.core
import adsk.fusion
import traceback
import time
import os
import json
import shutil
import csv

# =========================================================
# CONFIG — filter: SOURCE_MODEL_DIR → medium / high / rejected
# =========================================================

# -------------------------
# Default paths (used as inputBox defaults; user can change each run)
# -------------------------
DEFAULT_SOURCE_MODEL_DIR = r'C:\FusionDataset\input'
DEFAULT_OUTPUT_ROOT = r'C:\FusionDataset\filtered'

# Remember last-used folders between runs (JSON in user profile).
LAST_PATHS_FILE = os.path.join(
    os.path.expanduser('~'),
    '.fusion_dataset_filter_last_paths.json',
)

# -------------------------
# Filtering behavior
# -------------------------
COPY_INSTEAD_OF_MOVE = True

# Files processed per batch (also the default in the batch-settings dialog).
BATCH_SIZE = 50

# Max batches in one script run. 0 = keep going until no unprocessed files remain
# (still skips anything already listed in filter_log.csv by file_path).
MAX_BATCHES_PER_RUN = 0

# Pause after each batch (Fusion breathing room); 0 to disable.
PAUSE_BETWEEN_BATCHES_SEC = 2.0

PAUSE_EVERY_N_FILES = 10
PAUSE_SECONDS = 1.5

SHOW_PROGRESS_IN_TEXT = True

# -------------------------
# File size (reject before import)
# -------------------------
MIN_FILE_KB = 5
MAX_FILE_KB = 10240

# -------------------------
# Base eligibility (must pass or → rejected)
# Applies to both medium and high buckets.
# -------------------------
MIN_SOLID_BODIES = 1
MAX_SOLID_BODIES = 10

MIN_TOTAL_FACES = 10
MAX_TOTAL_FACES = 1200

MIN_DIAGONAL_CM = 0.5
MAX_DIAGONAL_CM = 150.0

# -------------------------
# Medium vs high split (partition of eligible models)
# If all three are at or below these limits → medium_complexity.
# If any exceeds → high_complexity (still subject to base caps above).
# -------------------------
MEDIUM_MAX_TOTAL_FACES = 250
MEDIUM_MAX_SOLID_BODIES = 5
MEDIUM_MAX_DIAGONAL_CM = 80.0

# =========================================================
# FILE TYPES — only extensions this script imports via API
# =========================================================

DIRECT_3D_IMPORT_EXTS = {
    '.step', '.stp', '.ste',
    '.sat', '.sab',
    '.smt', '.smb',
    '.iges', '.igs', '.ige',
    '.f3d',
}

# =========================================================
# GENERAL HELPERS
# =========================================================

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def make_paths(source_dir, output_root, log_root):
    """Layout: output_root/{medium_complexity,high_complexity,rejected}; logs in log_root."""
    src = os.path.normpath(os.path.abspath(source_dir))
    out = os.path.normpath(os.path.abspath(output_root))
    log_r = os.path.normpath(os.path.abspath(log_root))
    return {
        'source': src,
        'output_root': out,
        'log_root': log_r,
        'medium': os.path.join(out, 'medium_complexity'),
        'high': os.path.join(out, 'high_complexity'),
        'rejected': os.path.join(out, 'rejected'),
        'filter_log_csv': os.path.join(log_r, 'filter_log.csv'),
        'run_log_txt': os.path.join(log_r, 'run_log.txt'),
    }


def ensure_all_dirs(paths):
    ensure_dir(paths['output_root'])
    ensure_dir(paths['medium'])
    ensure_dir(paths['high'])
    ensure_dir(paths['rejected'])
    ensure_dir(paths['log_root'])


def app_and_ui():
    app = adsk.core.Application.get()
    ui = app.userInterface if app else None
    return app, ui


def _input_box_str(ui, prompt, title, default):
    """Returns (text_or_None, cancelled). Fusion may return (value, cancelled) or (cancelled, value)."""
    r = ui.inputBox(prompt, title, default)
    if r is None:
        return None, True
    try:
        if len(r) < 2:
            return None, True
    except Exception:
        return None, True
    a, b = r[0], r[1]
    if isinstance(a, bool):
        return (b, a)
    if isinstance(b, bool):
        return (a, b)
    return (str(a), False)


def _normalize_local_folder(path_str):
    s = path_str.strip().strip('"').strip("'")
    return os.path.normpath(os.path.abspath(s))


def load_last_folder_paths():
    """Returns dict with optional keys: source, output_root, log_root."""
    try:
        if not os.path.isfile(LAST_PATHS_FILE):
            return {}
        with open(LAST_PATHS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for key in ('source', 'output_root', 'log_root'):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                out[key] = _normalize_local_folder(v)
        return out
    except Exception:
        return {}


def save_last_folder_paths(source_dir, output_root, log_root):
    try:
        payload = {
            'source': _normalize_local_folder(source_dir),
            'output_root': _normalize_local_folder(output_root),
            'log_root': _normalize_local_folder(log_root),
        }
        with open(LAST_PATHS_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except Exception:
        pass


def _parse_positive_int(text, default_value):
    s = text.strip() if text else ''
    if not s:
        s = str(default_value)
    n = int(s, 10)
    if n < 1:
        raise ValueError('Must be a positive integer.')
    return n


def _parse_nonneg_int(text, default_value):
    s = text.strip() if text else ''
    if not s:
        s = str(default_value)
    n = int(s, 10)
    if n < 0:
        raise ValueError('Must be zero or a positive integer.')
    return n


def prompt_batch_settings(ui):
    """
    Returns {'batch_size': int, 'max_batches': int} or None if cancelled/invalid.
    max_batches 0 means no limit (process until CSV shows no remaining files).
    """
    if not ui:
        return {'batch_size': BATCH_SIZE, 'max_batches': MAX_BATCHES_PER_RUN}

    val, cancelled = _input_box_str(
        ui,
        'How many files to process in each batch?\n'
        '(Already-logged paths in filter_log.csv are always skipped.)',
        'Dataset filter — batch size',
        str(BATCH_SIZE),
    )
    if cancelled:
        return None
    try:
        batch_size = _parse_positive_int(val, BATCH_SIZE)
    except ValueError as e:
        ui.messageBox(str(e))
        return None

    val, cancelled = _input_box_str(
        ui,
        'Maximum batches this run:\n'
        '0 = continue until every file is either done or skipped by the CSV log.',
        'Dataset filter — max batches',
        str(MAX_BATCHES_PER_RUN),
    )
    if cancelled:
        return None
    try:
        max_batches = _parse_nonneg_int(val, MAX_BATCHES_PER_RUN)
    except ValueError as e:
        ui.messageBox(str(e))
        return None

    return {'batch_size': batch_size, 'max_batches': max_batches}


def prompt_filter_paths(ui):
    """
    Ask for input folder, output folder (classified files), and log folder (CSV + run_log).
    Returns paths dict from make_paths, or None if cancelled/invalid.
    Remembers the last successful three paths in LAST_PATHS_FILE.
    """
    if not ui:
        return None

    last = load_last_folder_paths()
    default_source = last.get('source') or DEFAULT_SOURCE_MODEL_DIR
    default_output = last.get('output_root') or DEFAULT_OUTPUT_ROOT

    val, cancelled = _input_box_str(
        ui,
        'Folder containing models to filter (STEP, IGES, SAT, F3D, …):',
        'Dataset filter — input folder',
        default_source,
    )
    if cancelled:
        return None
    try:
        source_dir = _normalize_local_folder(val)
    except Exception:
        ui.messageBox('Invalid input folder path.')
        return None
    if not os.path.isdir(source_dir):
        ui.messageBox(f'Input folder does not exist or is not a directory:\n{source_dir}')
        return None

    val, cancelled = _input_box_str(
        ui,
        'Output folder — medium_complexity, high_complexity, and rejected subfolders are created here:',
        'Dataset filter — output folder',
        default_output,
    )
    if cancelled:
        return None
    try:
        output_root = _normalize_local_folder(val)
    except Exception:
        ui.messageBox('Invalid output folder path.')
        return None

    val, cancelled = _input_box_str(
        ui,
        'Log folder — filter_log.csv and run_log.txt are written here:\n'
        '(can be the same as the output folder)',
        'Dataset filter — log folder',
        output_root,
    )
    if cancelled:
        return None
    try:
        log_root = _normalize_local_folder(val)
    except Exception:
        ui.messageBox('Invalid log folder path.')
        return None

    save_last_folder_paths(source_dir, output_root, log_root)
    return make_paths(source_dir, output_root, log_root)


def log_line(app, text, paths):
    ensure_dir(paths['log_root'])
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{stamp}] {text}'
    try:
        if app and SHOW_PROGRESS_IN_TEXT:
            app.log(line)
    except Exception:
        pass
    try:
        with open(paths['run_log_txt'], 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def get_file_ext(path):
    return os.path.splitext(path)[1].lower()


def list_model_files(folder):
    """Only files we can import with import_model_into_component."""
    if not os.path.isdir(folder):
        return []
    files = []
    for name in os.listdir(folder):
        ext = os.path.splitext(name)[1].lower()
        if ext in DIRECT_3D_IMPORT_EXTS:
            files.append(os.path.join(folder, name))
    files.sort()
    return files


def safe_copy_or_move(src, dst_dir):
    ensure_dir(dst_dir)
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        return dst
    if COPY_INSTEAD_OF_MOVE:
        shutil.copy2(src, dst)
    else:
        shutil.move(src, dst)
    return dst


def import_model_into_component(app, target_comp, file_path):
    import_mgr = app.importManager
    ext = get_file_ext(file_path)

    if ext in ('.step', '.stp', '.ste'):
        opts = import_mgr.createSTEPImportOptions(file_path)
    elif ext in ('.sat', '.sab'):
        opts = import_mgr.createSATImportOptions(file_path)
    elif ext in ('.smt', '.smb'):
        opts = import_mgr.createSMTImportOptions(file_path)
    elif ext in ('.iges', '.igs', '.ige'):
        opts = import_mgr.createIGESImportOptions(file_path)
    elif ext == '.f3d':
        opts = import_mgr.createFusionArchiveImportOptions(file_path)
    else:
        raise RuntimeError(f'Unsupported direct 3D import type: {file_path}')

    try:
        opts.isViewFit = False
    except Exception:
        pass

    import_mgr.importToTarget(opts, target_comp)

# =========================================================
# CSV LOGGING / RESUME
# =========================================================

CSV_HEADERS = [
    'timestamp',
    'file_name',
    'file_path',
    'file_ext',
    'file_size_kb',
    'status',
    'bucket',
    'complexity',
    'solid_body_count',
    'total_faces',
    'max_diagonal',
    'avg_max_dim',
    'avg_min_dim',
    'message'
]


def ensure_csv_exists(paths):
    csv_path = paths['filter_log_csv']
    ensure_dir(paths['log_root'])
    if not os.path.exists(csv_path):
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
        return

    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(CSV_HEADERS)
        return

    header = rows[0]
    if 'complexity' in header:
        return

    body = rows[1:]
    idx_map = {name: i for i, name in enumerate(header)}
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for row in body:
            d = {}
            for name, i in idx_map.items():
                d[name] = row[i] if i < len(row) else ''
            if d.get('bucket') == 'good':
                d['complexity'] = 'legacy_good'
            elif d.get('bucket') == 'bad':
                d['bucket'] = 'rejected'
                if d.get('status') == 'bad':
                    d['status'] = 'rejected'
                d['complexity'] = ''
            else:
                d['complexity'] = ''
            writer.writerow([d.get(h, '') for h in CSV_HEADERS])


def append_csv_row(row_dict, paths):
    ensure_csv_exists(paths)
    with open(paths['filter_log_csv'], 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([row_dict.get(h, '') for h in CSV_HEADERS])


def load_processed_paths(paths):
    processed = set()
    csv_path = paths['filter_log_csv']
    if not os.path.exists(csv_path):
        return processed

    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                p = row.get('file_path', '').strip()
                if p:
                    processed.add(p)
    except Exception:
        pass
    return processed

# =========================================================
# GEOMETRY ANALYSIS
# =========================================================

def collect_bodies_recursive(comp):
    bodies = []

    for i in range(comp.bRepBodies.count):
        bodies.append(comp.bRepBodies.item(i))

    for i in range(comp.occurrences.count):
        occ = comp.occurrences.item(i)
        child = occ.component
        bodies.extend(collect_bodies_recursive(child))

    return bodies


def body_bbox_lengths(body):
    bb = body.boundingBox
    lx = abs(bb.maxPoint.x - bb.minPoint.x)
    ly = abs(bb.maxPoint.y - bb.minPoint.y)
    lz = abs(bb.maxPoint.z - bb.minPoint.z)
    dims = sorted([lx, ly, lz])
    return dims[0], dims[1], dims[2]


def body_diagonal(body):
    bb = body.boundingBox
    return bb.minPoint.distanceTo(bb.maxPoint)


def summarize_design(design):
    root = design.rootComponent
    all_bodies = collect_bodies_recursive(root)
    solid_bodies = [b for b in all_bodies if b.isSolid]

    total_faces = 0
    diagonals = []
    max_dims = []
    min_dims = []

    for b in solid_bodies:
        total_faces += b.faces.count
        diagonals.append(body_diagonal(b))
        mn, md, mx = body_bbox_lengths(b)
        min_dims.append(mn)
        max_dims.append(mx)

    return {
        'all_body_count': len(all_bodies),
        'solid_body_count': len(solid_bodies),
        'total_faces': total_faces,
        'max_diagonal': max(diagonals) if diagonals else 0.0,
        'avg_max_dim': sum(max_dims) / len(max_dims) if max_dims else 0.0,
        'avg_min_dim': sum(min_dims) / len(min_dims) if min_dims else 0.0,
    }

# =========================================================
# VALIDATION
# =========================================================

def file_size_kb(path):
    return os.path.getsize(path) / 1024.0


def file_size_ok(path):
    kb = file_size_kb(path)
    return MIN_FILE_KB <= kb <= MAX_FILE_KB


def passes_base_eligibility(stats):
    solid_count = stats['solid_body_count']
    total_faces = stats['total_faces']
    max_diag = stats['max_diagonal']

    if solid_count < MIN_SOLID_BODIES:
        return False
    if solid_count > MAX_SOLID_BODIES:
        return False
    if total_faces < MIN_TOTAL_FACES:
        return False
    if total_faces > MAX_TOTAL_FACES:
        return False
    if max_diag < MIN_DIAGONAL_CM:
        return False
    if max_diag > MAX_DIAGONAL_CM:
        return False

    return True


def complexity_tier(stats):
    """
    Return 'medium' or 'high' if stats pass base eligibility; None if not eligible.
    """
    if not passes_base_eligibility(stats):
        return None

    if (
        stats['total_faces'] <= MEDIUM_MAX_TOTAL_FACES
        and stats['solid_body_count'] <= MEDIUM_MAX_SOLID_BODIES
        and stats['max_diagonal'] <= MEDIUM_MAX_DIAGONAL_CM
    ):
        return 'medium'
    return 'high'

# =========================================================
# SAFE FILE ANALYSIS
# =========================================================

def analyze_model_file(app, file_path):
    doc = None
    try:
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            raise RuntimeError('Active product is not a Fusion design.')

        root = design.rootComponent
        import_model_into_component(app, root, file_path)

        stats = summarize_design(design)
        return stats

    finally:
        if doc:
            try:
                doc.close(False)
            except Exception:
                pass

# =========================================================
# FILTER
# =========================================================

def next_unprocessed_batch(files, processed_paths, max_count):
    batch = []
    for p in files:
        if p not in processed_paths:
            batch.append(p)
        if len(batch) >= max_count:
            break
    return batch


def count_unprocessed(files, processed_paths):
    return sum(1 for p in files if p not in processed_paths)


def process_filter_batch(app, paths, batch):
    """Process one batch of files; each row is appended to CSV as we go (resume-safe)."""
    medium_count = 0
    high_count = 0
    rejected_count = 0

    for idx, path in enumerate(batch, 1):
        name = os.path.basename(path)
        log_line(app, f'BEGIN [{idx}/{len(batch)}] {name}', paths)

        try:
            status, stats = filter_one_file(app, path, paths)

            if status == 'medium':
                medium_count += 1
                if stats:
                    log_line(
                        app,
                        f'MEDIUM [{idx}/{len(batch)}] {name} | '
                        f'solids={stats["solid_body_count"]}, '
                        f'faces={stats["total_faces"]}, '
                        f'maxDiag={stats["max_diagonal"]:.2f}',
                        paths,
                    )
                else:
                    log_line(app, f'MEDIUM [{idx}/{len(batch)}] {name}', paths)
            elif status == 'high':
                high_count += 1
                if stats:
                    log_line(
                        app,
                        f'HIGH [{idx}/{len(batch)}] {name} | '
                        f'solids={stats["solid_body_count"]}, '
                        f'faces={stats["total_faces"]}, '
                        f'maxDiag={stats["max_diagonal"]:.2f}',
                        paths,
                    )
                else:
                    log_line(app, f'HIGH [{idx}/{len(batch)}] {name}', paths)
            else:
                rejected_count += 1
                if stats:
                    log_line(
                        app,
                        f'REJECTED [{idx}/{len(batch)}] {name} | '
                        f'solids={stats["solid_body_count"]}, '
                        f'faces={stats["total_faces"]}, '
                        f'maxDiag={stats["max_diagonal"]:.2f}',
                        paths,
                    )
                else:
                    log_line(app, f'REJECTED [{idx}/{len(batch)}] {name}', paths)

        except Exception as e:
            safe_copy_or_move(path, paths['rejected'])
            append_csv_row({
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'file_name': name,
                'file_path': path,
                'file_ext': get_file_ext(path),
                'file_size_kb': f'{file_size_kb(path):.2f}',
                'status': 'rejected',
                'bucket': 'rejected',
                'complexity': '',
                'solid_body_count': '',
                'total_faces': '',
                'max_diagonal': '',
                'avg_max_dim': '',
                'avg_min_dim': '',
                'message': f'exception: {e}',
            }, paths)
            rejected_count += 1
            log_line(app, f'ERROR [{idx}/{len(batch)}] {name} | {e}', paths)

        adsk.doEvents()
        time.sleep(0.25)

        if idx % PAUSE_EVERY_N_FILES == 0:
            log_line(app, f'Cooldown pause after {idx} files...', paths)
            adsk.doEvents()
            time.sleep(PAUSE_SECONDS)

    return medium_count, high_count, rejected_count


def filter_one_file(app, file_path, paths):
    name = os.path.basename(file_path)
    ext = get_file_ext(file_path)
    size_kb = file_size_kb(file_path)

    base_row = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'file_name': name,
        'file_path': file_path,
        'file_ext': ext,
        'file_size_kb': f'{size_kb:.2f}',
        'status': '',
        'bucket': '',
        'complexity': '',
        'solid_body_count': '',
        'total_faces': '',
        'max_diagonal': '',
        'avg_max_dim': '',
        'avg_min_dim': '',
        'message': '',
    }

    if not file_size_ok(file_path):
        safe_copy_or_move(file_path, paths['rejected'])
        base_row['status'] = 'rejected'
        base_row['bucket'] = 'rejected'
        base_row['message'] = 'rejected_by_file_size'
        append_csv_row(base_row, paths)
        return 'rejected', None

    stats = analyze_model_file(app, file_path)

    base_row['solid_body_count'] = stats['solid_body_count']
    base_row['total_faces'] = stats['total_faces']
    base_row['max_diagonal'] = f'{stats["max_diagonal"]:.3f}'
    base_row['avg_max_dim'] = f'{stats["avg_max_dim"]:.3f}'
    base_row['avg_min_dim'] = f'{stats["avg_min_dim"]:.3f}'

    tier = complexity_tier(stats)
    if tier is None:
        safe_copy_or_move(file_path, paths['rejected'])
        base_row['status'] = 'rejected'
        base_row['bucket'] = 'rejected'
        base_row['message'] = 'rejected_by_geometry'
        append_csv_row(base_row, paths)
        return 'rejected', stats

    base_row['complexity'] = tier
    base_row['status'] = 'ok'

    if tier == 'medium':
        safe_copy_or_move(file_path, paths['medium'])
        base_row['bucket'] = 'medium_complexity'
        base_row['message'] = 'accepted_medium'
    else:
        safe_copy_or_move(file_path, paths['high'])
        base_row['bucket'] = 'high_complexity'
        base_row['message'] = 'accepted_high'

    append_csv_row(base_row, paths)
    return tier, stats


def filter_dataset(context):
    app, ui = app_and_ui()

    try:
        paths = prompt_filter_paths(ui)
        if not paths:
            return

        batch_cfg = prompt_batch_settings(ui)
        if not batch_cfg:
            return

        batch_size = batch_cfg['batch_size']
        max_batches = batch_cfg['max_batches']

        ensure_all_dirs(paths)
        ensure_csv_exists(paths)

        files = list_model_files(paths['source'])
        if not files:
            raise RuntimeError(
                f'No importable model files in: {paths["source"]}\n'
                f'Extensions: {", ".join(sorted(DIRECT_3D_IMPORT_EXTS))}',
            )

        processed_paths = load_processed_paths(paths)
        initial_remaining = count_unprocessed(files, processed_paths)

        if initial_remaining == 0:
            ui.messageBox(
                'Nothing to do.\n\n'
                'Every file under the input folder is already listed in the CSV '
                '(same log folder as this run). Those paths are skipped.\n\n'
                f'CSV log:\n{paths["filter_log_csv"]}',
            )
            return

        log_line(
            app,
            f'Filter session start. Input={paths["source"]}, output={paths["output_root"]}, '
            f'logs={paths["log_root"]} | Source files={len(files)}, '
            f'already in CSV (skipped)={len(processed_paths)}, '
            f'unprocessed={initial_remaining}, batch_size={batch_size}, '
            f'max_batches={max_batches if max_batches else "unlimited"}',
            paths,
        )

        run_medium = 0
        run_high = 0
        run_rejected = 0
        run_files = 0
        batch_num = 0
        stopped_early = False

        while True:
            processed_paths = load_processed_paths(paths)
            remaining = count_unprocessed(files, processed_paths)
            if remaining == 0:
                break

            batch_num += 1
            if max_batches > 0 and batch_num > max_batches:
                stopped_early = True
                log_line(
                    app,
                    f'Stopping: reached max_batches={max_batches} '
                    f'({remaining} file(s) still unprocessed — run again to continue).',
                    paths,
                )
                break

            batch = next_unprocessed_batch(files, processed_paths, batch_size)
            if not batch:
                break

            log_line(
                app,
                f'--- Batch {batch_num}: {len(batch)} file(s), '
                f'{remaining} unprocessed before this batch ---',
                paths,
            )

            m, h, r = process_filter_batch(app, paths, batch)
            run_medium += m
            run_high += h
            run_rejected += r
            run_files += len(batch)

            processed_paths = load_processed_paths(paths)
            remaining_after = count_unprocessed(files, processed_paths)
            log_line(
                app,
                f'--- Batch {batch_num} done: medium={m}, high={h}, rejected={r} | '
                f'unprocessed remaining={remaining_after} ---',
                paths,
            )

            if remaining_after == 0:
                break

            if PAUSE_BETWEEN_BATCHES_SEC > 0:
                adsk.doEvents()
                time.sleep(PAUSE_BETWEEN_BATCHES_SEC)

        total_rows = len(load_processed_paths(paths))
        remaining_final = count_unprocessed(files, load_processed_paths(paths))

        summary = (
            'Filter session complete.\n\n'
            f'Batches run: {batch_num}\n'
            f'Files touched this session: {run_files}\n'
            f'Medium (session): {run_medium}\n'
            f'High (session): {run_high}\n'
            f'Rejected (session): {run_rejected}\n\n'
            f'Unprocessed still in input folder: {remaining_final}\n'
            f'Total paths in CSV: {total_rows}\n\n'
        )
        if stopped_early and remaining_final > 0:
            summary += (
                'Stopped at your max batch limit. Run the script again with the same '
                'folders — files already in the CSV stay skipped.\n\n'
            )
        summary += (
            f'Input:\n{paths["source"]}\n\n'
            f'Medium folder:\n{paths["medium"]}\n\n'
            f'High folder:\n{paths["high"]}\n\n'
            f'Rejected folder:\n{paths["rejected"]}\n\n'
            f'Run log:\n{paths["run_log_txt"]}\n\n'
            f'CSV log:\n{paths["filter_log_csv"]}'
        )
        ui.messageBox(summary)

    except Exception:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def run(context):
    filter_dataset(context)
