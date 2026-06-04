import base64
from io import BytesIO
import json

import os
from pathlib import Path
from uuid import uuid4

import numpy as np
import requests

from models import db, EncounterMap
from services.audit_service import log_audit_event
from services.world_service import clean_text


OPENAI_IMAGE_GENERATION_URL = 'https://api.openai.com/v1/images/generations'
OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
DEFAULT_IMAGE_MODEL = 'gpt-image-2'
DEFAULT_IMAGE_SIZE = '1024x1024'
DEFAULT_IMAGE_QUALITY = 'medium'
DEFAULT_IMAGE_TIMEOUT_SECONDS = 240
DEFAULT_IMAGE_QA_MODEL = 'gpt-5.4'
DEFAULT_IMAGE_QA_THRESHOLD = 8
DEFAULT_IMAGE_QA_MAX_RETRIES = 1
DEFAULT_IMAGE_QA_TIMEOUT_SECONDS = 90
DEFAULT_VTT_SETUP_MODEL = 'gpt-5.4'
DEFAULT_VTT_SETUP_TIMEOUT_SECONDS = 90
DEFAULT_IMAGE_GRID_MAX_RETRIES = 2
MIN_GRID_CONFIDENCE = 0.45
TERRAIN_KINDS = {'clear', 'blocked', 'difficult', 'cover', 'hazard', 'water', 'elevation', 'door', 'chokepoint'}


def encounter_map_storage_dir():
    default_dir = Path(__file__).resolve().parents[1] / 'instance' / 'encounter_maps'
    return Path(os.environ.get('ENCOUNTER_MAP_STORAGE_DIR') or default_dir)


def encounter_map_path(encounter_map):
    return encounter_map_storage_dir() / encounter_map.image_filename


def encounter_map_labeled_path(encounter_map):
    if not encounter_map.labeled_image_filename:
        return None
    return encounter_map_storage_dir() / encounter_map.labeled_image_filename


def latest_encounter_map(campaign_id):
    return (
        EncounterMap.query.filter_by(campaign_id=campaign_id, is_archived=False)
        .order_by(EncounterMap.created_at.desc(), EncounterMap.id.desc())
        .first()
    )


