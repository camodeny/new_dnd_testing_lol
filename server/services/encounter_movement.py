BLOCKING_TERRAIN_KINDS = {'blocked', 'wall'}
DIFFICULT_TERRAIN_KINDS = {'difficult', 'water'}
BLOCKING_MOVEMENT_EFFECTS = {'blocks_movement'}
DIFFICULT_MOVEMENT_EFFECTS = {'costs_extra_movement'}
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
