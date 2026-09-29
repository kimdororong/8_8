#!/usr/bin/env python3
"""크로스 축구 - 낙서 공 (Windows 전용)

내 화면 아래에 작은 낙서판이 떠요. 거기에 마우스로 그림을 그리고 '차기!'를 누르면
그 그림이 공이 되어 날아가요. 공은 내 모니터의 벽에서 튕기다가, 옆사람 쪽 면으로
나가면 옆사람 화면에서 튀어나와요. 마우스 커서로 공을 튕겨 낼 수 있어요.

  한 사람(왼쪽):    python cross_soccer.py host
  다른 사람(오른쪽): python cross_soccer.py join <host가 알려준 IP> <4자리 코드>

  Ctrl+Shift+Q : 언제든 종료 (상대에게도 알려요)
  Ctrl+Shift+P : 낙서판 숨기기/보이기
"""
import argparse
import json
import math
import queue
import random
import re
import socket
import sys
import threading
import time

PORT = 48123
BALL_R = 34
CURSOR_R = 7
GOAL_LEN = 200            # 골대 세로 길이(px). 바깥쪽 벽의 맨 아래에 붙어 있어요
GOAL_DEPTH = 24           # 골대 그림의 가로 두께(얇게)
MIN_SPEED = 220.0
KICK_SPEED = 700.0
MAX_SPEED = 1500.0
LAUNCH_SPEED = 950.0
FRICTION = 0.12
BOUNCE = 0.95
MAX_BALLS = 6
BALL_TTL = 45.0           # 이 시간이 지나면 공이 펑 하고 사라져요
LAUNCH_COOLDOWN = 0.8
STROKE_SCALE = 200        # 낙서 좌표는 -200~200 정수로 보내요
KEY = "#010203"           # 투명 처리할 색
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def normalize_strokes(raw, max_points=400):
    """낙서판 좌표(px)의 선들을 '반지름 200인 원 안'의 정수 좌표로 바꿔요."""
    pts = [p for s in raw for p in s]
    if not pts:
        return []
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    radius = max(10.0, max(math.hypot(p[0] - cx, p[1] - cy) for p in pts))
    thinned = []
    for s in raw:
        if not s:
            continue
        keep = [s[0]]
        for p in s[1:]:
            if math.hypot(p[0] - keep[-1][0], p[1] - keep[-1][1]) >= 3:
                keep.append(p)
        if len(s) > 1 and keep[-1] != s[-1]:
            keep.append(s[-1])
        thinned.append(keep)
    total = sum(len(s) for s in thinned)
    if total > max_points:
        step = math.ceil(total / max_points)
        thinned = [s[::step] + ([s[-1]] if (len(s) - 1) % step else []) for s in thinned]
    return [[[round((x - cx) / radius * STROKE_SCALE), round((y - cy) / radius * STROKE_SCALE)]
             for x, y in s] for s in thinned]


def sanitize_strokes(strokes, max_strokes=60, max_points=600):
    """상대가 보낸 낙서 데이터를 믿지 않고 걸러 내요."""
    out, total = [], 0
    if not isinstance(strokes, list):
        return out
    for s in strokes[:max_strokes]:
        if not isinstance(s, list):
            continue
        pts = []
        for p in s:
            if (isinstance(p, (list, tuple)) and len(p) == 2
                    and all(isinstance(v, (int, float)) for v in p)):
                pts.append([clamp(p[0], -STROKE_SCALE, STROKE_SCALE),
                            clamp(p[1], -STROKE_SCALE, STROKE_SCALE)])
        total += len(pts)
        if total > max_points:
            break
        if pts:
            out.append(pts)
    return out