def openai_image_timeout_seconds():
    try:
        return max(30, int(os.environ.get('OPENAI_IMAGE_TIMEOUT_SECONDS', DEFAULT_IMAGE_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_TIMEOUT_SECONDS


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def _env_int(name, default, minimum=0, maximum=None):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _build_map_prompt(title, map_prompt, terrain='', tactical_features='', mood=''):
    parts = [
        'Create a VTT-ready Dungeons & Dragons battle map asset.',
        'Use a battlemap/cartography style, not an environmental illustration or scenic painting.',
        'Use a strict top-down orthographic camera, like a flat tabletop map, with no cinematic perspective or angled building facades.',
        'Make the full image a playable map surface from edge to edge; avoid postcard composition, horizon lighting, deep shadows, and decorative foreground framing.',
        'Design the map around the grid from the beginning, not as scenery with a grid overlay.',
        'The map must have a clean visible square tactical grid baked into the artwork, with straight evenly spaced grid lines aligned to the image edges.',
        'Every square should represent approximately 5 feet and remain readable across floors, roads, roofs, and terrain.',
        'Each grid cell should have an obvious gameplay meaning: clear, blocked, difficult terrain, cover, hazard, elevation, doorway, bridge, or water.',
        'Major terrain boundaries, paths, walls, fences, doors, streams, platforms, cover objects, hazards, and choke points should align to grid squares or half-squares whenever possible.',
        'Paths, bridges, stairs, ruins, walls, water edges, and cover should snap cleanly to grid lines or half-grid increments.',
        'Use clear square-based play zones: obvious open squares, blocked squares, difficult-terrain squares, cover squares, and movement lanes.',
        'Keep tactical contrast high: playable ground, blocked terrain, difficult terrain, and water should be visually distinct at a glance.',
        'Tree canopies and foliage should be simplified into readable terrain masses that occupy clear square footprints.',
        'Do not let canopy texture obscure grid intersections.',
        'Do not draw terrain details that visually cross many grid squares without a clear tactical purpose.',
        'Avoid painterly organic clutter that ignores the grid; make obstacles occupy readable square footprints that a DM can adjudicate quickly.',
        'Keep at least 60 percent of the grid squares visibly playable and unobscured.',
        'Use flat neutral lighting with no vignette, dramatic light beams, bloom, atmospheric haze, fog blobs, soft painterly overlays, or heavy shadows that hide grid intersections.',
        'Prefer clean modular VTT map design over cinematic realism.',
        'Do not include people, humanoids, NPCs, player characters, animals, monsters, corpses, silhouettes, portraits, tokens, or minis anywhere in the image.',
        'Do not include labels, words, UI, icons, legends, coordinates, watermarks, or text.',
        'Use simple readable terrain and clear tactical features: cover, obstacles, elevation, doors, paths, hazards, and open movement lanes.',
        'Keep props and set dressing sparse enough that virtual tabletop tokens can be placed on top without visual clutter.',
        f'Map title: {title}.',
        f'Scene request: {map_prompt}.',
    ]
    if terrain:
        parts.append(f'Terrain: {terrain}.')
    if tactical_features:
        parts.append(f'Tactical features: {tactical_features}.')
    if mood:
        parts.append(f'Mood and lighting: {mood}.')
    return '\n'.join(parts)


def _retry_prompt(base_prompt, qa_result):
    patch = clean_text(qa_result.get('retry_prompt_patch'), 1500)
    issues = qa_result.get('issues') if isinstance(qa_result.get('issues'), list) else []
    issue_text = '; '.join(clean_text(issue, 160) for issue in issues[:6] if clean_text(issue, 160))
    additions = [
        '',
        'VTT QA corrections from the previous generated image:',
    ]
    if issue_text:
        additions.append(f'- Fix these issues: {issue_text}.')
    if patch:
        additions.append(f'- Apply this correction patch exactly: {patch}')
    additions.append('- The regenerated image must satisfy the original VTT grid-first constraints.')
    return base_prompt + '\n'.join(additions)


def _grid_retry_prompt(base_prompt, grid=None, error=''):
    additions = [
        '',
        'Machine grid-detection corrections from the previous generated image:',
        '- Regenerate the map with a cleaner baked-in square grid that can be detected by software.',
        '- Use one consistent square size horizontally and vertically across the entire image.',
        '- Align all grid lines perfectly to the image edges with no perspective, warping, rotation, or broken line rhythm.',
        '- Increase grid line contrast enough that every vertical and horizontal line is visible through terrain, ruins, foliage, and shadows.',
        '- Avoid decorative seams, rubble rows, wall edges, or texture bands that look like a second competing grid.',
    ]
    if grid:
        additions.append(f'- Previous detected grid metadata was unreliable: {json.dumps(grid, sort_keys=True)}.')
    if error:
        additions.append(f'- Previous grid detection error: {clean_text(error, 300)}.')
    return base_prompt + '\n'.join(additions)


def _post_image_generation(api_key, prompt, model, size, quality, timeout_seconds):
    try:
        response = requests.post(
            OPENAI_IMAGE_GENERATION_URL,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'prompt': prompt,
                'n': 1,
                'size': size,
                'quality': quality,
            },
            timeout=timeout_seconds,
        )
    except requests.Timeout as err:
        raise RuntimeError(
            f'OpenAI image generation timed out after {timeout_seconds} seconds. '
            'Try again, or lower OPENAI_IMAGE_QUALITY for faster drafts.'
        ) from err
    response.raise_for_status()
    payload = response.json()
    images = payload.get('data') if isinstance(payload, dict) else None
    first = images[0] if isinstance(images, list) and images else {}
    b64_json = first.get('b64_json') if isinstance(first, dict) else None
    if not b64_json:
        raise RuntimeError('OpenAI image response did not include image data.')
    return base64.b64decode(b64_json), payload


def _extract_response_text(payload):
    if isinstance(payload, dict) and isinstance(payload.get('output_text'), str):
        return payload['output_text']
    output = payload.get('output') if isinstance(payload, dict) else []
    for item in output if isinstance(output, list) else []:
        content = item.get('content') if isinstance(item, dict) else []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get('type') in {'output_text', 'text'}:
                return part.get('text') or ''
    return ''


def _blur_1d(a, sigma=1.0):
    import cv2

    k = int(6 * sigma + 1)
    if k % 2 == 0:
        k += 1
    return cv2.GaussianBlur(a.astype(np.float32).reshape(1, -1), (k, 1), sigmaX=sigma).ravel()


def _default_grid_options(**overrides):
    return {
        'min_cell_px': 18.0,
        'max_cell_px': 150.0,
        'min_cells_x': 10,
        'min_cells_y': 10,
        'max_cells_x': None,
        'max_cells_y': None,
        'size_penalty': 0.75,
        'scoring_mode': 'adjusted',
        'harmonic_divisors': [2, 3, 4, 5],
        'top_n_candidates': 10,
        'prefer_smallest_plausible': True,
        'expected_cell_px': None,
        'expected_columns': None,
        'expected_rows': None,
        'expected_major_every': None,
        'bright_line_weight': 0.6,
        'refine_enabled': True,
        'refine_period_radius': 1.5,
        'refine_period_step': 0.05,
        'refine_offset_step': 0.5,
        **overrides,
    }


def _make_candidate(period, offset, raw_score, cell_count, harmonic_of=None, divisor=None, rejected=False, rejection_reason=None):
    return {
        'period_px': round(float(period), 3),
        'offset_px': round(float(offset), 3),
        'raw_score': round(float(raw_score), 3),
        'adjusted_score': 0.0,
        'cell_count': cell_count,
        'harmonic_of': harmonic_of,
        'divisor': divisor,
        'rejected': rejected,
        'rejection_reason': rejection_reason,
    }


def _score_grid_contrast(projection, period, offset, radius=2):
    n = len(projection)

    def sample_at(test_offset):
        idx = np.round(np.arange(test_offset, n, period)).astype(int)
        idx = idx[(idx >= 0) & (idx < n)]
        vals = []
        for i in idx:
            lo = max(0, i - radius)
            hi = min(n, i + radius + 1)
            vals.append(float(projection[lo:hi].max()))
        if not vals:
            return 0.0, 0
        energy = float(0.55 * np.median(vals) + 0.45 * np.mean(vals))
        return energy, len(vals)

    on_energy, count = sample_at(offset)

    off_samples = []
    for frac in (0.25, 0.5, 0.75):
        off_energy, _ = sample_at((offset + period * frac) % period)
        off_samples.append(off_energy)

    off_energy = float(np.median(off_samples))
    contrast = max(0.0, on_energy - off_energy)
    score = contrast + 0.20 * on_energy

    debug = {
        'on_grid_energy': on_energy,
        'off_grid_energy': off_energy,
        'contrast': contrast,
        'count': count,
    }

    return score, count, debug


def _confidence_from_contrast(debug):
    on_energy = debug.get('on_grid_energy', 0.0)
    off_energy = debug.get('off_grid_energy', 0.0)
    if on_energy <= 0:
        return 0.0
    return max(0.0, min(1.0, (on_energy - off_energy) / on_energy))


def _scan_top_periods(projection, min_period, max_period, p_step, o_step, top_n):
    n = len(projection)
    best_by_period = {}

    for period in np.arange(min_period, max_period + 0.001, p_step):
        best_score = -1.0
        best_offset = 0.0
        best_count = 0

        for offset in np.arange(0, period, o_step):
            score, count, _debug = _score_grid_contrast(projection, period, offset)
            if score > best_score:
                best_score = score
                best_offset = offset
                best_count = count

        if best_score >= 0:
            best_by_period[float(period)] = (best_offset, best_score, best_count)

    candidates = []
    for period, (offset, score, count) in best_by_period.items():
        candidates.append(_make_candidate(period, offset, score, count))

    candidates.sort(key=lambda c: c['raw_score'], reverse=True)
    return candidates[:top_n]


def _generate_subdivision_candidates(projection, base_candidate, divisors, options):
    subdivisions = []
    base_period = base_candidate['period_px']

    for d in divisors:
        sub_period = base_period / d
        if sub_period < options['min_cell_px'] or sub_period > options['max_cell_px']:
            continue

        base_offset = base_candidate['offset_px']
        best_score = -1.0
        best_offset = 0.0
        best_count = 0

        for k in range(d):
            test_offset = (base_offset + k * sub_period) % sub_period
            score, count, _debug = _score_grid_contrast(projection, sub_period, test_offset)
            if score > best_score:
                best_score = score
                best_offset = test_offset
                best_count = count

        if best_score >= 0:
            subdivisions.append(_make_candidate(
                sub_period, best_offset, best_score, best_count,
                harmonic_of=base_period, divisor=d,
            ))

    return subdivisions


def _compute_adjusted_score(candidate, options):
    if options['scoring_mode'] == 'raw':
        return candidate['raw_score']

    period = candidate['period_px']
    raw = candidate['raw_score']
    score = raw / (period ** options['size_penalty'])

    if options.get('expected_cell_px') is not None:
        expected = options['expected_cell_px']
        distance = abs(period - expected) / expected
        score *= max(0.1, 1.0 - distance)

    return score


def _validate_candidate(candidate, options, axis_length):
    period = candidate['period_px']
    cell_count = candidate['cell_count']

    if period < options['min_cell_px']:
        return False, 'period {:.1f} below min_cell_px {}'.format(period, options['min_cell_px'])
    if period > options['max_cell_px']:
        return False, 'period {:.1f} above max_cell_px {}'.format(period, options['max_cell_px'])

    if cell_count < options.get('min_cells', 3):
        return False, 'only {} cells, need at least {}'.format(cell_count, options.get('min_cells', 3))

    if options.get('max_cells') is not None and cell_count > options['max_cells']:
        return False, '{} cells exceeds max_cells {}'.format(cell_count, options['max_cells'])

    return True, None


def _fit_periodic_lines(projection, options, axis_length):
    notes = []

    base_candidates = _scan_top_periods(
        projection,
        options['min_cell_px'],
        options['max_cell_px'],
        p_step=0.1,
        o_step=0.5,
        top_n=options['top_n_candidates'],
    )

    if not base_candidates:
        raise RuntimeError('No grid candidates found.')

    all_candidates = list(base_candidates)
    for c in base_candidates:
        subs = _generate_subdivision_candidates(
            projection, c, options['harmonic_divisors'], options,
        )
        all_candidates.extend(subs)

    if options.get('expected_cell_px') is not None:
        expected = options['expected_cell_px']
        for test_period in np.arange(expected * 0.85, expected * 1.15 + 0.01, 0.5):
            best_score = -1.0
            best_offset = 0.0
            best_count = 0
            for offset in np.arange(0, test_period, 0.5):
                score, count, _debug = _score_grid_contrast(projection, test_period, offset)
                if score > best_score:
                    best_score = score
                    best_offset = offset
                    best_count = count
            if best_score >= 0:
                all_candidates.append(_make_candidate(test_period, best_offset, best_score, best_count))

    if options.get('expected_major_every') is not None:
        major = options['expected_major_every']
        for c in base_candidates:
            sub_period = c['period_px'] / major
            if options['min_cell_px'] <= sub_period <= options['max_cell_px']:
                best_score = -1.0
                best_offset = 0.0
                best_count = 0
                for k in range(major):
                    test_offset = (c['offset_px'] + k * sub_period) % sub_period
                    score, count, _debug = _score_grid_contrast(projection, sub_period, test_offset)
                    if score > best_score:
                        best_score = score
                        best_offset = test_offset
                        best_count = count
                if best_score >= 0:
                    all_candidates.append(_make_candidate(
                        sub_period, best_offset, best_score, best_count,
                        harmonic_of=c['period_px'], divisor=major,
                    ))

    for c in all_candidates:
        c['adjusted_score'] = round(_compute_adjusted_score(c, options), 6)

    valid = []
    rejected = []
    for c in all_candidates:
        is_valid, reason = _validate_candidate(c, options, axis_length)
        if is_valid:
            valid.append(c)
        else:
            c['rejected'] = True
            c['rejection_reason'] = reason
            rejected.append(c)

    if not valid:
        raw_best = sorted(all_candidates, key=lambda c: c['raw_score'], reverse=True)[:1]
        valid = raw_best
        for c in valid:
            c['rejected'] = False
            c['rejection_reason'] = None
        notes.append('All candidates rejected; falling back to best raw score candidate.')

    valid.sort(key=lambda c: c['adjusted_score'], reverse=True)

    if options.get('prefer_smallest_plausible', True) and len(valid) >= 2:
        best = valid[0]
        best_score = best['adjusted_score']
        threshold = best_score * 0.80
        smallest = min(
            (c for c in valid if c['adjusted_score'] >= threshold),
            key=lambda c: c['period_px'],
        )
        if smallest['period_px'] < best['period_px']:
            selected = smallest
            notes.append(
                'Preferring smaller period {:.1f}px over {:.1f}px '
                '(adjusted score {:.4f} >= {:.4f} threshold).'.format(
                    selected['period_px'], best['period_px'],
                    selected['adjusted_score'], threshold,
                )
            )
        else:
            selected = best
    else:
        selected = valid[0]

    if selected.get('harmonic_of'):
        notes.append(
            'Selected period is subdivision of harmonic period {:.1f}px (divisor {}).'.format(
                selected['harmonic_of'], selected['divisor'],
            )
        )

    rejected_large = []
    for c in all_candidates:
        if c.get('harmonic_of') is not None and c.get('rejected'):
            rejected_large.append(c)
    for c in base_candidates:
        if c.get('rejected') and c['period_px'] > selected['period_px'] * 1.3:
            if not any(r is c for r in rejected_large):
                rejected_large.append(c)

    return {
        'selected': selected,
        'top_candidates': valid[:options['top_n_candidates']],
        'rejected_large_harmonics': rejected_large[:options['top_n_candidates']],
        'notes': notes,
    }


def _refine_period_offset(projection, candidate, options):
    base_period = float(candidate['period_px'])

    best = None

    period_radius = options.get('refine_period_radius', 1.5)
    period_step = options.get('refine_period_step', 0.05)
    offset_step = options.get('refine_offset_step', 0.5)
    size_penalty = options.get('size_penalty', 0.75)

    for period in np.arange(
        base_period - period_radius,
        base_period + period_radius + 1e-9,
        period_step,
    ):
        if period <= 0:
            continue

        for offset in np.arange(0, period, offset_step):
            raw_score, count, debug = _score_grid_contrast(
                projection,
                period,
                offset,
            )

            if options.get('scoring_mode', 'adjusted') == 'raw':
                adjusted_score = raw_score
            else:
                adjusted_score = raw_score / (period ** size_penalty)

            if best is None or adjusted_score > best['adjusted_score']:
                best = dict(candidate)
                best['period_px'] = round(float(period), 3)
                best['offset_px'] = round(float(offset), 3)
                best['raw_score'] = round(float(raw_score), 3)
                best['adjusted_score'] = round(float(adjusted_score), 6)
                best['cell_count'] = count
                best['refined'] = True
                best['refinement_debug'] = debug
                best['pre_refine_period_px'] = candidate['period_px']
                best['pre_refine_offset_px'] = candidate['offset_px']
                best['pre_refine_adjusted_score'] = candidate.get('adjusted_score')

    return best or candidate


def _edge_line_supported(projection, offset, period, radius=2):
    prior = float(offset) - float(period)
    if projection is None or not (-radius <= prior < 0):
        return False

    n = len(projection)
    if n == 0:
        return False

    edge_energy = float(projection[:min(n, radius + 1)].max())
    line_center = int(round(offset))
    lo = max(0, line_center - radius)
    hi = min(n, line_center + radius + 1)
    line_energy = float(projection[lo:hi].max()) if lo < hi else 0.0

    return line_energy > 0 and edge_energy >= line_energy * 0.60


def _make_lines_from_offset(offset, period, limit, projection=None):
    lines = []
    p = 0.0 if _edge_line_supported(projection, offset, period) else float(offset)
    period = float(period)

    while p - period >= 0:
        p -= period

    while p <= limit:
        if p >= 0:
            lines.append(float(p))
        p += period

    if len(lines) < 2:
        return [float(offset), float(offset + period)]

    return lines


def detect_grid_from_image(image_bytes, grid_options=None):
    import cv2

    np_bytes = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_bytes, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError('Encounter map image could not be decoded for grid detection.')

    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    sobel_x = np.abs(cv2.Sobel(enhanced, cv2.CV_32F, 1, 0, ksize=3))
    sobel_y = np.abs(cv2.Sobel(enhanced, cv2.CV_32F, 0, 1, ksize=3))

    opts = _default_grid_options(**(grid_options or {}))
    bright_weight = opts.get('bright_line_weight', 0.6)

    blur = cv2.GaussianBlur(enhanced, (0, 0), 2)
    bright = np.maximum(enhanced.astype(np.float32) - blur.astype(np.float32), 0)

    proj_x = _blur_1d(sobel_x.sum(axis=0) + bright_weight * bright.sum(axis=0), sigma=1.0)
    proj_y = _blur_1d(sobel_y.sum(axis=1) + bright_weight * bright.sum(axis=1), sigma=1.0)

    min_cell = max(18.0, min(w, h) / 50.0)
    max_cell = min(w, h) / 6.0

    x_opts = dict(opts)
    x_opts['min_cell_px'] = max(x_opts.get('min_cell_px', min_cell), min_cell)
    x_opts['max_cell_px'] = min(x_opts.get('max_cell_px', max_cell), max_cell)
    x_opts['min_cells'] = x_opts.pop('min_cells_x', 10)
    x_opts['max_cells'] = x_opts.pop('max_cells_x', None)
    x_expected_cols = x_opts.pop('expected_columns', None)
    if x_expected_cols is not None:
        x_opts['expected_cell_px'] = x_opts.get('expected_cell_px') or (w / x_expected_cols)

    y_opts = dict(opts)
    y_opts['min_cell_px'] = max(y_opts.get('min_cell_px', min_cell), min_cell)
    y_opts['max_cell_px'] = min(y_opts.get('max_cell_px', max_cell), max_cell)
    y_opts['min_cells'] = y_opts.pop('min_cells_y', 10)
    y_opts['max_cells'] = y_opts.pop('max_cells_y', None)
    y_expected_rows = y_opts.pop('expected_rows', None)
    if y_expected_rows is not None:
        y_opts['expected_cell_px'] = y_opts.get('expected_cell_px') or (h / y_expected_rows)

    fit_x = _fit_periodic_lines(proj_x, x_opts, w)
    fit_y = _fit_periodic_lines(proj_y, y_opts, h)

    sel_x = fit_x['selected']
    sel_y = fit_y['selected']
    pre_refine_sel_x = dict(sel_x)
    pre_refine_sel_y = dict(sel_y)

    if opts.get('refine_enabled', True):
        sel_x = _refine_period_offset(proj_x, sel_x, x_opts)
        sel_y = _refine_period_offset(proj_y, sel_y, y_opts)

    origin_x = float(sel_x['offset_px'])
    origin_y = float(sel_y['offset_px'])

    xs = _make_lines_from_offset(origin_x, sel_x['period_px'], w, proj_x)
    ys = _make_lines_from_offset(origin_y, sel_y['period_px'], h, proj_y)

    cols = max(0, len(xs) - 1)
    rows = max(0, len(ys) - 1)

    cell_x = round(float(sel_x['period_px']), 3)
    cell_y = round(float(sel_y['period_px']), 3)
    cell_avg = round(float((cell_x + cell_y) / 2), 3)
    cell_legacy = int(round(cell_avg))
    spacing_delta = abs(cell_x - cell_y) / max(cell_x, cell_y)

    x_debug = sel_x.get('refinement_debug') or sel_x.get('debug') or {}
    y_debug = sel_y.get('refinement_debug') or sel_y.get('debug') or {}
    x_conf = _confidence_from_contrast(x_debug)
    y_conf = _confidence_from_contrast(y_debug)
    confidence = min(x_conf, y_conf) * max(0.0, 1.0 - spacing_delta)

    warnings = []
    if spacing_delta > 0.08:
        warnings.append('Horizontal and vertical grid spacing differ slightly.')
    if cols < 4 or rows < 4:
        warnings.append('Fewer than 4 cells detected on one axis.')
    if sel_x.get('harmonic_of'):
        warnings.append(
            'x-axis selected period {:.1f}px is subdivision of harmonic {:.1f}px.'.format(
                sel_x['period_px'], sel_x['harmonic_of'],
            )
        )
    if sel_y.get('harmonic_of'):
        warnings.append(
            'y-axis selected period {:.1f}px is subdivision of harmonic {:.1f}px.'.format(
                sel_y['period_px'], sel_y['harmonic_of'],
            )
        )
    if sel_x.get('refined'):
        warnings.append(
            'x-axis refined from {:.2f}px to {:.2f}px, offset {:.2f} to {:.2f}.'.format(
                pre_refine_sel_x['period_px'], sel_x['period_px'],
                pre_refine_sel_x['offset_px'], sel_x['offset_px'],
            )
        )
    if sel_y.get('refined'):
        warnings.append(
            'y-axis refined from {:.2f}px to {:.2f}px, offset {:.2f} to {:.2f}.'.format(
                pre_refine_sel_y['period_px'], sel_y['period_px'],
                pre_refine_sel_y['offset_px'], sel_y['offset_px'],
            )
        )

    return {
        'origin_px': {
            'x': round(float(xs[0]), 3),
            'y': round(float(ys[0]), 3),
        },
        'cell_size_px': {
            'x': cell_x,
            'y': cell_y,
            'average': cell_avg,
        },
        'cell_size_px_legacy': cell_legacy,
        'columns': cols,
        'rows': rows,
        'rotation_degrees': 0,
        'confidence': round(float(confidence), 3),
        'axis_confidence': {
            'x': round(float(x_conf), 3),
            'y': round(float(y_conf), 3),
        },
        'warnings': warnings,
        'selected_x_candidate': sel_x,
        'selected_y_candidate': sel_y,
        'pre_refine_x_candidate': pre_refine_sel_x,
        'pre_refine_y_candidate': pre_refine_sel_y,
        'refinement_enabled': opts.get('refine_enabled', True),
        'top_x_candidates': fit_x['top_candidates'],
        'top_y_candidates': fit_y['top_candidates'],
        'rejected_large_harmonics': fit_x['rejected_large_harmonics'] + fit_y['rejected_large_harmonics'],
        'detection_notes': fit_x['notes'] + fit_y['notes'],
    }


def _load_font(size, bold=False):
    from PIL import ImageFont

    candidates = [
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial Bold.ttf' if bold else '/Library/Fonts/Arial.ttf',
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            pass
    return ImageFont.load_default()


def create_labeled_grid_image(image_bytes, grid, output_path):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(BytesIO(image_bytes)).convert('RGBA')
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    origin = grid['origin_px']
    raw_cell = grid['cell_size_px']
    if isinstance(raw_cell, dict):
        cell_x = float(raw_cell['x'])
        cell_y = float(raw_cell['y'])
    else:
        cell_x = cell_y = float(raw_cell)
    columns = int(grid['columns'])
    rows = int(grid['rows'])
    width, height = image.size
    major_every = grid.get('major_every', 5)
    font_cell = ImageFont.load_default()
    font_report = _load_font(15)

    for col in range(columns + 1):
        x = int(round(origin['x'] + col * cell_x))
        if 0 <= x < width:
            is_major = (col % major_every == 0) if major_every > 0 else False
            color = (255, 235, 120, 220) if is_major else (255, 255, 255, 155)
            line_width = 2 if is_major else 1
            draw.line([(x, 0), (x, height)], fill=color, width=line_width)
    for row in range(rows + 1):
        y = int(round(origin['y'] + row * cell_y))
        if 0 <= y < height:
            is_major = (row % major_every == 0) if major_every > 0 else False
            color = (255, 235, 120, 220) if is_major else (255, 255, 255, 155)
            line_width = 2 if is_major else 1
            draw.line([(0, y), (width, y)], fill=color, width=line_width)

    for row in range(rows):
        for col in range(columns):
            cx = origin['x'] + col * cell_x + cell_x / 2.0
            cy = origin['y'] + row * cell_y + cell_y / 2.0
            if cx < 0 or cy < 0 or cx >= width or cy >= height:
                continue
            label = '{},{}'.format(col, row)
            bbox = draw.textbbox((0, 0), label, font=font_cell)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x0, y0 = cx - tw / 2.0 - 2, cy - th / 2.0 - 1
            draw.rounded_rectangle(
                [x0, y0, x0 + tw + 4, y0 + th + 2],
                radius=2,
                fill=(0, 0, 0, 72),
            )
            draw.text((cx - tw / 2.0, cy - th / 2.0), label, fill=(255, 255, 255, 205), font=font_cell)

    if isinstance(raw_cell, dict):
        cell_report = 'cell {:.2f}x{:.2f}px'.format(cell_x, cell_y)
    else:
        cell_report = 'cell {}px'.format(int(raw_cell))

    report = [
        'Grid: {}x{}  |  {}  |  origin ({},{})'.format(
            columns, rows, cell_report, origin['x'], origin['y'],
        ),
        'confidence: {}'.format(grid.get('confidence', '?')),
    ]
    pad, line_h = 8, 20
    box_w = max(380, len(report[0]) * 9 + 20)
    box_h = pad * 2 + line_h * len(report)
    box_x, box_y = 16, height - box_h - 16
    draw.rounded_rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h],
        radius=6,
        fill=(0, 0, 0, 180),
        outline=(255, 235, 120, 220),
        width=2,
    )
    for i, line in enumerate(report):
        draw.text((box_x + pad, box_y + pad + i * line_h), line, fill=(255, 245, 210, 255), font=font_report)

    labeled = Image.alpha_composite(image, overlay).convert('RGB')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    labeled.save(output_path, format='PNG')
    buffer = BytesIO()
    labeled.save(buffer, format='PNG')
    return buffer.getvalue()


def _quality_review_schema():
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['pass', 'score', 'issues', 'retry_prompt_patch'],
        'properties': {
            'pass': {'type': 'boolean'},
            'score': {'type': 'integer', 'minimum': 1, 'maximum': 10},
            'issues': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': 8,
            },
            'retry_prompt_patch': {
                'type': 'string',
                'description': 'Concise prompt additions that would fix the observed VTT map issues.',
            },
        },
    }


