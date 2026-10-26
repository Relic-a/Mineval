"""Explicitly reviewed, local-only PNG/GIF exports of real interactive websites."""
import io
import os
from pathlib import Path
from uuid import uuid4

from .arena import now
from .artifacts import digest


def labels(arena, benchmark, anonymous):
    runs = arena.records('generations', benchmark)
    identities = list(dict.fromkeys((r['config']['harness'], r['config']['model']) for r in runs))
    result = {}
    for r in runs:
        c = r['config']
        i = identities.index((c['harness'], c['model']))
        alias = chr(65+i) if i < 26 else str(i+1)
        result[r['id']] = ('Model ' + alias if anonymous else c['model']) + ' · ' + c['effort']
    return result



def export(arena, manager, benchmark, data):
    if data.get('reviewed_content') is not True or data.get('reviewed_identity') is not True:
        raise ValueError('Review website content and identity/privacy choices before exporting')
    keys = data.get('generations', [])
    if not isinstance(keys, list) or not 1 <= len(keys) <= 4 or len(set(keys)) != len(keys):
        raise ValueError('Select 1–4 distinct websites')
    task = arena.get('benchmarks', benchmark)
    for key in keys:
        r = arena.get('generations', key)
        if r['benchmark'] != benchmark or r['status'] != 'ready':
            raise ValueError('Select ready websites from this benchmark')
    kind = data.get('format', 'png')
    if kind not in ('png', 'gif'): raise ValueError('Choose PNG or GIF')
    try:
        from PIL import Image, ImageDraw, ImageFont
        from playwright.sync_api import sync_playwright, Error as CaptureError
    except ImportError as exc:
        raise ValueError('Sharing needs the arena-share extra and Chromium. See docs/web-arena.md.') from exc
    names = labels(arena, benchmark, data.get('anonymous', True))
    board = arena.leaderboard(benchmark)
    positions = {r['id']: r['position'] for r in board}
    selected = [r for r in board if r['id'] in keys]
    frames = {key: [] for key in keys}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, chromium_sandbox=True,
                executable_path=os.environ.get('MINEVAL_ARENA_CHROMIUM'))
        except CaptureError as exc:
            raise ValueError('Capture browser unavailable. Run python -m playwright install chromium, or set MINEVAL_ARENA_CHROMIUM to a trusted sandboxed Chromium executable.') from exc
        try:
            for key in keys:
                url = manager.open(key)
                from urllib.parse import urlsplit
                origin = 'http://' + urlsplit(url).netloc
                context = browser.new_context(viewport={'width': 960, 'height': 640}, device_scale_factor=1,
                                              service_workers='block', accept_downloads=False)
                context.route('**/*', lambda route, request, origin=origin: route.continue_() if request.url.startswith(origin + '/') else route.abort())
                page = context.new_page()
                page.goto(url, wait_until='networkidle', timeout=30000)
                for _ in range(3 if kind == 'gif' else 1):
                    frames[key].append(Image.open(io.BytesIO(page.screenshot(timeout=15000))).convert('RGB'))
                    if kind == 'gif': page.wait_for_timeout(700)
                context.close()
        except CaptureError as exc:
            raise ValueError('Website capture failed: ' + str(exc)[:1000]) from exc
        finally:
            browser.close()
    def font(size, bold=False):
        path = '/usr/share/fonts/TTF/DejaVuSans' + ('-Bold' if bold else '') + '.ttf'
        if not Path(path).exists(): path = '/usr/share/fonts/truetype/dejavu/DejaVuSans' + ('-Bold' if bold else '') + '.ttf'
        return ImageFont.truetype(path, size) if Path(path).exists() else ImageFont.load_default(size=size)
    import textwrap
    heading = task['title'] if data.get('include_title', True) else 'Website design comparison'
    task_lines = textwrap.wrap(task['prompt'], 110)[:3] if data.get('include_prompt', False) else []
    cols = 1 if len(keys) == 1 else 2
    width = 1200
    thumb_width = (width - 80 - (cols-1)*24)//cols
    thumb_height = round(thumb_width*640/960)
    top = 160 + 26 * len(task_lines)
    height = top + ((len(keys)+cols-1)//cols)*(thumb_height+80) + 100
    images = []
    for frame in range(3 if kind == 'gif' else 1):
        image = Image.new('RGB', (width, height), '#f5f4ef')
        draw = ImageDraw.Draw(image)
        draw.text((40, 24), 'Mineval / Web Arena', fill='#235d48', font=font(20, True))
        draw.text((40, 65), textwrap.shorten(heading, width=55), fill='#242d2a', font=font(34, True))
        for n, line in enumerate(task_lines): draw.text((40, 115+n*26), line, fill='#53645e', font=font(18))
        for i, key in enumerate(keys):
            x = 40 + (i % cols)*(thumb_width+24)
            y = top + (i // cols)*(thumb_height+80)
            draw.text((x, y-34), textwrap.shorten(f"{'#'+str(positions[key]) if positions[key] else 'Unplaced'}  {names[key]}", width=44), fill='#242d2a', font=font(19, True))
            image.paste(frames[key][frame].resize((thumb_width, thumb_height)), (x, y))
            row = next(r for r in selected if r['id'] == key)
            draw.text((x, y+thumb_height+8), f"{row['judgments']} judgments · provisional position", fill='#53645e', font=font(15))
        draw.text((40, height-65), f"Human preferences · {len(arena.records('judgments', benchmark))} saved judgments · {len(keys)} of {len(board)} outputs shown", fill='#53645e', font=font(17))
        draw.text((40, height-37), now()[:10] + ' · ' + ('Anonymous labels; review website content for identity clues' if data.get('anonymous', True) else 'Model identities included'), fill='#53645e', font=font(15))
        if kind == 'gif':
            # A quiet capture-progress accent keeps even static websites a real
            # animated showcase rather than a single-frame GIF container.
            draw.rectangle((40, height-5, 40 + int((width-80)*(frame+1)/3), height-1), fill='#235d48')
        images.append(image)
    key = uuid4().hex
    folder = arena.root / 'shares'
    folder.mkdir(exist_ok=True)
    path = folder / (key + '.' + kind)
    if kind == 'png': images[0].save(path)
    else: images[0].save(path, save_all=True, append_images=images[1:], duration=700, loop=0)
    return arena.insert('exports', dict(id=key, benchmark=benchmark, created_at=now(),
        format=kind, sha256=digest(path), options=data, generations=keys))