class Field:
    """경기 규칙과 공 물리. 화면/네트워크와 상관없이 동작해요 (테스트 가능).

    좌표계는 '쓸 수 있는 화면 영역'(작업표시줄 제외)의 왼쪽 위가 (0, 0)이에요.
    """

    def __init__(self, w, h, pass_edge, rng=random):
        self.w, self.h = w, h
        self.pass_edge = pass_edge           # 상대 화면과 이어진 가장자리: 'right' 또는 'left'
        self.rng = rng
        self.balls = []
        self.score_me = 0
        self.score_op = 0
        self.out = []                        # 상대에게 보낼 이벤트
        self.hits = []                       # [x, y, age, color] 효과
        self.pad_rect = None                 # 낙서판 영역: 안에서는 커서가 공을 안 쳐요
        self.cool = 0.0
        self.banner = None
        self.banner_t = 0.0

    @property
    def goal_x(self):
        """골대가 있는 바깥쪽 벽의 x (옆사람과 이어진 면의 반대쪽)."""
        return 0.0 if self.pass_edge == "right" else float(self.w)

    @property
    def goal_zone(self):
        """바깥쪽 벽 맨 아래 GOAL_LEN 만큼이 골대예요 (세로 구간)."""
        return (self.h - GOAL_LEN, float(self.h))

    def say(self, text, secs=1.5):
        self.banner, self.banner_t = text, secs

    # --- 공 만들기 / 받기
    def launch(self, strokes, color, origin):
        """낙서(정규화된 선들)를 공으로 만들어 상대 쪽을 향해 차 내요."""
        if self.cool > 0 or not strokes:
            return False
        if len(self.balls) >= MAX_BALLS:
            self.say("공이 너무 많아요!", 1.2)
            return False
        dirx = 1 if self.pass_edge == "right" else -1
        a = self.rng.uniform(-0.6, 0.1)                   # 살짝 위로
        self.balls.append({
            "x": origin[0], "y": origin[1],
            "vx": dirx * math.cos(a) * LAUNCH_SPEED, "vy": math.sin(a) * LAUNCH_SPEED,
            "rot": 0.0, "ttl": BALL_TTL, "col": color, "strokes": strokes})
        self.cool = LAUNCH_COOLDOWN
        return True

    def receive_ball(self, m):
        col = m.get("col")
        vx, vy = float(m["vx"]) * self.w, float(m["vy"]) * self.h
        if self.pass_edge == "right":
            x, vx = float(self.w), -max(abs(vx), MIN_SPEED)
        else:
            x, vx = 0.0, max(abs(vx), MIN_SPEED)
        self.balls.append({
            "x": x, "y": clamp(float(m["y"]) * self.h, BALL_R, self.h - BALL_R),
            "vx": vx, "vy": vy, "rot": float(m.get("rot", 0.0)),
            "ttl": clamp(float(m.get("ttl", BALL_TTL)), 1.0, BALL_TTL),
            "col": col if isinstance(col, str) and COLOR_RE.match(col) else "#333333",
            "strokes": sanitize_strokes(m.get("s"))})

    def goal_for_me(self):
        self.score_me += 1
        self.say("골! ⚽", 2.0)

    # --- 매 프레임
    def update(self, dt, cur, prev):
        if self.banner:
            self.banner_t -= dt
            if self.banner_t <= 0:
                self.banner = None
        for hit in self.hits:
            hit[2] += dt
        self.hits = [hit for hit in self.hits if hit[2] < 0.35]
        self.cool = max(0.0, self.cool - dt)
        in_pad = False
        if self.pad_rect:
            x1, y1, x2, y2 = self.pad_rect
            in_pad = x1 <= cur[0] <= x2 and y1 <= cur[1] <= y2
        keep = []
        for b in self.balls:
            b["ttl"] -= dt
            if b["ttl"] <= 0:
                self.hits.append([b["x"], b["y"], 0.0, "#888888"])
                continue
            damp = max(0.0, 1 - FRICTION * dt)
            b["vx"] *= damp
            b["vy"] *= damp
            self._limit_speed(b, MIN_SPEED)
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt
            b["rot"] += b["vx"] * dt / BALL_R * 0.5
            if not in_pad:
                self._cursor(b, cur, prev, dt)
            if not self._walls(b):
                keep.append(b)
        self.balls = keep

    def _limit_speed(self, b, floor):
        sp = math.hypot(b["vx"], b["vy"])
        if sp < 1e-6:
            a = self.rng.uniform(0, 2 * math.pi)
            b["vx"], b["vy"], sp = math.cos(a), math.sin(a), 1.0
        target = clamp(sp, floor, MAX_SPEED)
        if target != sp:
            k = target / sp
            b["vx"] *= k
            b["vy"] *= k

    def _cursor(self, b, cur, prev, dt):
        px, py = prev
        dx, dy = cur[0] - px, cur[1] - py
        rr = BALL_R + CURSOR_R
        fx, fy = px - b["x"], py - b["y"]
        a = dx * dx + dy * dy
        c = fx * fx + fy * fy - rr * rr
        t = 0.0                                  # 커서가 공에 처음 닿는 순간 (0~1)
        if c > 0:
            if a == 0:
                return
            bq = 2 * (fx * dx + fy * dy)
            disc = bq * bq - 4 * a * c
            if disc < 0:
                return
            t = (-bq - math.sqrt(disc)) / (2 * a)
            if t < 0 or t > 1:
                return
        qx, qy = px + dx * t, py + dy * t
        ox, oy = b["x"] - qx, b["y"] - qy
        d = math.hypot(ox, oy)
        if d > 1e-6:
            nx, ny = ox / d, oy / d
        elif a > 0:                              # 정확히 겹친 경우: 커서가 가던 방향으로
            nx, ny = dx / math.sqrt(a), dy / math.sqrt(a)
        else:
            ang = self.rng.uniform(0, 2 * math.pi)
            nx, ny = math.cos(ang), math.sin(ang)
        b["x"] = qx + nx * (rr + 1)
        b["y"] = qy + ny * (rr + 1)
        dot = b["vx"] * nx + b["vy"] * ny
        if dot < 0:                              # 다가오던 공은 반사
            b["vx"] -= 2 * dot * nx
            b["vy"] -= 2 * dot * ny
        push = (dx * nx + dy * ny) / max(dt, 1e-3)
        if push > 0:                             # 커서가 밀면 그만큼 더 빠르게
            b["vx"] += nx * push * 0.9
            b["vy"] += ny * push * 0.9
        self._limit_speed(b, KICK_SPEED)
        self.hits.append([qx, qy, 0.0, "#ffd400"])

    def _walls(self, b):
        """벽 처리. 공이 화면을 떠나면(골 / 상대 화면으로 넘어감) True."""
        w, h = self.w, self.h
        if b["y"] < BALL_R:
            b["y"], b["vy"] = BALL_R, abs(b["vy"]) * BOUNCE
        elif b["y"] > h - BALL_R:
            b["y"], b["vy"] = h - BALL_R, -abs(b["vy"]) * BOUNCE
        in_goal = b["y"] >= self.goal_zone[0]
        if self.pass_edge == "right":
            if b["x"] < -BALL_R * 0.5 and in_goal:      # 골대 구간은 벽이 뚫려 있어요
                return self._conceded()
            if b["x"] < BALL_R and not in_goal:
                b["x"], b["vx"] = BALL_R, abs(b["vx"]) * BOUNCE
            if b["x"] > w:
                return self._hand_off(b)
        else:
            if b["x"] > w + BALL_R * 0.5 and in_goal:
                return self._conceded()
            if b["x"] > w - BALL_R and not in_goal:
                b["x"], b["vx"] = w - BALL_R, -abs(b["vx"]) * BOUNCE
            if b["x"] < 0:
                return self._hand_off(b)
        return False

    def _conceded(self):
        self.score_op += 1
        self.out.append(("goal",))
        self.say("실점… 😢", 2.0)
        return True

    def _hand_off(self, b):
        self.out.append(("ball", {
            "y": b["y"] / self.h, "vx": b["vx"] / self.w, "vy": b["vy"] / self.h,
            "rot": b["rot"], "ttl": b["ttl"], "col": b["col"], "s": b["strokes"]}))
        return True