def _normalize_quality_review(payload, threshold):
    raw_text = _extract_response_text(payload)
    try:
        result = json.loads(raw_text)
    except (TypeError, ValueError):
        result = {}
    score = result.get('score')
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 1
    score = max(1, min(10, score))
    issues = result.get('issues') if isinstance(result.get('issues'), list) else []
    retry_prompt_patch = clean_text(result.get('retry_prompt_patch'), 1500)
    passed = bool(result.get('pass')) and score >= threshold
    return {
        'pass': passed,
        'score': score,
        'issues': [clean_text(issue, 240) for issue in issues if clean_text(issue, 240)][:8],
        'retry_prompt_patch': retry_prompt_patch,
    }


def _review_map_quality(api_key, image_bytes, title, prompt, threshold):
    model = os.environ.get('OPENAI_IMAGE_QA_MODEL', DEFAULT_IMAGE_QA_MODEL).strip() or DEFAULT_IMAGE_QA_MODEL
    timeout_seconds = _env_int('OPENAI_IMAGE_QA_TIMEOUT_SECONDS', DEFAULT_IMAGE_QA_TIMEOUT_SECONDS, minimum=15)
    encoded = base64.b64encode(image_bytes).decode('ascii')
    review_prompt = (
        'You are a strict virtual tabletop battle map QA reviewer. '
        'Evaluate whether this image is ready to use as a D&D VTT battle map. '
        'Check for: no people/creatures/tokens/minis; strict top-down orthographic view; '
        'clean square grid; terrain designed around grid cells; clear open/blocked/difficult/cover/water zones; '
        'readable tactical contrast; minimal clutter; no cinematic lighting, haze, vignette, or text. '
        'Return JSON only. If it fails, retry_prompt_patch must be a concise instruction patch for regenerating the map.\n\n'
        f'Map title: {title}\n'
        f'Original prompt:\n{prompt}'
    )
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'input': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': review_prompt},
                        {
                            'type': 'input_image',
                            'image_url': f'data:image/png;base64,{encoded}',
                            'detail': 'low',
                        },
                    ],
                },
            ],
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'encounter_map_quality_review',
                    'schema': _quality_review_schema(),
                    'strict': True,
                },
            },
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    result = _normalize_quality_review(response.json(), threshold)
    result['model'] = model
    return result


