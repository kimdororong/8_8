#!/usr/bin/env python3
"""크로스 축구 - 낙서 공 (Windows 전용)

낙서판에 마우스로 그림을 그리고 '차기!'를 누르면 그 그림이 그대로 날아가요.
공은 내 모니터의 벽에서 튕기다가, 옆사람 쪽 면으로 나가면 옆사람 화면에서 튀어나와요.
마우스 커서로 공을 튕겨 낼 수 있어요.

  python cross_soccer.py            : 시작 화면 (왼쪽/오른쪽 고르고 방 만들기 / 참가하기)
  python cross_soccer.py host [--me left|right]
  python cross_soccer.py join <IP> <코드> [--me left|right]

  Ctrl+Shift+Q : 언제든 종료 (상대에게도 알려요)
  Ctrl+Shift+P : 낙서판 닫기/열기
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
MIN_R = 12                # 낙서 공의 최소 반지름 (점 하나만 찍어도 칠 수 있게)
MAX_R = 150               # 이보다 큰 낙서는 이 크기로 줄여요 (골대에 들어갈 수 있게)
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
FANFARE_SECS = 3.5
CONFETTI_N = 160
CONFETTI_COLORS = ("#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#af52de", "#ff9500", "#ff2d55")
KEY = "#010203"           # 투명 처리할 색
FONT = "Malgun Gothic"
SIDE_NAME = {"left": "왼쪽", "right": "오른쪽"}
SIDE_COLOR = {"left": "#1e6fe0", "right": "#e02020"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def other_side(me):
    return "right" if me == "left" else "left"


# ------------------------------------------------------------------ 낙서

def doodle_radius(strokes):
    """낙서를 감싸는 반지름 (선 두께 포함). 벽/커서 충돌에 써요."""
    far = max((math.hypot(x, y) for s in strokes for x, y in s), default=0.0)
    return clamp(far + 3, MIN_R, MAX_R + 3)


def make_doodle(raw, max_points=400):
    """낙서판 좌표(px)의 선들을 '가운데 기준 상대 좌표'로 바꿔요. 크기는 그대로.

    돌려주는 값: (선들, 반지름, 낙서판 안에서의 가운데 좌표)
    """
    pts = [p for s in raw for p in s]
    if not pts:
        return [], 0.0, (0.0, 0.0)
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
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
    far = max(math.hypot(x - cx, y - cy) for s in thinned for x, y in s)
    k = MAX_R / far if far > MAX_R else 1.0
    strokes = [[[round((x - cx) * k), round((y - cy) * k)] for x, y in s] for s in thinned]
    return strokes, doodle_radius(strokes), (cx, cy)


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
                    and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p)):
                pts.append([clamp(p[0], -MAX_R, MAX_R), clamp(p[1], -MAX_R, MAX_R)])
        total += len(pts)
        if total > max_points:
            break
        if pts:
            out.append(pts)
    return out


# ------------------------------------------------------------------ 경기 규칙

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
        self.fanfare = 0.0                   # 골 먹혔을 때 팡파레가 남은 시간
        self.fanfare_new = False             # 이번 프레임에 팡파레가 시작됐는지 (소리용)
        self.confetti = []                   # [x, y, vx, vy, color, size]

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
        """낙서를 그린 자리(origin)에서 상대 쪽을 향해 차 내요."""
        if self.cool > 0 or not strokes:
            return False
        if len(self.balls) >= MAX_BALLS:
            self.say("공이 너무 많아요!", 1.2)
            return False
        dirx = 1 if self.pass_edge == "right" else -1
        a = self.rng.uniform(-0.6, 0.1)                   # 살짝 위로
        self.balls.append({
            "x": float(origin[0]), "y": float(origin[1]),
            "vx": dirx * math.cos(a) * LAUNCH_SPEED, "vy": math.sin(a) * LAUNCH_SPEED,
            "r": doodle_radius(strokes), "ttl": BALL_TTL, "col": color, "strokes": strokes})
        self.cool = LAUNCH_COOLDOWN
        return True

    def receive_ball(self, m):
        col = m.get("col")
        strokes = sanitize_strokes(m.get("s"))
        r = doodle_radius(strokes)
        vx, vy = float(m["vx"]) * self.w, float(m["vy"]) * self.h
        if not (math.isfinite(vx) and math.isfinite(vy)):
            raise ValueError("bad speed")
        if self.pass_edge == "right":
            x, vx = float(self.w), -max(abs(vx), MIN_SPEED)
        else:
            x, vx = 0.0, max(abs(vx), MIN_SPEED)
        self.balls.append({
            "x": x, "y": clamp(float(m["y"]) * self.h, r, self.h - r),
            "vx": vx, "vy": vy, "r": r,
            "ttl": clamp(float(m.get("ttl", BALL_TTL)), 1.0, BALL_TTL),
            "col": col if isinstance(col, str) and COLOR_RE.match(col) else "#333333",
            "strokes": strokes})

    def goal_for_me(self):
        self.score_me += 1
        self.say("골! ⚽ 상대 화면에 팡파레가 울렸어요", 2.5)

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
        self._update_fanfare(dt)
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
            if not in_pad:
                self._cursor(b, cur, prev, dt)
            if not self._walls(b):
                keep.append(b)
        self.balls = keep

    def _update_fanfare(self, dt):
        self.fanfare = max(0.0, self.fanfare - dt)
        for p in self.confetti:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += 220 * dt
        self.confetti = [p for p in self.confetti if p[1] < self.h + 20]

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
        rr = b["r"] + CURSOR_R
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
        w, h, r = self.w, self.h, b["r"]
        if b["y"] < r:
            b["y"], b["vy"] = r, abs(b["vy"]) * BOUNCE
        elif b["y"] > h - r:
            b["y"], b["vy"] = h - r, -abs(b["vy"]) * BOUNCE
        in_goal = b["y"] >= self.goal_zone[0]
        if self.pass_edge == "right":
            if b["x"] < -r * 0.5 and in_goal:            # 골대 구간은 벽이 뚫려 있어요
                return self._conceded()
            if b["x"] < r and not in_goal:
                b["x"], b["vx"] = r, abs(b["vx"]) * BOUNCE
            if b["x"] > w:
                return self._hand_off(b)
        else:
            if b["x"] > w + r * 0.5 and in_goal:
                return self._conceded()
            if b["x"] > w - r and not in_goal:
                b["x"], b["vx"] = w - r, -abs(b["vx"]) * BOUNCE
            if b["x"] < 0:
                return self._hand_off(b)
        return False

    def _conceded(self):
        self.score_op += 1
        self.out.append(("goal",))
        self.fanfare = FANFARE_SECS
        self.fanfare_new = True
        rng = self.rng
        for _ in range(CONFETTI_N):
            self.confetti.append([rng.uniform(0, self.w), rng.uniform(-self.h * 0.4, 0),
                                  rng.uniform(-140, 140), rng.uniform(120, 420),
                                  rng.choice(CONFETTI_COLORS), rng.uniform(6, 13)])
        return True

    def _hand_off(self, b):
        self.out.append(("ball", {
            "y": b["y"] / self.h, "vx": b["vx"] / self.w, "vy": b["vy"] / self.h,
            "ttl": b["ttl"], "col": b["col"], "s": b["strokes"]}))
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
                msg = json.loads(line)
                if isinstance(msg, dict):
                    self.q.put(msg)
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


def send_line(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


class HostRoom:
    """방 만들기. me: 내 모니터가 옆사람 기준 어느 쪽인지 ('left'/'right')."""

    def __init__(self, me, port=PORT):
        self.me = me
        self.code = "%04d" % random.randrange(10000)
        self.ip = local_ip()
        self.srv = socket.socket()
        if sys.platform != "win32":            # Windows에서는 같은 포트를 두 번 열 수 있게 돼서 안 써요
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.srv.bind(("", port))
            self.srv.listen(2)
        except OSError:
            self.srv.close()
            raise

    def wait(self, on_info=print):
        """옆사람이 맞는 코드와 반대쪽 자리로 접속할 때까지 기다려요. close()하면 OSError."""
        want = other_side(self.me)
        while True:
            conn, addr = self.srv.accept()
            conn.settimeout(5)
            sock, reader = open_link(conn)
            try:
                hello = json.loads(reader.readline() or "{}")
            except (OSError, ValueError):
                hello = {}
            if not isinstance(hello, dict):
                hello = {}
            try:
                if hello.get("code") != self.code:
                    send_line(sock, {"t": "no", "why": "code"})
                    on_info("코드가 틀린 접속을 거절했어요 (%s)" % addr[0])
                elif hello.get("me") not in (None, want):
                    send_line(sock, {"t": "no", "why": "side", "host": self.me})
                    on_info("옆사람도 '%s'을 골랐어요. 옆사람은 '%s'을 골라야 해요."
                            % (SIDE_NAME[self.me], SIDE_NAME[want]))
                else:
                    send_line(sock, {"t": "ok", "me": want})
                    sock.settimeout(None)
                    self.close()
                    return Link(sock, reader)
            except OSError:
                pass
            Link.drop(sock)

    def close(self):
        try:
            self.srv.shutdown(socket.SHUT_RDWR)   # 다른 스레드의 accept()를 깨워요 (리눅스)
        except OSError:
            pass
        try:
            self.srv.close()
        except OSError:
            pass


class JoinError(Exception):
    pass


def join_to(ip, code, me=None, port=PORT):
    """참가하기. me를 안 주면 방장의 반대쪽이 돼요. (Link, 내 자리)를 돌려줘요."""
    try:
        sock, reader = open_link(socket.create_connection((ip, port), timeout=10))
        send_line(sock, {"t": "hello", "code": code, "me": me})
        reply = json.loads(reader.readline() or "{}")
    except (OSError, ValueError):
        raise JoinError("접속하지 못했어요. IP가 맞는지, 같은 와이파이인지, "
                        "방을 만든 사람이 방화벽 '허용'을 눌렀는지 확인해 주세요.")
    if not isinstance(reply, dict) or reply.get("t") != "ok":
        Link.drop(sock)
        if isinstance(reply, dict) and reply.get("why") == "side" and me in SIDE_NAME:
            raise JoinError("방을 만든 사람도 '%s'이에요. '%s'을 골라서 다시 참가해 주세요."
                            % (SIDE_NAME[me], SIDE_NAME[other_side(me)]))
        raise JoinError("코드가 맞지 않아요.")
    sock.settimeout(None)
    mine = reply.get("me") if reply.get("me") in SIDE_NAME else (me or "right")
    return Link(sock, reader), mine


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


def make_fanfare_wav(path, rate=22050, seconds=2.0):
    """2초짜리 축하 사운드(빰빠바밤~ + 화음 + 관중 함성)를 WAV 파일로 만들어요."""
    import array
    import wave
    n = int(rate * seconds)
    buf = [0.0] * n

    def tone(freq, start, dur, vol, fade_out=0.04):
        i0, i1 = int(start * rate), min(n, int((start + dur) * rate))
        for i in range(i0, i1):
            t = (i - i0) / rate
            env = min(1.0, t / 0.012) * min(1.0, (dur - t) / fade_out)
            vib = 1 + 0.004 * math.sin(2 * math.pi * 5.5 * t) if dur > 0.5 else 1
            w = 2 * math.pi * freq * vib * t
            # 배음을 섞어서 나팔 소리처럼
            buf[i] += vol * env * (math.sin(w) + 0.5 * math.sin(2 * w) + 0.3 * math.sin(3 * w)
                                   + 0.15 * math.sin(4 * w))

    # 빰 빠 바 밤~ 빠 밤!
    for f, st, du in ((392, 0.00, 0.11), (523, 0.12, 0.11), (659, 0.24, 0.11), (784, 0.36, 0.22),
                      (659, 0.60, 0.10), (784, 0.72, 0.14)):
        tone(f, st, du, 0.22)
    for f in (523, 659, 784, 1047):              # 마지막 화음을 길게
        tone(f, 0.88, seconds - 0.88, 0.12, fade_out=0.7)
    # 관중 함성: 부드럽게 거른 잡음이 커졌다가 사라져요
    rng = random.Random(7)
    lp = 0.0
    for i in range(int(0.25 * rate), n):
        t = i / rate
        lp += 0.08 * (rng.uniform(-1, 1) - lp)
        env = min(1.0, (t - 0.25) / 0.5) * min(1.0, (seconds - t) / 0.6)
        buf[i] += 0.45 * env * lp

    peak = max(1e-9, max(abs(v) for v in buf))
    pcm = array.array("h", (int(v / peak * 0.85 * 32767) for v in buf))
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


_fanfare_path = None


def prepare_fanfare():
    """게임 시작할 때 한 번만 효과음 파일을 만들어 둬요."""
    global _fanfare_path
    import os
    import tempfile
    try:
        path = os.path.join(tempfile.gettempdir(), "cross_soccer_fanfare.wav")
        make_fanfare_wav(path)
        _fanfare_path = path
    except OSError:
        _fanfare_path = None


def play_fanfare():
    """골 먹힌 쪽 스피커로 2초 축하 사운드. 파일이 없으면 삑 소리로 대신해요."""
    try:
        import winsound
    except ImportError:
        return
    if _fanfare_path:
        try:
            winsound.PlaySound(_fanfare_path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
            return
        except RuntimeError:
            pass

    def beeps():
        for freq, ms in ((523, 110), (659, 110), (784, 110), (1047, 260), (784, 120), (1047, 480)):
            try:
                winsound.Beep(freq, ms)
            except RuntimeError:
                return
    threading.Thread(target=beeps, daemon=True).start()


class Pad:
    """낙서판. 제목줄을 끌어서 옮기고, ✕로 닫을 수 있어요."""
    W, H = 300, 300
    HINT = "여기에 그림을 그리고\n⚽ 차기!"

    def __init__(self, tk, root, color, bottom_center, on_fire):
        self.color, self.on_fire = color, on_fire
        self.strokes = []
        self.visible = True
        self._off = (0, 0)

        self.top = top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=color)
        title = tk.Frame(top, bg=color)
        title.pack(fill="x")
        grip = tk.Label(title, text="  낙서판  ·  여기를 끌어서 옮겨요", bg=color, fg="white",
                        font=(FONT, 9, "bold"), cursor="fleur", anchor="w")
        grip.pack(side="left", fill="x", expand=True, pady=4)
        tk.Button(title, text="✕", command=self.hide, bg=color, fg="white", bd=0, relief="flat",
                  activebackground="#b00000", activeforeground="white", font=(FONT, 10, "bold"),
                  width=3, cursor="hand2").pack(side="right")
        for w in (title, grip):
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
        self.c = tk.Canvas(top, width=self.W, height=self.H, bg="white", cursor="pencil",
                           highlightthickness=0)
        self.c.pack(padx=3)
        bar = tk.Frame(top, bg=color)
        bar.pack(fill="x", padx=3, pady=(0, 3))
        font = (FONT, 10, "bold")
        tk.Button(bar, text="지우기", font=font, command=self.clear).pack(side="left", expand=True, fill="x")
        tk.Button(bar, text="⚽ 차기!", font=font, command=self.fire).pack(side="left", expand=True, fill="x")
        self.c.bind("<ButtonPress-1>", self._down)
        self.c.bind("<B1-Motion>", self._move)
        self.c.bind("<Button-3>", lambda e: self.clear())

        top.update_idletasks()
        x = int(bottom_center[0] - top.winfo_reqwidth() / 2)
        y = int(bottom_center[1] - top.winfo_reqheight())
        top.geometry("+%d+%d" % (x, y))

        # 닫았을 때 남는 작은 '다시 열기' 버튼
        self.mini = tk.Toplevel(root)
        self.mini.overrideredirect(True)
        self.mini.attributes("-topmost", True)
        tk.Button(self.mini, text="✎ 낙서판 열기", command=self.show, bg=color, fg="white",
                  activebackground=color, activeforeground="white", font=(FONT, 9, "bold"),
                  bd=0, padx=10, pady=6, cursor="hand2").pack()
        self.mini.withdraw()
        self.clear()

    # --- 옮기기 / 닫기
    def _grab(self, e):
        self._off = (e.x_root - self.top.winfo_x(), e.y_root - self.top.winfo_y())

    def _drag(self, e):
        self.top.geometry("+%d+%d" % (e.x_root - self._off[0], e.y_root - self._off[1]))

    def rect(self):
        t = self.top
        x, y = t.winfo_rootx(), t.winfo_rooty()
        return (x, y, x + t.winfo_width(), y + t.winfo_height())

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        x, y = self.top.winfo_x(), self.top.winfo_y()
        h = self.top.winfo_height()
        self.top.withdraw()
        self.mini.geometry("+%d+%d" % (x, y + h - 34))
        self.mini.deiconify()
        self.mini.attributes("-topmost", True)

    def show(self):
        if self.visible:
            return
        self.visible = True
        self.mini.withdraw()
        self.top.deiconify()
        self.top.attributes("-topmost", True)

    def toggle(self):
        self.hide() if self.visible else self.show()

    # --- 그리기
    def _pt(self, e):
        return (clamp(e.x, 0, self.W), clamp(e.y, 0, self.H))

    def _down(self, e):
        self.c.delete("hint")
        x, y = self._pt(e)
        self.strokes.append([(x, y)])
        r = 2
        self.c.create_oval(x - r, y - r, x + r, y + r, fill=self.color, outline=self.color)

    def _move(self, e):
        if not self.strokes:
            return
        x, y = self._pt(e)
        last = self.strokes[-1][-1]
        if math.hypot(x - last[0], y - last[1]) < 2:
            return
        self.c.create_line(last[0], last[1], x, y, fill=self.color, width=4,
                           capstyle="round", joinstyle="round")
        self.strokes[-1].append((x, y))

    def clear(self):
        self.c.delete("all")
        self.strokes = []
        self.c.create_text(self.W / 2, self.H / 2, text=self.HINT, fill="#bbb", tags="hint",
                           font=(FONT, 12, "bold"), justify="center")

    def fire(self):
        strokes, _, (cx, cy) = make_doodle(self.strokes)
        if not strokes:
            return
        origin = (self.c.winfo_rootx() + cx, self.c.winfo_rooty() + cy)   # 그린 자리에서 출발
        if self.on_fire(strokes, self.color, origin):
            self.clear()


class Overlay:
    PILL_FONT = (FONT, 11, "bold")

    def __init__(self, root, canvas, win, link, field, origin, me, tk):
        self.root, self.c, self.win, self.link, self.f = root, canvas, win, link, field
        self.ox, self.oy = origin
        self.last = time.monotonic()
        self.prev = None
        self.closing = False
        self.pad = Pad(tk, root, SIDE_COLOR[me],
                       (self.ox + field.w / 2, self.oy + field.h - 10), self.launch)

    def launch(self, strokes, color, origin):
        return self.f.launch(strokes, color, (origin[0] - self.ox, origin[1] - self.oy))

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
                except (KeyError, TypeError, ValueError, OverflowError):
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
        if self.f.fanfare_new:
            self.f.fanfare_new = False
            play_fanfare()
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
        font = self.PILL_FONT if size is None else (FONT, size, "bold")
        t = self.c.create_text(x, y, text=text, font=font, fill="#222")
        x1, y1, x2, y2 = self.c.bbox(t)
        r = self.c.create_rectangle(x1 - 12, y1 - 5, x2 + 12, y2 + 5, fill="white", outline="#222", width=2)
        self.c.tag_lower(r, t)

    def draw(self):
        c, f = self.c, self.f
        c.delete("all")
        self.draw_goal()
        for x, y, age, col in f.hits:
            r = CURSOR_R + age * 120
            c.create_oval(x - r, y - r, x + r, y + r, outline=col, width=3)
        for b in f.balls:
            self.draw_doodle(b)
        self.pill(f.w / 2, 22, "나 %d  :  %d 상대" % (f.score_me, f.score_op))
        if f.fanfare > 0 or f.confetti:
            self.draw_fanfare()
        elif f.banner:
            self.pill(f.w / 2, f.h / 2 - 120, f.banner, 24)
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

    def draw_doodle(self, b):
        """테두리 없이 그린 그대로. 어떤 배경에서도 보이게 흰 테를 살짝 둘러요."""
        c, x, y, col = self.c, b["x"], b["y"], b["col"]
        for width, fill in ((8, "white"), (4, col)):
            for pts in b["strokes"]:
                if len(pts) >= 2:
                    flat = [v for px, py in pts for v in (x + px, y + py)]
                    c.create_line(*flat, fill=fill, width=width, capstyle="round", joinstyle="round")
                else:
                    r = width / 2 + 0.5
                    px, py = x + pts[0][0], y + pts[0][1]
                    c.create_oval(px - r, py - r, px + r, py + r, fill=fill, outline=fill)

    def draw_fanfare(self):
        c, f = self.c, self.f
        for x, y, _, _, col, s in f.confetti:
            c.create_rectangle(x, y, x + s, y + s * 0.6, fill=col, outline="")
        if f.fanfare <= 0:
            return
        t = FANFARE_SECS - f.fanfare
        size = int(40 + 80 * min(1.0, t / 0.35)) // 10 * 10          # 뿅 하고 커지게
        cx, cy = f.w / 2, f.h / 2 - 80
        col = CONFETTI_COLORS[int(t * 8) % len(CONFETTI_COLORS)]
        font = ("Arial Black", size, "bold")
        for dx, dy in ((-5, 0), (5, 0), (0, -5), (0, 5), (-4, -4), (4, 4), (-4, 4), (4, -4)):
            c.create_text(cx + dx, cy + dy, text="GOAL!!!", font=font, fill="#1a1a1a")
        c.create_text(cx, cy, text="GOAL!!!", font=font, fill=col)
        self.pill(cx, cy + size + 20, "상대 골~인!   나 %d : %d 상대" % (f.score_me, f.score_op), 22)


def run_game(tk, win, link, me):
    """경기장(투명 창)과 낙서판을 띄워요. me: 내 자리 ('left'/'right')."""
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

    prepare_fanfare()
    left, top, right, bottom = win.workarea()
    field = Field(right - left, bottom - top, other_side(me))
    field.say("낙서를 그리고 ⚽ 차기!", 4.0)
    Overlay(root, canvas, win, link, field, (left, top), me, tk).frame()
    root.mainloop()


# ------------------------------------------------------------------ 시작 화면

def launcher(tk):
    """왼쪽/오른쪽을 고르고 방 만들기 / 참가하기. 연결되면 (Link, 내 자리), 닫으면 None."""
    root = tk.Tk()
    root.title("크로스 축구")
    root.resizable(False, False)
    root.configure(padx=22, pady=16)
    side = tk.StringVar(value="")
    q = queue.Queue()
    st = {"room": None, "result": None, "busy": False}
    bold = (FONT, 11, "bold")

    tk.Label(root, text="⚽ 크로스 축구", font=(FONT, 20, "bold")).pack()
    tk.Label(root, text="낙서를 공으로 만들어 옆사람 화면으로 차 넣어요", fg="#666", font=(FONT, 10)).pack()

    tk.Label(root, text="① 내 모니터는 어느 쪽이에요?", font=bold, anchor="w").pack(fill="x", pady=(16, 6))
    pv = tk.Canvas(root, width=360, height=112, bg="white", highlightthickness=1, highlightbackground="#ddd")
    pv.pack()
    row = tk.Frame(root)
    row.pack(pady=8)
    radios = []
    for text, value in (("◀  내가 왼쪽", "left"), ("내가 오른쪽  ▶", "right")):
        rb = tk.Radiobutton(row, text=text, variable=side, value=value, indicatoron=0, width=14,
                            font=bold, pady=6, selectcolor="#cfe0ff", cursor="hand2")
        rb.pack(side="left", padx=6)
        radios.append(rb)

    def draw_preview(*_):
        pv.delete("all")
        s = side.get()
        for i, x0 in enumerate((20, 190)):
            box_side = "left" if i == 0 else "right"
            who = "?" if not s else ("나" if s == box_side else "옆사람")
            mine = who == "나"
            pv.create_rectangle(x0, 10, x0 + 150, 90, fill="#e8f0ff" if mine else "#f5f5f5",
                                outline=SIDE_COLOR[box_side] if mine else "#999", width=3 if mine else 1)
            pv.create_text(x0 + 75, 46, text=who, font=(FONT, 15, "bold"),
                           fill=SIDE_COLOR[box_side] if mine else "#555")
            gx = x0 + 3 if i == 0 else x0 + 147
            pv.create_line(gx, 62, gx, 88, fill="#e02020", width=5)
            pv.create_rectangle(x0 + 65, 92, x0 + 85, 100, fill="#888", outline="")
        pv.create_text(180, 50, text="⇄", font=(FONT, 13, "bold"), fill="#888")
        pv.create_text(180, 106, text="빨간 선 = 각자 지키는 골대", font=(FONT, 8), fill="#999")

    side.trace_add("write", draw_preview)
    draw_preview()

    tk.Label(root, text="② 시작하기", font=bold, anchor="w").pack(fill="x", pady=(10, 6))
    host_btn = tk.Button(root, text="방 만들기 (먼저 한 명만)", font=bold, pady=4, cursor="hand2")
    host_btn.pack(fill="x")
    info = tk.Label(root, text="", font=(FONT, 16, "bold"), fg="#1e6fe0")
    info.pack(pady=(4, 0))

    jf = tk.LabelFrame(root, text=" 또는 옆사람이 만든 방에 참가 ", font=(FONT, 9), padx=8, pady=8)
    jf.pack(fill="x", pady=(8, 0))
    tk.Label(jf, text="IP", font=(FONT, 10)).grid(row=0, column=0, sticky="w")
    ip_e = tk.Entry(jf, width=16, font=(FONT, 11))
    ip_e.grid(row=0, column=1, padx=(4, 10))
    tk.Label(jf, text="코드", font=(FONT, 10)).grid(row=0, column=2, sticky="w")
    code_e = tk.Entry(jf, width=6, font=(FONT, 11))
    code_e.grid(row=0, column=3, padx=4)
    join_btn = tk.Button(jf, text="참가하기", font=bold, cursor="hand2")
    join_btn.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))

    status = tk.Label(root, text="", font=(FONT, 10), wraplength=360, justify="left", fg="#444")
    status.pack(fill="x", pady=(10, 0))
    tk.Label(root, text="게임 중 종료: Ctrl+Shift+Q   ·   낙서판 닫기/열기: Ctrl+Shift+P",
             font=(FONT, 8), fg="#999").pack(pady=(8, 0))

    def say(text, color="#444"):
        status.config(text=text, fg=color)

    def lock(on):
        st["busy"] = on
        state = "disabled" if on else "normal"
        for w in radios + [join_btn, ip_e, code_e]:
            w.config(state=state)

    def cancel_host():
        if st["room"]:
            st["room"].close()
            st["room"] = None
        info.config(text="")
        host_btn.config(text="방 만들기 (먼저 한 명만)", command=start_host)
        lock(False)
        say("방을 닫았어요.")

    def start_host():
        me = side.get()
        if not me:
            return say("먼저 ①에서 왼쪽/오른쪽을 골라 주세요.", "#d00")
        try:
            room = HostRoom(me)
        except OSError:
            return say("방을 열 수 없어요. 이 컴퓨터에서 게임이 이미 켜져 있는지 확인해 주세요.", "#d00")
        st["room"] = room
        lock(True)
        host_btn.config(text="취소", command=cancel_host)
        info.config(text="IP %s    코드 %s" % (room.ip, room.code))
        say("옆사람에게 이 IP와 코드를 알려 주세요. 옆사람이 '%s'을 고르고 참가하면 시작돼요.\n"
            "Windows 방화벽 창이 뜨면 '허용'을 눌러 주세요." % SIDE_NAME[other_side(me)])

        def work():
            try:
                link = room.wait(on_info=lambda m: q.put(("info", m)))
                q.put(("ok", link, me))
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def start_join():
        me = side.get()
        ip, code = ip_e.get().strip(), code_e.get().strip()
        if not me:
            return say("먼저 ①에서 왼쪽/오른쪽을 골라 주세요.", "#d00")
        if not ip or not re.fullmatch(r"\d{4}", code):
            return say("옆사람 화면에 나온 IP와 4자리 코드를 넣어 주세요.", "#d00")
        lock(True)
        host_btn.config(state="disabled")
        say("접속하는 중…")

        def work():
            try:
                link, mine = join_to(ip, code, me)
                q.put(("ok", link, mine))
            except JoinError as e:
                q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                msg = q.get_nowait()
                if msg[0] == "ok":
                    st["result"] = (msg[1], msg[2])
                    root.destroy()
                    return
                if msg[0] == "info":
                    say(msg[1], "#d06000")
                elif msg[0] == "err":
                    lock(False)
                    host_btn.config(state="normal")
                    say(msg[1], "#d00")
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        if st["room"]:
            st["room"].close()
        root.destroy()

    host_btn.config(command=start_host)
    join_btn.config(command=start_join)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll)
    root.mainloop()
    return st["result"]


def main():
    ap = argparse.ArgumentParser(description="크로스 축구 - 낙서 공")
    sub = ap.add_subparsers(dest="mode")
    h = sub.add_parser("host", help="방 만들기")
    h.add_argument("--me", choices=["left", "right"], default="left",
                   help="내 모니터가 옆사람 기준 어느 쪽인지 (기본 left)")
    j = sub.add_parser("join", help="참가하기")
    j.add_argument("ip")
    j.add_argument("code")
    j.add_argument("--me", choices=["left", "right"], default=None,
                   help="내 자리 (안 주면 방장의 반대쪽)")
    args = ap.parse_args()

    if sys.platform != "win32":
        sys.exit("Windows에서만 실행돼요.")

    import tkinter as tk
    win = Win()
    win.dpi_aware()

    if args.mode is None:
        res = launcher(tk)
        if not res:
            return
        link, me = res
    elif args.mode == "host":
        try:
            room = HostRoom(args.me)
        except OSError:
            sys.exit("방을 열 수 없어요. 이 컴퓨터에서 게임이 이미 켜져 있는지 확인해 주세요.")
        print("옆사람에게 이 명령을 알려 주세요:")
        print("  python cross_soccer.py join %s %s" % (room.ip, room.code))
        print("(처음 실행하면 Windows 방화벽 창이 뜨는데 '허용'을 눌러 주세요)")
        link, me = room.wait(), args.me
    else:
        try:
            link, me = join_to(args.ip, args.code, args.me)
        except JoinError as e:
            sys.exit(str(e))
    print("연결됐어요! 내 자리: %s. 종료: Ctrl+Shift+Q, 낙서판: Ctrl+Shift+P" % SIDE_NAME[me])
    run_game(tk, win, link, me)


if __name__ == "__main__":
    main()
