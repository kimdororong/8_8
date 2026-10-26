# 전체 상세 이미지를 0.3배로 줄여 세로 띠(원본 2400px)로 나란히 놓은 개요 시트를 만든다. 띠 위에 '파일 y시작' 표기.
from pathlib import Path
from PIL import Image, ImageDraw
D=Path(__file__).parent; S=0.3; STRIP=2400
strips=[]
for p in sorted((D/"img").iterdir()):
    im=Image.open(p); im.seek(0); im=im.convert("RGB")
    for y in range(0,im.height,STRIP):
        c=im.crop((0,y,im.width,min(y+STRIP,im.height)))
        c=c.resize((int(c.width*S),int(c.height*S)))
        strips.append((f"{p.stem} y{y}",c))
W=int(860*S)+12; H=int(STRIP*S)+24
for k in range(0,len(strips),7):
    g=strips[k:k+7]; sh=Image.new("RGB",(W*len(g),H),"#888")
    d=ImageDraw.Draw(sh)
    for i,(lab,c) in enumerate(g):
        sh.paste(c,(i*W+6,22)); d.text((i*W+6,4),lab,fill="black")
        for t in range(0,c.height,int(200*S)): d.line((i*W,22+t,i*W+5,22+t),fill="red")
    sh.save(D/"preview"/f"sheet_{k//7}.jpg",quality=85)
print(len(strips))