def _vtt_setup_schema():
    rect_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['col', 'row', 'width', 'height'],
        'properties': {
            'col': {'type': 'integer', 'minimum': 0},
            'row': {'type': 'integer', 'minimum': 0},
            'width': {'type': 'integer', 'minimum': 1},
            'height': {'type': 'integer', 'minimum': 1},
        },
    }
    point_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['col', 'row'],
        'properties': {
            'col': {'type': 'integer', 'minimum': 0},
            'row': {'type': 'integer', 'minimum': 0},
        },
    }
    area_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['label', 'rect', 'description', 'confidence'],
        'properties': {
            'label': {'type': 'string'},
            'rect': rect_schema,
            'description': {'type': 'string'},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        },
    }
    obstacle_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': [
            'label',
            'kind',
            'shape_type',
            'rect',
            'polygon',
            'movement_effect',
            'cover_type',
            'description',
            'confidence',
        ],
        'properties': {
            'label': {'type': 'string'},
            'kind': {'type': 'string', 'enum': ['blocked', 'cover', 'door', 'elevation', 'hazard', 'object', 'wall']},
            'shape_type': {'type': 'string', 'enum': ['rect', 'polygon']},
            'rect': rect_schema,
            'polygon': {'type': 'array', 'items': point_schema, 'maxItems': 24},
            'movement_effect': {
                'type': 'string',
                'enum': [
                    'blocks_movement',
                    'provides_cover',
                    'costs_extra_movement',
                    'line_of_sight_blocker',
                    'interactive',
                    'hazardous',
                    'none',
                ],
            },
            'cover_type': {'type': 'string', 'enum': ['none', 'half', 'three_quarters', 'full']},
            'description': {'type': 'string'},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        },
    }
    terrain_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['kind', 'label', 'shape_type', 'rect', 'polygon', 'description', 'confidence'],
        'properties': {
            'kind': {'type': 'string', 'enum': sorted(TERRAIN_KINDS)},
            'label': {'type': 'string'},
            'shape_type': {'type': 'string', 'enum': ['rect', 'polygon']},
            'rect': rect_schema,
            'polygon': {'type': 'array', 'items': point_schema, 'maxItems': 24},
            'description': {'type': 'string'},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        },
    }
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': [
            'map_summary',
            'dm_setup_context',
            'friendly_spawn_boxes',
            'enemy_spawn_boxes',
            'terrain_zones',
            'obstacles',
            'tactical_notes',
        ],
        'properties': {
            'map_summary': {'type': 'string'},
            'dm_setup_context': {'type': 'string'},
            'friendly_spawn_boxes': {'type': 'array', 'items': area_schema, 'maxItems': 8},
            'enemy_spawn_boxes': {'type': 'array', 'items': area_schema, 'maxItems': 12},
            'terrain_zones': {'type': 'array', 'items': terrain_schema, 'maxItems': 40},
            'obstacles': {'type': 'array', 'items': obstacle_schema, 'maxItems': 40},
            'tactical_notes': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12},
        },
    }