# ------------------------------------------------------------------ 네트워크

class Link:
    """줄 단위 JSON을 주고받는 TCP 연결."""

    def __init__(self, sock, reader):
        self.sock = sock
        self.reader = reader
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.reader:
                self.q.put(json.loads(line))
        except (OSError, ValueError):
            pass
        self.q.put({"t": "bye"})

    def send(self, **msg):
        try:
            self.sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass

    def poll(self):
        msgs = []
        while True:
            try:
                msgs.append(self.q.get_nowait())
            except queue.Empty:
                return msgs

    @staticmethod
    def drop(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)        # makefile이 잡고 있어도 상대에게 끊김이 전달돼요
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def close(self):
        Link.drop(self.sock)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def open_link(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock, sock.makefile("r", encoding="utf-8")


def host_wait():
    code = "%04d" % random.randrange(10000)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", PORT))
    srv.listen(1)
    print("상대에게 이 명령을 알려 주세요:")
    print("  python cross_soccer.py join %s %s" % (local_ip(), code))
    print("(처음 실행하면 Windows 방화벽 창이 뜨는데 '허용'을 눌러 주세요)")
    while True:
        conn, addr = srv.accept()
        sock, reader = open_link(conn)
        try:
            hello = json.loads(reader.readline())
        except ValueError:
            hello = {}
        if hello.get("code") == code:
            sock.sendall(b'{"t": "ok"}\n')
            srv.close()
            return Link(sock, reader)
        print("코드가 틀린 접속을 거절했어요:", addr[0])
        Link.drop(sock)


def join_to(ip, code):
    try:
        sock, reader = open_link(socket.create_connection((ip, PORT), timeout=10))
        sock.sendall((json.dumps({"t": "hello", "code": code}) + "\n").encode())
        ok = json.loads(reader.readline() or "{}").get("t") == "ok"
    except (OSError, ValueError):
        sys.exit("접속하지 못했어요. IP가 맞는지, 같은 와이파이인지, 상대의 방화벽 허용을 눌렀는지 확인해 주세요.")
    if not ok:
        sys.exit("코드가 맞지 않아요.")
    sock.settimeout(None)
    return Link(sock, reader)


# ------------------------------------------------------------------ Windows / 화면

class Win:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u = ctypes.windll.user32
        self.u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.u.GetWindowLongW.restype = ctypes.c_long
        self.u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        self.u.GetParent.argtypes = [ctypes.c_void_p]
        self.u.GetParent.restype = ctypes.c_void_p
        self.u.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                      ctypes.c_ubyte, ctypes.c_uint32]
        self._pt = wintypes.POINT()
        self._was = {}

    def dpi_aware(self):
        self.u.SetProcessDPIAware()

    def workarea(self):
        """기본 모니터에서 작업표시줄을 뺀 영역 (left, top, right, bottom)."""
        r = self.wt.RECT()
        self.u.SystemParametersInfoW(0x30, 0, self.ct.byref(r), 0)   # SPI_GETWORKAREA
        return r.left, r.top, r.right, r.bottom

    def cursor(self):
        self.u.GetCursorPos(self.ct.byref(self._pt))
        return (self._pt.x, self._pt.y)

    def combo_pressed(self, key):
        """Ctrl+Shift+key 를 '누른 순간'에만 True."""
        down = all(self.u.GetAsyncKeyState(vk) & 0x8000 for vk in (0x11, 0x10, ord(key)))
        fired = down and not self._was.get(key, False)
        self._was[key] = down
        return fired

    def click_through(self, root, colorkey):
        """경기장 창을 클릭이 통과하는 투명 창으로 만들어요 (바깥 창 하나에만 적용)."""
        root.update_idletasks()
        try:
            hwnd = int(root.wm_frame(), 16)                 # Tk가 감싼 진짜 최상위 창
        except (ValueError, TypeError):
            hwnd = 0
        hwnd = hwnd or self.u.GetParent(root.winfo_id()) or root.winfo_id()
        style = self.u.GetWindowLongW(hwnd, -20)
        # LAYERED | TRANSPARENT(클릭 통과) | TOOLWINDOW(작업표시줄에서 숨김)
        self.u.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)
        r, g, b = (int(colorkey[i:i + 2], 16) for i in (1, 3, 5))
        self.u.SetLayeredWindowAttributes(hwnd, r | g << 8 | b << 16, 0, 0x1)   # 이 색은 투명(LWA_COLORKEY)


