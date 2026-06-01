BLOCKING_TERRAIN_KINDS = {'blocked', 'wall'}
DIFFICULT_TERRAIN_KINDS = {'difficult', 'water'}
BLOCKING_MOVEMENT_EFFECTS = {'blocks_movement'}
DIFFICULT_MOVEMENT_EFFECTS = {'costs_extra_movement'}
COVER_LEVEL_SCORES = {
    'none': 0,
    'half': 1,
    'three_quarters': 2,
    'full': 3,
}


def _point_in_polygon(points, x, y):
    if len(points) < 3:
        return False

    inside = False
    previous = points[-1]
    for current in points:
        try:
            x1 = float(previous.get('col'))
            y1 = float(previous.get('row'))
            x2 = float(current.get('col'))
            y2 = float(current.get('row'))
        except (TypeError, ValueError):
            previous = current
            continue

        if (y1 > y) != (y2 > y):
            x_intersection = ((x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9)) + x1
            if x < x_intersection:
                inside = not inside
        previous = current
    return inside


def _rect_contains_cell(rect, col, row):
    rect = rect if isinstance(rect, dict) else {}
    try:
        rect_col = int(rect.get('col'))
        rect_row = int(rect.get('row'))
        width = int(rect.get('width'))
        height = int(rect.get('height'))
    except (TypeError, ValueError):
        return False
    return rect_col <= col < rect_col + width and rect_row <= row < rect_row + height


def _area_contains_cell(area, col, row):
    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    if polygon:
        return _point_in_polygon(polygon, col + 0.5, row + 0.5)
    return _rect_contains_cell(area.get('rect'), col, row)


def _area_contains_point(area, x, y):
    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    if polygon:
        return _point_in_polygon(polygon, x, y)

    rect = area.get('rect') if isinstance(area, dict) else {}
    try:
        rect_col = float(rect.get('col'))
        rect_row = float(rect.get('row'))
        width = float(rect.get('width'))
        height = float(rect.get('height'))
    except (TypeError, ValueError, AttributeError):
        return False
    return rect_col <= x <= rect_col + width and rect_row <= y <= rect_row + height


def _area_dimensions(area):
    rect = area.get('rect') if isinstance(area, dict) else {}
    try:
        width = float(rect.get('width'))
        height = float(rect.get('height'))
    except (TypeError, ValueError, AttributeError):
        width = 0.0
        height = 0.0

    if width > 0 and height > 0:
        return width, height

    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    points = []
    for point in polygon:
        try:
            points.append((float(point.get('col')), float(point.get('row'))))
        except (TypeError, ValueError, AttributeError):
            continue
    if not points:
        return 0.0, 0.0

    cols = [point[0] for point in points]
    rows = [point[1] for point in points]
    return max(cols) - min(cols), max(rows) - min(rows)


def _infer_cover_type(area):
    kind = str(area.get('kind') or '').strip().lower()
    movement_effect = str(area.get('movement_effect') or '').strip().lower()
    cover_type = str(area.get('cover_type') or '').strip().lower()

    if cover_type in COVER_LEVEL_SCORES:
        return cover_type
    if kind in {'wall', 'blocked'} or movement_effect == 'blocks_movement':
        return 'full'
    if kind == 'cover' or movement_effect == 'provides_cover':
        return 'half'
    return 'none'


def _is_cover_candidate(area, group):
    kind = str(area.get('kind') or '').strip().lower()
    movement_effect = str(area.get('movement_effect') or '').strip().lower()
    cover_type = str(area.get('cover_type') or '').strip().lower()

    if cover_type in {'half', 'three_quarters', 'full'}:
        return True
    if kind in {'wall', 'cover'}:
        return True
    if movement_effect in {'provides_cover', 'blocks_movement'}:
        return True
    return group == 'terrain_zones' and kind == 'blocked'