def _clamp_int(value, minimum, maximum, default):
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _clamp_float(value, minimum=0.0, maximum=1.0, default=0.5):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _normalize_rect(rect, grid):
    rect = rect if isinstance(rect, dict) else {}
    columns = max(1, int(grid.get('columns') or 1))
    rows = max(1, int(grid.get('rows') or 1))
    col = _clamp_int(rect.get('col'), 0, columns - 1, 0)
    row = _clamp_int(rect.get('row'), 0, rows - 1, 0)
    width = _clamp_int(rect.get('width'), 1, columns - col, 1)
    height = _clamp_int(rect.get('height'), 1, rows - row, 1)
    return {'col': col, 'row': row, 'width': width, 'height': height}


def _normalize_area(area, grid, fallback_label):
    area = area if isinstance(area, dict) else {}
    return {
        'label': clean_text(area.get('label'), 80) or fallback_label,
        'rect': _normalize_rect(area.get('rect'), grid),
        'description': clean_text(area.get('description'), 300),
        'confidence': _clamp_float(area.get('confidence')),
    }


def _normalize_point(point, grid):
    point = point if isinstance(point, dict) else {}
    return {
        'col': _clamp_int(point.get('col'), 0, max(0, int(grid.get('columns') or 1) - 1), 0),
        'row': _clamp_int(point.get('row'), 0, max(0, int(grid.get('rows') or 1) - 1), 0),
    }


