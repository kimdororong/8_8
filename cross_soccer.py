#!/usr/bin/env python3
"""크로스 축구 (Windows 전용)

두 사람의 화면 위에 투명한 경기장이 떠요. 공이 내 화면 가장자리를 넘어가면
옆사람 화면에서 튀어나와요. 마우스 커서에 닿으면 공이 튕겨요.

  한 사람(왼쪽):  python cross_soccer.py host
  다른 사람(오른쪽): python cross_soccer.py join <host가 알려준 IP> <4자리 코드>

  Ctrl+Shift+Q : 언제든 종료 (상대에게도 알려요)
"""
import argparse
import json
import math
import queue
import random
import socket
import sys
import threading
import time

PORT = 48123
BALL_R = 22
CURSOR_R = 7
GOAL_FRAC = 0.2          # 골대 길이 = 화면 세로의 1/5
MIN_SPEED = 220.0
KICK_SPEED = 700.0
MAX_SPEED = 1500.0
FRICTION = 0.12
BOUNCE = 0.95
KEY = "#010203"          # 투명 처리할 색


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Field:
    """경기 규칙과 공 물리. 화면/네트워크와 상관없이 동작해요 (테스트 가능)."""

    def __init__(self, w, h, pass_edge, rng=random):
        self.w, self.h = w, h
        self.pass_edge = pass_edge           # 상대 화면과 이어진 가장자리: 'right' 또는 'left'
        self.goal_len = h * GOAL_FRAC
        self.rng = rng
        self.ball = None
        self.spin = 0.0
        self.score_me = 0
        self.score_op = 0
        self.out = []                        # 상대에게 보낼 이벤트
        self.hits = []                       # [x, y, age] 커서가 공을 친 효과
        self.serve_in = None
        self.banner = None
        self.banner_t = 0.0

    @property
    def goal_x(self):
        return 0 if self.pass_edge == "right" else self.w

    def say(self, text, secs=1.5):
        self.banner, self.banner_t = text, secs

    def serve(self, delay):
        self.serve_in = delay

    def receive_ball(self, yf, vxf, vyf):
        b_y = clamp(yf * self.h, BALL_R, self.h - BALL_R)
        vx, vy = vxf * self.w, vyf * self.h
        if self.pass_edge == "right":
            x, vx = self.w, -max(abs(vx), MIN_SPEED)
        else:
            x, vx = 0, max(abs(vx), MIN_SPEED)
        self.ball = {"x": x, "y": b_y, "vx": vx, "vy": vy}
        self.serve_in = None

    def goal_for_me(self):
        self.score_me += 1
        self.say("골! ⚽", 2.0)

    def _spawn_serve(self):
        a = self.rng.uniform(0, 2 * math.pi)
        self.ball = {"x": self.w / 2, "y": self.h / 2,
                     "vx": math.cos(a) * 320, "vy": math.sin(a) * 320}

    def update(self, dt, cur, prev):
        if self.banner:
            self.banner_t -= dt
            if self.banner_t <= 0:
                self.banner = None
        for hit in self.hits:
            hit[2] += dt
        self.hits = [hit for hit in self.hits if hit[2] < 0.35]
        if self.serve_in is not None:
            self.serve_in -= dt
            if self.serve_in <= 0:
                self.serve_in = None
                self._spawn_serve()
        b = self.ball
        if not b:
            return
        damp = max(0.0, 1 - FRICTION * dt)
        b["vx"] *= damp
        b["vy"] *= damp
        self._limit_speed(b, MIN_SPEED)
        b["x"] += b["vx"] * dt
        b["y"] += b["vy"] * dt
        self.spin += b["vx"] * dt / BALL_R
        self._cursor(b, cur, prev, dt)
        self._walls(b)

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
        self.hits.append([qx, qy, 0.0])

    def _walls(self, b):
        w, h = self.w, self.h
        if b["y"] < BALL_R:
            b["y"], b["vy"] = BALL_R, abs(b["vy"]) * BOUNCE
        elif b["y"] > h - BALL_R:
            b["y"], b["vy"] = h - BALL_R, -abs(b["vy"]) * BOUNCE
        in_goal = b["y"] <= self.goal_len
        if self.pass_edge == "right":
            if b["x"] < -BALL_R and in_goal:
                return self._conceded()
            if b["x"] < BALL_R and not in_goal:
                b["x"], b["vx"] = BALL_R, abs(b["vx"]) * BOUNCE
            if b["x"] > w:
                return self._hand_off(b)
        else:
            if b["x"] > w + BALL_R and in_goal:
                return self._conceded()
            if b["x"] > w - BALL_R and not in_goal:
                b["x"], b["vx"] = w - BALL_R, -abs(b["vx"]) * BOUNCE
            if b["x"] < 0:
                return self._hand_off(b)

    def _conceded(self):
        self.ball = None
        self.score_op += 1
        self.out.append(("goal",))
        self.say("실점… 😢", 2.0)
        self.serve_in = 2.0

    def _hand_off(self, b):
        self.out.append(("ball", b["y"] / self.h, b["vx"] / self.w, b["vy"] / self.h))
        self.ball = None


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
            self.sock.sendall((json.dumps(msg) + "\n").encode())
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
        self._pt = wintypes.POINT()
        self._was = False

    def dpi_aware(self):
        self.u.SetProcessDPIAware()

    def cursor(self):
        self.u.GetCursorPos(self.ct.byref(self._pt))
        return (self._pt.x, self._pt.y)

    def combo_pressed(self, key):
        down = all(self.u.GetAsyncKeyState(vk) & 0x8000 for vk in (0x11, 0x10, ord(key)))
        fired = down and not self._was
        self._was = down
        return fired

    def click_through(self, hwnd):
        for h in {hwnd, self.u.GetParent(hwnd)}:
            if h:
                style = self.u.GetWindowLongW(h, -20)
                # LAYERED | TRANSPARENT(클릭 통과) | TOOLWINDOW(작업표시줄에서 숨김)
                self.u.SetWindowLongW(h, -20, style | 0x80000 | 0x20 | 0x80)


