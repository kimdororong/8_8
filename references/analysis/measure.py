"""픽셀 측정: 이미지별 '단색 가로줄' 구간(여백/배경)과 내용 구간을 찾아 bands.json으로 저장하고, 미리보기 조각을 만든다."""
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image

IMG = Path(__file__).parent / "img"
PRE = Path(__file__).parent / "preview"; PRE.mkdir(exist_ok=True)

def hexc(c): return "#%02X%02X%02X" % tuple(int(v) for v in c)

def bands(a, tol=6):
    # 한 줄 전체 픽셀이 그 줄 평균색에서 tol 이내면 '단색 줄'
    mean = a.mean(axis=1)
    flat = (np.abs(a - mean[:, None, :]).max(axis=(1, 2)) <= tol)
    out, y, H = [], 0, a.shape[0]
    while y < H:
        f = flat[y]; s = y
        while y < H and flat[y] == f and (not f or np.abs(mean[y] - mean[s]).max() <= tol):
            y += 1
        out.append({"y0": s, "y1": y, "h": y - s, "flat": bool(f), "color": hexc(mean[s:y].mean(0)) if f else None})
    return out

res = {}
for p in sorted(IMG.iterdir()):
    im = Image.open(p); frames = getattr(im, "n_frames", 1)
    im.seek(0); rgb = im.convert("RGB"); a = np.asarray(rgb).astype(int)
    b = bands(a)
    res[p.name] = {"w": rgb.width, "h": rgb.height, "frames": frames, "bands": b}
    # 미리보기: 1600px 높이씩 잘라 절반 크기로
    for i, y in enumerate(range(0, rgb.height, 1600)):
        rgb.crop((0, y, rgb.width, min(y + 1600, rgb.height))).resize((rgb.width // 2, (min(y + 1600, rgb.height) - y) // 2)).save(PRE / f"{p.stem}_{i:02d}_y{y}.jpg", quality=80)

json.dump(res, open(Path(__file__).parent / "bands.json", "w"), ensure_ascii=False, indent=1)
for k, v in res.items():
    big = [f"{x['y0']}-{x['y1']}({x['h']}){x['color']}" for x in v["bands"] if x["flat"] and x["h"] >= 30]
    print(k, v["w"], "x", v["h"], "frames", v["frames"], "| 30px+ 단색:", " ".join(big))