def _normalize_terrain_zone(zone, grid, index):
    zone = zone if isinstance(zone, dict) else {}
    kind = clean_text(zone.get('kind'), 40)
    if kind not in TERRAIN_KINDS:
        kind = 'clear'
    shape_type = clean_text(zone.get('shape_type'), 20)
    if shape_type not in {'rect', 'polygon'}:
        shape_type = 'rect'
    polygon = zone.get('polygon') if isinstance(zone.get('polygon'), list) else []
    return {
        'kind': kind,
        'label': clean_text(zone.get('label'), 100) or f'Terrain {index + 1}',
        'shape_type': shape_type,
        'rect': _normalize_rect(zone.get('rect'), grid),
        'polygon': [_normalize_point(point, grid) for point in polygon[:24]],
        'description': clean_text(zone.get('description'), 400),
        'confidence': _clamp_float(zone.get('confidence')),
    }


def _normalize_obstacle(obstacle, grid, index):
    obstacle = obstacle if isinstance(obstacle, dict) else {}
    kind = clean_text(obstacle.get('kind'), 40)
    if kind not in {'blocked', 'cover', 'door', 'elevation', 'hazard', 'object', 'wall'}:
        kind = 'object'
    shape_type = clean_text(obstacle.get('shape_type'), 20)
    if shape_type not in {'rect', 'polygon'}:
        shape_type = 'rect'
    movement_effect = clean_text(obstacle.get('movement_effect'), 40)
    if movement_effect not in {
        'blocks_movement',
        'provides_cover',
        'costs_extra_movement',
        'line_of_sight_blocker',
        'interactive',
        'hazardous',
        'none',
    }:
        movement_effect = 'none'
    cover_type = clean_text(obstacle.get('cover_type'), 40)
    if cover_type not in {'none', 'half', 'three_quarters', 'full'}:
        cover_type = 'none'
    polygon = obstacle.get('polygon') if isinstance(obstacle.get('polygon'), list) else []
    return {
        'label': clean_text(obstacle.get('label'), 100) or f'Obstacle {index + 1}',
        'kind': kind,
        'shape_type': shape_type,
        'rect': _normalize_rect(obstacle.get('rect'), grid),
        'polygon': [_normalize_point(point, grid) for point in polygon[:24]],
        'movement_effect': movement_effect,
        'cover_type': cover_type,
        'description': clean_text(obstacle.get('description'), 400),
        'confidence': _clamp_float(obstacle.get('confidence')),
    }


def _normalize_vtt_setup(payload, grid, setup_context=''):
    raw_text = _extract_response_text(payload)
    try:
        result = json.loads(raw_text)
    except (TypeError, ValueError) as err:
        raise RuntimeError('VTT setup response did not contain valid JSON.') from err
    if not isinstance(result, dict):
        raise RuntimeError('VTT setup response JSON was not an object.')
    friendly_areas = result.get('friendly_spawn_boxes')
    if not isinstance(friendly_areas, list):
        friendly_areas = result.get('player_start_areas') if isinstance(result.get('player_start_areas'), list) else []
    enemy_areas = result.get('enemy_spawn_boxes')
    if not isinstance(enemy_areas, list):
        enemy_areas = result.get('enemy_start_areas') if isinstance(result.get('enemy_start_areas'), list) else []
    terrain_zones = result.get('terrain_zones') if isinstance(result.get('terrain_zones'), list) else []
    obstacles = result.get('obstacles') if isinstance(result.get('obstacles'), list) else []
    tactical_notes = result.get('tactical_notes') if isinstance(result.get('tactical_notes'), list) else []
    friendly_spawn_boxes = [
        _normalize_area(area, grid, f'Friendly Spawn {index + 1}')
        for index, area in enumerate(friendly_areas[:8])
    ]
    enemy_spawn_boxes = [
        _normalize_area(area, grid, f'Enemy Spawn {index + 1}')
        for index, area in enumerate(enemy_areas[:12])
    ]
    return {
        'map_summary': clean_text(result.get('map_summary'), 500),
        'dm_setup_context': clean_text(result.get('dm_setup_context'), 1000) or clean_text(setup_context, 1000),
        'friendly_spawn_boxes': friendly_spawn_boxes,
        'enemy_spawn_boxes': enemy_spawn_boxes,
        'player_start_areas': friendly_spawn_boxes,
        'enemy_start_areas': enemy_spawn_boxes,
        'terrain_zones': [
            _normalize_terrain_zone(zone, grid, index)
            for index, zone in enumerate(terrain_zones[:40])
        ],
        'obstacles': [
            _normalize_obstacle(obstacle, grid, index)
            for index, obstacle in enumerate(obstacles[:40])
        ],
        'tactical_notes': [
            clean_text(note, 240)
            for note in tactical_notes
            if clean_text(note, 240)
        ][:12],
    }