class Overlay:
    FONT = ("Malgun Gothic", 11, "bold")

    def __init__(self, root, canvas, win, link, field):
        self.root, self.c, self.win, self.link, self.f = root, canvas, win, link, field
        self.last = time.monotonic()
        self.prev = None
        self.closing = False

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
        for m in self.link.poll():
            t = m.get("t")
            if t == "ball":
                self.f.receive_ball(m["y"], m["vx"], m["vy"])
            elif t == "goal":
                self.f.goal_for_me()
            elif t == "bye":
                self.f.say("상대가 나갔어요. 곧 종료해요", 1.5)
                self.root.after(1500, lambda: self.quit(False))
        cur = self.win.cursor()
        self.f.update(dt, cur, self.prev or cur)
        self.prev = cur
        for ev in self.f.out:
            if ev[0] == "ball":
                self.link.send(t="ball", y=ev[1], vx=ev[2], vy=ev[3])
            else:
                self.link.send(t="goal")
        self.f.out.clear()
        self.draw()
        self.root.after(16, self.frame)

    # --- 그리기
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
        for x, y, age in f.hits:
            r = CURSOR_R + age * 120
            c.create_oval(x - r, y - r, x + r, y + r, outline="#ffd400", width=3)
        if f.ball:
            self.draw_ball(f.ball["x"], f.ball["y"])
        self.pill(f.w / 2, 22, "나 %d  :  %d 상대" % (f.score_me, f.score_op))
        if f.banner:
            self.pill(f.w / 2, f.h / 2, f.banner, 28)

    def draw_goal(self):
        f, c = self.f, self.c
        gx = f.goal_x
        depth = 18 if gx == 0 else -18
        c.create_rectangle(gx, 0, gx + depth, f.goal_len, outline="#e02020", width=4)
        for i in range(1, 9):
            y = f.goal_len * i / 9
            c.create_line(gx, y, gx + depth, y, fill="#e02020")
        c.create_line(gx + depth, 0, gx + depth, f.goal_len, fill="#e02020", width=4)

    def draw_ball(self, x, y):
        c, r, a0 = self.c, BALL_R, self.f.spin
        c.create_oval(x - r, y - r, x + r, y + r, fill="white", outline="#111", width=2)
        pts = []
        for i in range(5):
            a = a0 + i * 2 * math.pi / 5
            pts += [x + 0.42 * r * math.cos(a), y + 0.42 * r * math.sin(a)]
            c.create_line(x + 0.42 * r * math.cos(a), y + 0.42 * r * math.sin(a),
                          x + 0.95 * r * math.cos(a), y + 0.95 * r * math.sin(a), fill="#111", width=2)
        c.create_polygon(pts, fill="#111", outline="#111")


def main():
    ap = argparse.ArgumentParser(description="크로스 축구")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("host", help="왼쪽 사람: 방 열기")
    j = sub.add_parser("join", help="오른쪽 사람: 접속하기")
    j.add_argument("ip")
    j.add_argument("code")
    args = ap.parse_args()

    if sys.platform != "win32":
        sys.exit("Windows에서만 실행돼요.")

    link = host_wait() if args.mode == "host" else join_to(args.ip, args.code)
    print("연결됐어요! 종료: Ctrl+Shift+Q")

    import tkinter as tk
    win = Win()
    win.dpi_aware()
    root = tk.Tk()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.overrideredirect(True)
    root.geometry("%dx%d+0+0" % (w, h))
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.config(bg=KEY)
    canvas = tk.Canvas(root, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
    canvas.pack()
    root.update()
    win.click_through(root.winfo_id())

    field = Field(w, h, "right" if args.mode == "host" else "left")
    if args.mode == "host":
        field.serve(3.0)
    field.say("준비!", 3.0)
    Overlay(root, canvas, win, link, field).frame()
    root.mainloop()


if __name__ == "__main__":
    main()