class Pad:
    """작은 낙서판. 여기에 그린 그림이 공이 돼요."""
    W, H, BTN_H = 300, 300, 32
    HINT = "여기에 그림을 그리고\n⚽ 차기!"

    def __init__(self, tk, root, color, pos, on_fire):
        self.color, self.on_fire = color, on_fire
        self.x, self.y = pos
        self.strokes = []
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry("%dx%d+%d+%d" % (self.W, self.H + self.BTN_H, self.x, self.y))
        self.c = tk.Canvas(self.top, width=self.W, height=self.H, bg="white", cursor="pencil",
                           highlightthickness=3, highlightbackground=color)
        self.c.pack()
        bar = tk.Frame(self.top)
        bar.pack(fill="x")
        font = ("Malgun Gothic", 9, "bold")
        tk.Button(bar, text="지우기", font=font, command=self.clear).pack(side="left", expand=True, fill="x")
        tk.Button(bar, text="⚽ 차기!", font=font, command=self.fire).pack(side="left", expand=True, fill="x")
        self.c.bind("<ButtonPress-1>", self._down)
        self.c.bind("<B1-Motion>", self._move)
        self.c.bind("<Button-3>", lambda e: self.clear())
        self.visible = True
        self.clear()

    def rect(self):
        return (self.x, self.y, self.x + self.W, self.y + self.H + self.BTN_H)

    def center(self):
        return (self.x + self.W / 2, self.y + self.H / 2)

    def _down(self, e):
        self.c.delete("hint")
        self.strokes.append([(e.x, e.y)])
        r = 2
        self.c.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, fill=self.color, outline=self.color)

    def _move(self, e):
        if not self.strokes:
            return
        last = self.strokes[-1][-1]
        if math.hypot(e.x - last[0], e.y - last[1]) < 2:
            return
        self.c.create_line(last[0], last[1], e.x, e.y, fill=self.color, width=4,
                           capstyle="round", joinstyle="round")
        self.strokes[-1].append((e.x, e.y))

    def clear(self):
        self.c.delete("all")
        self.strokes = []
        self.c.create_text(self.W / 2, self.H / 2, text=self.HINT, fill="#bbb", tags="hint",
                           font=("Malgun Gothic", 12, "bold"), justify="center")

    def fire(self):
        strokes = normalize_strokes(self.strokes)
        if strokes and self.on_fire(strokes, self.color, self.center()):
            self.clear()

    def toggle(self):
        self.visible = not self.visible
        if self.visible:
            self.top.deiconify()
            self.top.attributes("-topmost", True)
        else:
            self.top.withdraw()