def _request_vtt_setup(api_key, title, prompt, grid, original_image_bytes, labeled_image_bytes, setup_context=''):
    model = os.environ.get('OPENAI_IMAGE_SETUP_MODEL', DEFAULT_VTT_SETUP_MODEL).strip() or DEFAULT_VTT_SETUP_MODEL
    timeout_seconds = _env_int('OPENAI_IMAGE_SETUP_TIMEOUT_SECONDS', DEFAULT_VTT_SETUP_TIMEOUT_SECONDS, minimum=15)
    original_b64 = base64.b64encode(original_image_bytes).decode('ascii')
    labeled_b64 = base64.b64encode(labeled_image_bytes).decode('ascii')
    setup_prompt = (
        'You are preparing structured VTT encounter setup metadata from a D&D battle map for immediate play. '
        'Use the original image for visual terrain and the labeled image for exact grid coordinates. '
        'All coordinates must use zero-based grid cells where col=0,row=0 is the top-left playable grid cell. '
        'Honor the DM setup and placement instructions whenever they are compatible with the visible map. '
        'Create friendly_spawn_boxes for PCs/allies and enemy_spawn_boxes for enemy load-in groups. '
        'Spawn boxes must be rectangular grid areas large enough for likely tokens and should avoid blocked, hazardous, or impassable cells unless the DM explicitly asks otherwise. '
        'Classify important terrain zones only when they matter tactically: clear, blocked, difficult, cover, hazard, water, elevation, door, or chokepoint. '
        'Use obstacles for discrete objects and barriers such as walls, rubble piles, furniture, doors, barricades, cover, ledges, and blocking props. '
        'Give every spawn box, terrain zone, and obstacle a short table-ready label and a practical gameplay description. '
        'Use rectangles for simple areas and polygons only for irregular areas. '
        'Do not invent specific creatures, tokens, character names, hidden labels, treasure, or story content. Return JSON only.\n\n'
        f'Map title: {title}\n'
        f'Grid metadata: {json.dumps(grid, sort_keys=True)}\n'
        f'DM setup and placement instructions:\n{clean_text(setup_context, 1000) or "No explicit placement notes; infer practical play setup from the image and prompt."}\n'
        f'Original generation prompt:\n{prompt}'
    )
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'input': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': setup_prompt},
                        {
                            'type': 'input_image',
                            'image_url': f'data:image/png;base64,{original_b64}',
                            'detail': 'low',
                        },
                        {
                            'type': 'input_image',
                            'image_url': f'data:image/png;base64,{labeled_b64}',
                            'detail': 'high',
                        },
                    ],
                },
            ],
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'encounter_map_vtt_setup',
                    'schema': _vtt_setup_schema(),
                    'strict': True,
                },
            },
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    setup = _normalize_vtt_setup(response.json(), grid, setup_context)
    setup['model'] = model
    return setup


def build_vtt_setup_for_encounter_map(encounter_map, image_bytes, api_key, audit_context=None, precomputed_grid=None, setup_context=''):
    trace_id = (audit_context or {}).get('trace_id')
    trace_label = (audit_context or {}).get('trace_label')
    parent_trace_id = (audit_context or {}).get('parent_trace_id')
    grid = None
    labeled_filename = None
    try:
        grid = precomputed_grid or detect_grid_from_image(image_bytes)
        encounter_map.grid_json = json.dumps(grid, sort_keys=True)
        if grid['confidence'] < MIN_GRID_CONFIDENCE:
            raise RuntimeError(f'Grid detection confidence {grid["confidence"]} is below {MIN_GRID_CONFIDENCE}.')

        labeled_filename = f'campaign_{encounter_map.campaign_id}_map_{encounter_map.id}_labeled_{uuid4().hex}.png'
        labeled_path = encounter_map_storage_dir() / labeled_filename
        labeled_bytes = create_labeled_grid_image(image_bytes, grid, labeled_path)
        encounter_map.labeled_image_filename = labeled_filename

        setup = _request_vtt_setup(
            api_key,
            encounter_map.title,
            encounter_map.prompt,
            grid,
            image_bytes,
            labeled_bytes,
            setup_context,
        )
        encounter_map.vtt_setup_json = json.dumps(setup, sort_keys=True)
        encounter_map.setup_status = 'ready'
        encounter_map.setup_error = None
        log_audit_event(
            encounter_map.campaign_id,
            'encounter_map_vtt_setup_ready',
            f'Encounter map VTT setup ready: {encounter_map.title}',
            {
                'encounter_map_id': encounter_map.id,
                'session_id': encounter_map.session_id,
                'grid': grid,
                'setup_model': setup.get('model'),
            },
            source='encounter_maps',
            actor='session_dm',
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=False,
        )
        return {'status': 'ready', 'grid': grid, 'setup': setup}
    except Exception as err:
        encounter_map.setup_status = 'failed'
        encounter_map.setup_error = clean_text(str(err), 500) or err.__class__.__name__
        if grid is not None:
            encounter_map.grid_json = json.dumps(grid, sort_keys=True)
        if labeled_filename:
            encounter_map.labeled_image_filename = labeled_filename
        log_audit_event(
            encounter_map.campaign_id,
            'encounter_map_vtt_setup_failed',
            f'Encounter map VTT setup failed: {encounter_map.title}',
            {
                'encounter_map_id': encounter_map.id,
                'session_id': encounter_map.session_id,
                'error': encounter_map.setup_error,
                'grid': grid,
            },
            source='encounter_maps',
            actor='session_dm',
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=False,
        )
        return {'status': 'failed', 'error': encounter_map.setup_error, 'grid': grid}


