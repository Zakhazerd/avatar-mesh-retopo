"""Lays renders from render_views.py out as a labelled grid (columns = objects, rows = views).

    python compose.py <render dir> <out.png> <mode> <pose+tag> <views,comma,separated> [tile width] [font size]
                      [titles.json]

Renders are <mode>_<view>_<pose+tag>_<object>.png, looked up in each folder of <render dir> (several separated
by ';'). titles.json is a list of [object, column title] pairs, in column order (put the triangle counts in the
titles); without it, every object rendered for the first view becomes a column, titled by its name, shortest
name first (the original before its variants).
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont


def font(size):
    for f in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    folder, out, mode, pose = sys.argv[1:5]
    views = sys.argv[5].split(",")
    tile_w = int(sys.argv[6]) if len(sys.argv) > 6 else 640
    head_size = int(sys.argv[7]) if len(sys.argv) > 7 else 22
    dirs = folder.split(";")

    def find(v, n):
        for d in dirs:
            p = os.path.join(d, f"{mode}_{v}_{pose}_{n}.png")
            if os.path.exists(p):
                return p
        return None
    if len(sys.argv) > 8:
        titles = [tuple(t) for t in json.load(open(sys.argv[8], encoding="utf-8"))]
    else:
        prefix = f"{mode}_{views[0]}_{pose}_"
        names = {f[len(prefix):-4] for d in dirs if os.path.isdir(d) for f in os.listdir(d)
                 if f.startswith(prefix) and f.endswith(".png")}
        titles = [(n, n) for n in sorted(names, key=lambda n: (len(n), n))]
    cols = [(n, t) for n, t in titles if find(views[0], n)]
    rows = []
    for v in views:
        ims = [Image.open(find(v, n)).convert("RGB") for n, _ in cols]
        h = int(ims[0].height * tile_w / ims[0].width)
        rows.append((v, [im.resize((tile_w, h), Image.Resampling.LANCZOS) for im in ims], h))
    head = 44
    W = tile_w * len(cols)
    H = head + sum(h for _, _, h in rows)
    sheet = Image.new("RGB", (W, H), (32, 32, 36))
    d = ImageDraw.Draw(sheet)
    f_head, f_row = font(head_size), font(18)
    for i, (_, title) in enumerate(cols):
        d.text((i * tile_w + 12, 10), title, fill=(235, 235, 235), font=f_head)
    y = head
    for v, ims, h in rows:
        for i, im in enumerate(ims):
            sheet.paste(im, (i * tile_w, y))
            if i:
                d.line([(i * tile_w, y), (i * tile_w, y + h)], fill=(20, 20, 20), width=2)
        d.rectangle([(0, y), (len(v) * 10 + 22, y + 28)], fill=(20, 20, 24))
        d.text((8, y + 3), v, fill=(255, 210, 120), font=f_row)
        y += h
    sheet.save(out)
    print(out, sheet.size)


main()