class Overlay:
    FONT = ("Malgun Gothic", 11, "bold")

    def __init__(self, root, canvas, win, link, field, origin, pad_color, tk):
        self.root, self.c, self.win, self.link, self.f = root, canvas, win, link, field
        self.ox, self.oy = origin
        self.last = time.monotonic()
        self.prev = None
        self.closing = False
        pw, ph = Pad.W, Pad.H + Pad.BTN_H
        pad_pos = (self.ox + (field.w - pw) // 2, self.oy + field.h - ph - 12)
        self.pad = Pad(tk, root, pad_color, pad_pos, self.launch)

    def launch(self, strokes, color, center):
        return self.f.launch(strokes, color, (center[0] - self.ox, center[1] - self.oy))

    def quit(self, notify=True):
        if self.closing:
            return
        self.closing = True
        if notify:
            self.link.send(t="bye")
        self.link.close()
        self.root.destroy()

    def frame(self):
        if self.closing:
            return
        now = time.monotonic()
        dt = min(now - self.last, 0.05)
        self.last = now
        if self.win.combo_pressed("Q"):
            return self.quit()
        if self.win.combo_pressed("P"):
            self.pad.toggle()
        for m in self.link.poll():
            t = m.get("t")
            if t == "ball":
                try:
                    self.f.receive_ball(m)
                except (KeyError, TypeError, ValueError):
                    pass
            elif t == "goal":
                self.f.goal_for_me()
            elif t == "bye":
                self.f.say("상대가 나갔어요. 곧 종료해요", 1.5)
                self.root.after(1500, lambda: self.quit(False))
        cur = self.win.cursor()
        fcur = (cur[0] - self.ox, cur[1] - self.oy)
        self.f.pad_rect = None
        if self.pad.visible:
            x1, y1, x2, y2 = self.pad.rect()
            self.f.pad_rect = (x1 - self.ox, y1 - self.oy, x2 - self.ox, y2 - self.oy)
        self.f.update(dt, fcur, self.prev or fcur)
        self.prev = fcur
        for ev in self.f.out:
            if ev[0] == "ball":
                self.link.send(t="ball", **ev[1])
            else:
                self.link.send(t="goal")
        self.f.out.clear()
        self.draw()
        self.root.after(16, self.frame)

    # --- 그리기 (경기장 좌표로 그린 뒤 화면 좌표로 옮겨요)
    def pill(self, x, y, text, size=None):
        font = self.FONT if size is None else ("Malgun Gothic", size, "bold")
        t = self.c.create_text(x, y, text=text, font=font, fill="#222")
        x1, y1, x2, y2 = self.c.bbox(t)
        r = self.c.create_rectangle(x1 - 10, y1 - 4, x2 + 10, y2 + 4, fill="white", outline="#222", width=2)
        self.c.tag_lower(r, t)

    def draw(self):
        c, f = self.c, self.f
        c.delete("all")
        self.draw_goal()
        for x, y, age, col in f.hits:
            r = CURSOR_R + age * 120
            c.create_oval(x - r, y - r, x + r, y + r, outline=col, width=3)
        for b in f.balls:
            self.draw_ball(b)
        self.pill(f.w / 2, 22, "나 %d  :  %d 상대" % (f.score_me, f.score_op))
        if f.banner:
            self.pill(f.w / 2, f.h / 2 - 120, f.banner, 28)
        c.move("all", self.ox, self.oy)

    def draw_goal(self):
        f, c = self.f, self.c
        y0, y1 = f.goal_zone
        gx = f.goal_x
        x0, x1 = (gx, gx + GOAL_DEPTH) if gx == 0 else (gx - GOAL_DEPTH, gx)
        c.create_rectangle(x0, y0, x1, y1, outline="#e02020", width=4)
        y = y0 + 16
        while y < y1:
            c.create_line(x0, y, x1, y, fill="#e02020")
            y += 16
        for x in (x0 + GOAL_DEPTH / 3, x0 + 2 * GOAL_DEPTH / 3):
            c.create_line(x, y0, x, y1, fill="#e02020")

    def draw_ball(self, b):
        c, x, y, r = self.c, b["x"], b["y"], BALL_R
        c.create_oval(x - r, y - r, x + r, y + r, fill="white", outline=b["col"], width=3)
        k = 0.82 * r / STROKE_SCALE
        cs, sn = math.cos(b["rot"]), math.sin(b["rot"])
        for pts in b["strokes"]:
            flat = []
            for px, py in pts:
                u, v = px * k, py * k
                flat += [x + u * cs - v * sn, y + u * sn + v * cs]
            if len(flat) >= 4:
                c.create_line(*flat, fill=b["col"], width=3, capstyle="round", joinstyle="round")
            else:
                c.create_oval(flat[0] - 2, flat[1] - 2, flat[0] + 2, flat[1] + 2, fill=b["col"], outline=b["col"])


def main():
    ap = argparse.ArgumentParser(description="크로스 축구 - 낙서 공")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("host", help="왼쪽 사람: 방 열기")
    j = sub.add_parser("join", help="오른쪽 사람: 접속하기")
    j.add_argument("ip")
    j.add_argument("code")
    args = ap.parse_args()

    if sys.platform != "win32":
        sys.exit("Windows에서만 실행돼요.")

    link = host_wait() if args.mode == "host" else join_to(args.ip, args.code)
    print("연결됐어요! 종료: Ctrl+Shift+Q, 낙서판 숨기기: Ctrl+Shift+P")

    import tkinter as tk
    win = Win()
    win.dpi_aware()
    root = tk.Tk()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.overrideredirect(True)
    root.geometry("%dx%d+0+0" % (sw, sh))
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.config(bg=KEY)
    canvas = tk.Canvas(root, width=sw, height=sh, bg=KEY, highlightthickness=0, bd=0)
    canvas.pack()
    root.update()
    win.click_through(root, KEY)

    left, top, right, bottom = win.workarea()
    is_host = args.mode == "host"
    field = Field(right - left, bottom - top, "right" if is_host else "left")
    field.say("낙서를 그리고 ⚽ 차기!", 4.0)
    overlay = Overlay(root, canvas, win, link, field, (left, top),
                      "#1e6fe0" if is_host else "#e02020", tk)
    overlay.frame()
    root.mainloop()


if __name__ == "__main__":
    main()