def create_encounter_map(campaign, session, title, map_prompt, terrain='', tactical_features='', mood='', vtt_setup_notes='', audit_context=None):
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is required to generate encounter maps.')

    clean_title = clean_text(title, 200) or 'Encounter Map'
    clean_prompt = clean_text(map_prompt, 2000)
    if not clean_prompt:
        raise RuntimeError('A map prompt is required to generate an encounter map.')

    terrain = clean_text(terrain, 500)
    tactical_features = clean_text(tactical_features, 800)
    mood = clean_text(mood, 500)
    vtt_setup_notes = clean_text(vtt_setup_notes, 1000)
    final_prompt = _build_map_prompt(clean_title, clean_prompt, terrain, tactical_features, mood)
    model = os.environ.get('OPENAI_IMAGE_MODEL', DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    size = os.environ.get('OPENAI_IMAGE_SIZE', DEFAULT_IMAGE_SIZE).strip() or DEFAULT_IMAGE_SIZE
    quality = os.environ.get('OPENAI_IMAGE_QUALITY', DEFAULT_IMAGE_QUALITY).strip() or DEFAULT_IMAGE_QUALITY
    timeout_seconds = openai_image_timeout_seconds()
    qa_enabled = _env_bool('OPENAI_IMAGE_QA_ENABLED', True)
    qa_threshold = _env_int('OPENAI_IMAGE_QA_THRESHOLD', DEFAULT_IMAGE_QA_THRESHOLD, minimum=1, maximum=10)
    qa_max_retries = _env_int('OPENAI_IMAGE_QA_MAX_RETRIES', DEFAULT_IMAGE_QA_MAX_RETRIES, minimum=0, maximum=2)
    grid_validation_enabled = _env_bool('OPENAI_IMAGE_GRID_VALIDATION_ENABLED', True)
    grid_max_retries = _env_int('OPENAI_IMAGE_GRID_MAX_RETRIES', DEFAULT_IMAGE_GRID_MAX_RETRIES, minimum=0, maximum=5)

    trace_id = (audit_context or {}).get('trace_id')
    trace_label = (audit_context or {}).get('trace_label')
    parent_trace_id = (audit_context or {}).get('parent_trace_id')
    log_audit_event(
        campaign.id,
        'encounter_map_generation_requested',
        f'Encounter map requested: {clean_title}',
        {
            'session_id': session.id if session else None,
            'title': clean_title,
            'prompt': final_prompt,
            'model': model,
            'size': size,
            'quality': quality,
            'timeout_seconds': timeout_seconds,
            'qa_enabled': qa_enabled,
            'qa_threshold': qa_threshold,
            'qa_max_retries': qa_max_retries,
            'grid_validation_enabled': grid_validation_enabled,
            'grid_confidence_threshold': MIN_GRID_CONFIDENCE,
            'grid_max_retries': grid_max_retries,
            'vtt_setup_notes': vtt_setup_notes,
        },
        source='openai.images',
        actor='session_dm',
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='tools',
        commit=False,
    )

    current_prompt = final_prompt
    image_bytes = None
    payload = {}
    qa_result = None
    grid_result = None
    grid_error = None
    attempts = 0
    qa_retries_used = 0
    grid_retries_used = 0
    max_generation_attempts = 1 + (qa_max_retries if qa_enabled else 0) + (grid_max_retries if grid_validation_enabled else 0)
    while attempts < max_generation_attempts:
        attempts += 1
        image_bytes, payload = _post_image_generation(api_key, current_prompt, model, size, quality, timeout_seconds)

        if qa_enabled:
            try:
                qa_result = _review_map_quality(api_key, image_bytes, clean_title, current_prompt, qa_threshold)
            except Exception as err:
                log_audit_event(
                    campaign.id,
                    'encounter_map_quality_review_error',
                    f'Encounter map quality review failed: {clean_title}',
                    {
                        'session_id': session.id if session else None,
                        'title': clean_title,
                        'attempt': attempts,
                        'error': repr(err),
                    },
                    source='openai.responses',
                    actor='session_dm',
                    trace_id=trace_id,
                    parent_trace_id=parent_trace_id,
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=False,
                )
                qa_result = None
            else:
                log_audit_event(
                    campaign.id,
                    'encounter_map_quality_review',
                    f'Encounter map quality review: {clean_title}',
                    {
                        'session_id': session.id if session else None,
                        'title': clean_title,
                        'attempt': attempts,
                        'review': qa_result,
                        'threshold': qa_threshold,
                    },
                    source='openai.responses',
                    actor='session_dm',
                    trace_id=trace_id,
                    parent_trace_id=parent_trace_id,
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=False,
                )
                if not qa_result.get('pass') and qa_retries_used < qa_max_retries and attempts < max_generation_attempts:
                    qa_retries_used += 1
                    current_prompt = _retry_prompt(final_prompt, qa_result)
                    continue

        if grid_validation_enabled:
            grid_error = None
            try:
                grid_result = detect_grid_from_image(image_bytes)
                grid_passed = grid_result['confidence'] >= MIN_GRID_CONFIDENCE
            except Exception as err:
                grid_result = None
                grid_error = clean_text(str(err), 500) or err.__class__.__name__
                grid_passed = False

            log_audit_event(
                campaign.id,
                'encounter_map_grid_review',
                f'Encounter map grid review: {clean_title}',
                {
                    'session_id': session.id if session else None,
                    'title': clean_title,
                    'attempt': attempts,
                    'passed': grid_passed,
                    'grid': grid_result,
                    'error': grid_error,
                    'threshold': MIN_GRID_CONFIDENCE,
                    'grid_retries_used': grid_retries_used,
                },
                source='encounter_maps',
                actor='session_dm',
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=trace_label,
                audit_role='tools',
                commit=False,
            )

            if not grid_passed and grid_retries_used < grid_max_retries and attempts < max_generation_attempts:
                grid_retries_used += 1
                current_prompt = _grid_retry_prompt(final_prompt, grid_result, grid_error)
                continue

        if image_bytes:
            break

    filename = f'campaign_{campaign.id}_map_{uuid4().hex}.png'
    storage_dir = encounter_map_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / filename).write_bytes(image_bytes)

    encounter_map = EncounterMap(
        campaign_id=campaign.id,
        session_id=session.id if session else None,
        title=clean_title,
        prompt=current_prompt,
        image_filename=filename,
        model=model,
        size=size,
        quality=quality,
        setup_status='pending',
        created_by_tool=True,
    )
    db.session.add(encounter_map)
    db.session.flush()
    setup_result = build_vtt_setup_for_encounter_map(
        encounter_map,
        image_bytes,
        api_key,
        audit_context,
        precomputed_grid=grid_result if grid_validation_enabled else None,
        setup_context=vtt_setup_notes,
    )

    log_audit_event(
        campaign.id,
        'encounter_map_generated',
        f'Encounter map generated: {clean_title}',
        {
            'session_id': session.id if session else None,
            'encounter_map_id': encounter_map.id,
            'title': clean_title,
            'model': model,
            'size': size,
            'quality': quality,
            'generation_attempts': attempts,
            'qa_enabled': qa_enabled,
            'qa_result': qa_result,
            'grid_validation_enabled': grid_validation_enabled,
            'grid_retries_used': grid_retries_used,
            'grid_result': grid_result,
            'grid_error': grid_error,
            'setup_status': setup_result.get('status'),
            'setup_error': setup_result.get('error'),
            'usage': payload.get('usage') if isinstance(payload, dict) else {},
        },
        source='encounter_maps',
        actor='session_dm',
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='tools',
        commit=False,
    )
    return encounter_map
