"""글자 줄 측정: 지정 영역에서 배경(영역 테두리 중앙값)과 크게 다른 '잉크' 픽셀이 있는 가로줄을 묶어
줄의 위·아래(잉크 높이)와 줄 사이 간격을 잰다. 영역 크롭도 crops/에 저장한다.
사용: textlines.py 파일 x0 y0 x1 y1 이름 [임계값]"""
import sys
from pathlib import Path
import numpy as np
from PIL import Image

D = Path(__file__).parent; (D / "crops").mkdir(exist_ok=True)
f, x0, y0, x1, y1, name = sys.argv[1], *map(int, sys.argv[2:6]), sys.argv[6]
thr = int(sys.argv[7]) if len(sys.argv) > 7 else 80
im = Image.open(D / "img" / f); im.seek(0); im = im.convert("RGB")
crop = im.crop((x0, y0, x1, y1)); crop.save(D / "crops" / f"{name}.png")
a = np.asarray(crop).astype(int)
edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
bg = np.median(edge, axis=0)
ink = np.abs(a - bg).sum(axis=2) > thr
rows = ink.sum(axis=1) >= 2
runs, y = [], 0
while y < len(rows):
    if rows[y]:
        s = y
        while y < len(rows) and rows[y]: y += 1
        cols = np.where(ink[s:y].any(axis=0))[0]
        runs.append((s, y, cols.min(), cols.max()))
    else:
        y += 1
# 같은 줄 안의 받침/점 때문에 잘게 끊긴 조각을 3px 이내면 합친다
merged = []
for r in runs:
    if merged and r[0] - merged[-1][1] <= 3:
        m = merged[-1]; merged[-1] = (m[0], r[1], min(m[2], r[2]), max(m[3], r[3]))
    else:
        merged.append(r)
print(f"{name} {f} 영역({x0},{y0})-({x1},{y1}) 배경≈#{int(bg[0]):02X}{int(bg[1]):02X}{int(bg[2]):02X}")
prev = None
for s, e, c0, c1 in merged:
    gap = f" 앞줄과 간격 {s - prev}px, 줄 피치 {s - pt}px" if prev is not None else ""
    # 글자색: 배경과 가장 먼 픽셀 상위 20%의 중앙값(안티앨리어싱 제외)
    blk = a[s:e][ink[s:e]]; dist = np.abs(blk - bg).sum(1)
    core = np.median(blk[dist >= np.percentile(dist, 80)], axis=0).astype(int)
    col = "#%02X%02X%02X" % tuple(core)
    print(f"  y {y0+s}-{y0+e} 잉크높이 {e-s}px  x {x0+c0}-{x0+c1} (폭 {c1-c0+1}) 글자색≈{col}{gap}")
    prev, pt = e, s