def _is_precise_cover_provider(area, group):
    if not _is_cover_candidate(area, group):
        return False

    if group == 'obstacles':
        return True

    width, height = _area_dimensions(area)
    if width <= 0 or height <= 0:
        return False

    smaller_side = max(min(width, height), 1.0)
    larger_side = max(width, height)
    cell_area = width * height
    is_compact = cell_area <= 6
    is_narrow = smaller_side <= 1.25
    is_long_barrier = smaller_side <= 2.25 and (larger_side / smaller_side) >= 3.0
    return is_compact or is_narrow or is_long_barrier


def _cross_product(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _point_on_segment(ax, ay, bx, by, px, py):
    min_x = min(ax, bx) - 1e-9
    max_x = max(ax, bx) + 1e-9
    min_y = min(ay, by) - 1e-9
    max_y = max(ay, by) + 1e-9
    if not (min_x <= px <= max_x and min_y <= py <= max_y):
        return False
    return abs(_cross_product(ax, ay, bx, by, px, py)) <= 1e-9


def _segments_intersect(a1x, a1y, a2x, a2y, b1x, b1y, b2x, b2y):
    d1 = _cross_product(a1x, a1y, a2x, a2y, b1x, b1y)
    d2 = _cross_product(a1x, a1y, a2x, a2y, b2x, b2y)
    d3 = _cross_product(b1x, b1y, b2x, b2y, a1x, a1y)
    d4 = _cross_product(b1x, b1y, b2x, b2y, a2x, a2y)

    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    if abs(d1) <= 1e-9 and _point_on_segment(a1x, a1y, a2x, a2y, b1x, b1y):
        return True
    if abs(d2) <= 1e-9 and _point_on_segment(a1x, a1y, a2x, a2y, b2x, b2y):
        return True
    if abs(d3) <= 1e-9 and _point_on_segment(b1x, b1y, b2x, b2y, a1x, a1y):
        return True
    if abs(d4) <= 1e-9 and _point_on_segment(b1x, b1y, b2x, b2y, a2x, a2y):
        return True
    return False


def _segment_intersects_rect(area, start_x, start_y, end_x, end_y):
    rect = area.get('rect') if isinstance(area, dict) else {}
    try:
        left = float(rect.get('col'))
        top = float(rect.get('row'))
        width = float(rect.get('width'))
        height = float(rect.get('height'))
    except (TypeError, ValueError, AttributeError):
        return False

    if width <= 0 or height <= 0:
        return False

    right = left + width
    bottom = top + height
    if _area_contains_point(area, start_x, start_y) or _area_contains_point(area, end_x, end_y):
        return True

    edges = (
        (left, top, right, top),
        (right, top, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, top),
    )
    return any(
        _segments_intersect(start_x, start_y, end_x, end_y, edge[0], edge[1], edge[2], edge[3])
        for edge in edges
    )


def _segment_intersects_polygon(area, start_x, start_y, end_x, end_y):
    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    if len(polygon) < 3:
        return False
    if _area_contains_point(area, start_x, start_y) or _area_contains_point(area, end_x, end_y):
        return True

    previous = polygon[-1]
    for current in polygon:
        try:
            x1 = float(previous.get('col'))
            y1 = float(previous.get('row'))
            x2 = float(current.get('col'))
            y2 = float(current.get('row'))
        except (TypeError, ValueError, AttributeError):
            previous = current
            continue
        if _segments_intersect(start_x, start_y, end_x, end_y, x1, y1, x2, y2):
            return True
        previous = current
    return False


def _segment_intersects_area(area, start_x, start_y, end_x, end_y):
    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    if len(polygon) >= 3:
        return _segment_intersects_polygon(area, start_x, start_y, end_x, end_y)
    return _segment_intersects_rect(area, start_x, start_y, end_x, end_y)


def _cell_profile(vtt_setup, col, row):
    profile = {
        'blocked': False,
        'cost': 1,
        'blocked_by': None,
        'difficult_by': None,
    }
    if not isinstance(vtt_setup, dict):
        return profile

    for group in ('terrain_zones', 'obstacles'):
        areas = vtt_setup.get(group) if isinstance(vtt_setup.get(group), list) else []
        for area in areas:
            if not isinstance(area, dict) or not _area_contains_cell(area, col, row):
                continue

            kind = str(area.get('kind') or '').strip().lower()
            movement_effect = str(area.get('movement_effect') or '').strip().lower()
            label = str(area.get('label') or '').strip() or 'map feature'

            if kind in BLOCKING_TERRAIN_KINDS or movement_effect in BLOCKING_MOVEMENT_EFFECTS:
                profile['blocked'] = True
                profile['blocked_by'] = label
            if kind in DIFFICULT_TERRAIN_KINDS or movement_effect in DIFFICULT_MOVEMENT_EFFECTS:
                profile['cost'] = max(profile['cost'], 2)
                profile['difficult_by'] = label

    return profile


def movement_grid(vtt_setup, columns, rows):
    return [
        [_cell_profile(vtt_setup, col, row) for col in range(columns)]
        for row in range(rows)
    ]


def reachable_cells(vtt_setup, columns, rows, start_col, start_row, max_squares):
    if columns <= 0 or rows <= 0 or max_squares < 0:
        return {}
    if not (0 <= start_col < columns and 0 <= start_row < rows):
        return {}

    grid = movement_grid(vtt_setup, columns, rows)
    distances = {(start_col, start_row): 0}
    queue = [(0, start_col, start_row)]
    directions = (
        (-1, -1), (0, -1), (1, -1),
        (-1, 0),           (1, 0),
        (-1, 1),  (0, 1),  (1, 1),
    )

    while queue:
        queue.sort(reverse=True)
        spent, col, row = queue.pop()
        if spent != distances.get((col, row)):
            continue

        for dx, dy in directions:
            next_col = col + dx
            next_row = row + dy
            if not (0 <= next_col < columns and 0 <= next_row < rows):
                continue
            if grid[next_row][next_col]['blocked']:
                continue
            if dx and dy and (
                grid[row][next_col]['blocked'] or grid[next_row][col]['blocked']
            ):
                continue

            next_spent = spent + grid[next_row][next_col]['cost']
            key = (next_col, next_row)
            if next_spent > max_squares or next_spent >= distances.get(key, max_squares + 1):
                continue
            distances[key] = next_spent
            queue.append((next_spent, next_col, next_row))

    return distances


def evaluate_cover(vtt_setup, attacker_col, attacker_row, target_col, target_row):
    result = {
        'cover_type': 'none',
        'cover_score': 0,
        'providers': [],
    }
    if not isinstance(vtt_setup, dict):
        return result

    start_x = float(attacker_col) + 0.5
    start_y = float(attacker_row) + 0.5
    end_x = float(target_col) + 0.5
    end_y = float(target_row) + 0.5

    providers = []
    for group in ('terrain_zones', 'obstacles'):
        areas = vtt_setup.get(group) if isinstance(vtt_setup.get(group), list) else []
        for index, area in enumerate(areas):
            if not isinstance(area, dict) or not _is_precise_cover_provider(area, group):
                continue
            if _area_contains_point(area, start_x, start_y) or _area_contains_point(area, end_x, end_y):
                continue
            if not _segment_intersects_area(area, start_x, start_y, end_x, end_y):
                continue

            cover_type = _infer_cover_type(area)
            cover_score = COVER_LEVEL_SCORES.get(cover_type, 0)
            if cover_score <= 0:
                continue

            providers.append({
                'group': group,
                'index': index,
                'label': str(area.get('label') or '').strip() or 'Map feature',
                'cover_type': cover_type,
                'cover_score': cover_score,
            })

    if not providers:
        return result

    strongest_score = max(provider['cover_score'] for provider in providers)
    strongest_providers = [provider for provider in providers if provider['cover_score'] == strongest_score]
    strongest_cover_type = strongest_providers[0]['cover_type']
    return {
        'cover_type': strongest_cover_type,
        'cover_score': strongest_score,
        'providers': strongest_providers,
    }
