#!/usr/bin/env python3
"""페널티 대결 - 키커 vs 골키퍼 (2인, 같은 와이파이의 PC 두 대)

한 명은 공을 차는 키커, 한 명은 골키퍼. 한 판씩 번갈아 맡고, 골이 많은 쪽이 이겨요.

  방향키 : 움직이기 (키커는 공을 몰고 다닐 수 있어요)
  스페이스 : [키커] 공 차기 - 누르고 있는 방향키 쪽으로 날아가요
             [골키퍼] 방향키 쪽으로 순간 다이빙
  Esc : 종료

  python penalty_duel.py                  : 시작 화면 (방 만들기 / 참가하기)
  python penalty_duel.py host
  python penalty_duel.py join <IP> <코드>
"""
import argparse
import base64
import collections
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time

import cross_soccer as cs

# ---- 경기장 (논리 좌표. 화면이 작으면 알아서 줄여서 그려요)
PW, PH = 640, 800
GOAL_L, GOAL_R = 200, 440          # 골대 안쪽 폭
GOAL_Y = 90                        # 골라인
GOAL_BACK = 46                     # 골망 뒤쪽
POST_R = 6
KEEPER_ZONE = (176, 464, 100, 208)   # x1, x2, y1, y2  골키퍼가 움직일 수 있는 곳
KICKER_MIN_Y = 430                 # 키커가 갈 수 있는 가장 위쪽 (페널티킥처럼 골대에서 먼 곳에서만 차요)

BALL_R, KICKER_R, KEEPER_R = 12, 20, 25
KICKER_SPEED = 250.0
KEEPER_SPEED = 250.0
DIVE_SPEED, DIVE_TIME, DIVE_COOLDOWN = 560.0, 0.2, 1.3
KICK_SPEED = 1100.0
KICK_REACH = KICKER_R + BALL_R + 14
BALL_FRICTION = 0.55
DRIBBLE_MAX = 420.0

ROUNDS = 10                        # 각자 5번씩
READY_T, RESULT_T = 1.8, 2.4
PLAY_TIMEOUT = 14.0
SHOT_TIMEOUT = 5.0

KICKER_COLOR, KEEPER_COLOR = "#2f6fe0", "#f2a20c"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def held_dir(inp):
    """누르고 있는 방향키를 길이 1짜리 방향으로. 안 누르면 (0, 0)."""
    dx = (1 if inp.get("r") else 0) - (1 if inp.get("l") else 0)
    dy = (1 if inp.get("d") else 0) - (1 if inp.get("u") else 0)
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n else (0.0, 0.0)


class Match:
    """경기 규칙과 물리. 화면/네트워크와 상관없이 동작해요 (테스트 가능).

    입력은 {"l","r","u","d": 눌렀는지, "kick": 스페이스 누른 횟수, "restart": 다시하기 횟수,
    "swap": 공수교대 누른 횟수}.

    역할은 한 판마다 자동으로 바뀌고, 누구든 S 키로 공수교대를 할 수 있어요.
    각자 rounds/2 번씩 찬 뒤 골이 많은 쪽이 이겨요 (비기면 연장).
    """

    def __init__(self, rng=None, rounds=ROUNDS):
        self.rng = rng or random.Random()
        self.total_rounds = rounds
        self.seen = {p: {"kick": 0, "restart": 0, "swap": 0} for p in ("host", "guest")}
        self.reset_all()

    # ---------------------------------------------------------- 진행
    def reset_all(self):
        self.round = 0
        self.score = {"host": 0, "guest": 0}
        self.kicks_taken = {"host": 0, "guest": 0}      # 지금까지 각자 찬 횟수
        self.last_kicker = None
        self.swap_next = False                          # 다음 판 역할을 바꿔 달라는 예약
        self.winner = None
        self.start_round()

    def _pick_kicker(self):
        """다음 판 키커: 덜 찬 사람, 같으면 지난 판과 반대. 공수교대 예약이 있으면 뒤집어요."""
        nh, ng = self.kicks_taken["host"], self.kicks_taken["guest"]
        if nh != ng:
            pick = "host" if nh < ng else "guest"
        elif self.last_kicker is None:
            pick = "host"
        else:
            pick = "guest" if self.last_kicker == "host" else "host"
        if self.swap_next:
            pick = "guest" if pick == "host" else "host"
            self.swap_next = False
        return pick == "host"

    def start_round(self):
        self.kicker_is_host = self._pick_kicker()
        self.kicker = {"x": PW / 2, "y": 690.0, "fx": 0.0, "fy": -1.0, "vx": 0.0, "vy": 0.0}
        self.keeper = {"x": PW / 2, "y": 130.0, "dash": 0.0, "cd": 0.0, "dx": 0.0, "dy": 0.0}
        self.ball = {"x": PW / 2, "y": 600.0, "vx": 0.0, "vy": 0.0}
        self.phase = "ready"
        self.timer = READY_T
        self.play_t = 0.0
        self.shot = False
        self.shot_t = 0.0
        self.result = None

    @property
    def kicker_key(self):
        return "host" if self.kicker_is_host else "guest"

    @property
    def keeper_key(self):
        return "guest" if self.kicker_is_host else "host"

    def _finish(self, kind, text):
        self.phase = "result"
        self.timer = RESULT_T
        self.result = {"kind": kind, "text": text}
        self.kicks_taken[self.kicker_key] += 1
        self.last_kicker = self.kicker_key
        if kind == "goal":
            self.score[self.kicker_key] += 1

    def swap_roles(self):
        """준비 중이면 이번 판 역할을 바로 바꾸고, 결과 중이면 다음 판으로 예약해요."""
        if self.phase == "ready":
            self.kicker_is_host = not self.kicker_is_host
        elif self.phase == "result":
            self.swap_next = not self.swap_next

    def _next_round(self):
        self.round += 1
        nh, ng = self.kicks_taken["host"], self.kicks_taken["guest"]
        done = nh == ng and nh >= self.total_rounds // 2
        if done and self.score["host"] != self.score["guest"]:
            self.phase = "over"
            self.winner = "host" if self.score["host"] > self.score["guest"] else "guest"
            self.timer = 0.0
        else:
            self.start_round()

    # ---------------------------------------------------------- 매 프레임
    def step(self, dt, ins):
        dt = clamp(dt, 0.0, 0.05)
        kicks, restart, swap = {}, False, False
        for p in ("host", "guest"):
            i = ins.get(p) or {}
            k, r, w = int(i.get("kick", 0)), int(i.get("restart", 0)), int(i.get("swap", 0))
            kicks[p] = k > self.seen[p]["kick"]
            restart = restart or r > self.seen[p]["restart"]
            swap = swap or w > self.seen[p]["swap"]
            self.seen[p]["kick"], self.seen[p]["restart"], self.seen[p]["swap"] = k, r, w
        for p in ("host", "guest"):
            ins.setdefault(p, {})
        if swap:
            self.swap_roles()

        if self.phase == "ready":
            self.timer -= dt
            if self.timer <= 0:
                self.phase = "play"
        elif self.phase == "play":
            self._play(dt, ins, kicks)
        elif self.phase == "result":
            self._ball_step(dt, damp=2.0)
            self.timer -= dt
            if self.timer <= 0:
                self._next_round()
        elif self.phase == "over" and restart:
            self.reset_all()

    def _play(self, dt, ins, kicks):
        self.play_t += dt
        kp, gp = self.kicker_key, self.keeper_key
        self._move_kicker(dt, ins[kp])
        self._move_keeper(dt, ins[gp], kicks[gp])
        if kicks[kp]:
            self._kick(ins[kp])
        self._ball_step(dt)
        self._dribble()
        if self._keeper_touch():
            return
        self._posts()
        if self.ball["y"] < GOAL_Y - 4 and GOAL_L < self.ball["x"] < GOAL_R:
            return self._finish("goal", "GOAL!!!")
        if self.shot:
            self.shot_t += dt
            if math.hypot(self.ball["vx"], self.ball["vy"]) < 60 or self.shot_t > SHOT_TIMEOUT:
                return self._finish("miss", "빗나갔다!")
        if self.play_t > PLAY_TIMEOUT:
            self._finish("miss", "시간 초과!")

    # ---------------------------------------------------------- 선수
    def _move_kicker(self, dt, inp):
        k = self.kicker
        dx, dy = held_dir(inp)
        if dx or dy:
            k["fx"], k["fy"] = dx, dy
        k["vx"], k["vy"] = dx * KICKER_SPEED, dy * KICKER_SPEED
        k["x"] = clamp(k["x"] + k["vx"] * dt, KICKER_R, PW - KICKER_R)
        k["y"] = clamp(k["y"] + k["vy"] * dt, KICKER_MIN_Y, PH - KICKER_R)

    def _move_keeper(self, dt, inp, dive_pressed):
        g = self.keeper
        g["cd"] = max(0.0, g["cd"] - dt)
        dx, dy = held_dir(inp)
        if dive_pressed and g["cd"] <= 0:
            if not (dx or dy):
                dx = 1.0 if self.ball["x"] >= g["x"] else -1.0    # 방향키가 없으면 공 쪽으로
            g["dash"], g["cd"], g["dx"], g["dy"] = DIVE_TIME, DIVE_COOLDOWN, dx, dy
        if g["dash"] > 0:
            g["dash"] = max(0.0, g["dash"] - dt)
            vx, vy = g["dx"] * DIVE_SPEED, g["dy"] * DIVE_SPEED
        else:
            vx, vy = dx * KEEPER_SPEED, dy * KEEPER_SPEED
        x1, x2, y1, y2 = KEEPER_ZONE
        g["x"] = clamp(g["x"] + vx * dt, x1, x2)
        g["y"] = clamp(g["y"] + vy * dt, y1, y2)

    def _kick(self, inp):
        k, b = self.kicker, self.ball
        if math.hypot(b["x"] - k["x"], b["y"] - k["y"]) > KICK_REACH:
            return
        dx, dy = held_dir(inp)
        if not (dx or dy):
            dx, dy = k["fx"], k["fy"]
        b["vx"], b["vy"] = dx * KICK_SPEED, dy * KICK_SPEED
        self.shot = True
        self.shot_t = 0.0

    # ---------------------------------------------------------- 공
    def _ball_step(self, dt, damp=1.0):
        b = self.ball
        f = max(0.0, 1 - BALL_FRICTION * damp * dt)
        b["vx"] *= f
        b["vy"] *= f
        b["x"] += b["vx"] * dt
        b["y"] += b["vy"] * dt
        r = BALL_R
        if b["y"] < GOAL_Y:                                   # 골대 안쪽: 옆 그물과 뒷그물
            lo, hi = GOAL_L + r, GOAL_R - r
            if b["x"] < lo:
                b["x"], b["vx"] = lo, abs(b["vx"]) * 0.3
            elif b["x"] > hi:
                b["x"], b["vx"] = hi, -abs(b["vx"]) * 0.3
            if b["y"] < GOAL_BACK + r:
                b["y"], b["vy"] = GOAL_BACK + r, abs(b["vy"]) * 0.2
        else:
            if b["x"] < r:
                b["x"], b["vx"] = r, abs(b["vx"]) * 0.9
            elif b["x"] > PW - r:
                b["x"], b["vx"] = PW - r, -abs(b["vx"]) * 0.9
            in_mouth = GOAL_L < b["x"] < GOAL_R
            if b["y"] < GOAL_Y + r and not in_mouth:          # 골대 밖 끝선
                b["y"], b["vy"] = GOAL_Y + r, abs(b["vy"]) * 0.9
            if b["y"] > PH - r:
                b["y"], b["vy"] = PH - r, -abs(b["vy"]) * 0.9

    def _dribble(self):
        """키커가 공에 몸으로 닿으면 굴려요 (드리블)."""
        k, b = self.kicker, self.ball
        dx, dy = b["x"] - k["x"], b["y"] - k["y"]
        d = math.hypot(dx, dy)
        mind = KICKER_R + BALL_R
        if d >= mind:
            return
        nx, ny = (dx / d, dy / d) if d > 1e-6 else (0.0, -1.0)
        b["x"], b["y"] = k["x"] + nx * mind, k["y"] + ny * mind
        approach = k["vx"] * nx + k["vy"] * ny
        if approach > 0:
            sp = min(DRIBBLE_MAX, approach * 1.25 + 40)
            b["vx"], b["vy"] = nx * sp + b["vx"] * 0.3, ny * sp + b["vy"] * 0.3

    def _keeper_touch(self):
        g, b = self.keeper, self.ball
        dx, dy = b["x"] - g["x"], b["y"] - g["y"]
        d = math.hypot(dx, dy)
        mind = KEEPER_R + BALL_R
        if d >= mind:
            return False
        nx, ny = (dx / d, dy / d) if d > 1e-6 else (0.0, 1.0)
        b["x"], b["y"] = g["x"] + nx * mind, g["y"] + ny * mind
        dot = b["vx"] * nx + b["vy"] * ny
        if dot < 0:
            b["vx"] -= 2 * dot * nx
            b["vy"] -= 2 * dot * ny
        b["vx"], b["vy"] = b["vx"] * 0.55 + nx * 90, b["vy"] * 0.55 + ny * 90
        self._finish("save", "막았다!" if self.shot else "골키퍼가 가로챘다!")
        return True

    def _posts(self):
        b = self.ball
        for px in (GOAL_L, GOAL_R):
            dx, dy = b["x"] - px, b["y"] - GOAL_Y
            d = math.hypot(dx, dy)
            mind = BALL_R + POST_R
            if d >= mind:
                continue
            nx, ny = (dx / d, dy / d) if d > 1e-6 else (0.0, 1.0)
            b["x"], b["y"] = px + nx * mind, GOAL_Y + ny * mind
            dot = b["vx"] * nx + b["vy"] * ny
            if dot < 0:
                b["vx"] -= 2 * dot * nx
                b["vy"] -= 2 * dot * ny
                b["vx"], b["vy"] = b["vx"] * 0.8, b["vy"] * 0.8

    # ---------------------------------------------------------- 네트워크용
    def to_state(self):
        r = lambda v: round(v, 1)
        k, g, b = self.kicker, self.keeper, self.ball
        return {
            "ph": self.phase, "tm": r(self.timer), "rd": self.round, "tot": self.total_rounds,
            "kh": self.kicker_is_host, "sc": [self.score["host"], self.score["guest"]],
            "k": [r(k["x"]), r(k["y"]), round(k["fx"], 2), round(k["fy"], 2)],
            "g": [r(g["x"]), r(g["y"]), g["dash"] > 0, round(g["cd"], 2)],
            "b": [r(b["x"]), r(b["y"])], "res": self.result, "win": self.winner,
            "shot": self.shot, "sw": self.swap_next,
            "kt": [self.kicks_taken["host"], self.kicks_taken["guest"]],
        }


# ---------------------------------------------------------------- 화면 (3인칭)

VIEW_W, VIEW_H = 640, 800           # 그리는 기준 크기. 창 크기에 맞춰 통째로 늘리고 줄여요
FOCAL, CAM_H, HORIZON, NEAR = 480.0, 125.0, 235.0, 25.0
BACK_KICKER, BACK_KEEPER = 175.0, 130.0      # 캐릭터 뒤에서 카메라까지 거리
PERSON_H = 64.0                    # 캐릭터 키 (세계 단위)
GOAL_TOP = 60.0                    # 골대 높이
ASSET_DIR = "assets"               # 여기에 kicker_back.png 등이 있으면 그 그림을 써요
ASSET_NAMES = ("kicker_back", "kicker_front", "keeper_back", "keeper_front", "player_back", "player_front")
WALK_NAMES = tuple("%s_walk%d" % (n, i) for n in ASSET_NAMES for i in (1, 2))   # 걷는 동작 그림 (선택)
ALL_ASSET_NAMES = ASSET_NAMES + WALK_NAMES
KICK_TIME = 0.25
OTHER_COLOR = {"kicker": "#2f6fe0", "keeper": "#f2a20c"}


def pick_scale(screen_w, screen_h):
    return min(1.0, (screen_h - 110) / VIEW_H, (screen_w - 40) / VIEW_W)


class Walker:
    """캐릭터가 움직이는 만큼 걸음 위상(phase)과 세기(amp)를 계산해요. 다리 움직임에 써요."""

    def __init__(self):
        self.x = self.y = None
        self.phase, self.amp, self.kick = 0.0, 0.0, 0.0

    def update(self, x, y, dt):
        if self.x is None:
            self.x, self.y = x, y
        d = math.hypot(x - self.x, y - self.y)
        self.x, self.y = x, y
        moving = d / max(dt, 1e-3) > 25
        self.amp += ((1.0 if moving else 0.0) - self.amp) * min(1.0, dt * 12)
        self.phase += d * 0.085
        self.kick = max(0.0, self.kick - dt)

    def kicked(self):
        self.kick = KICK_TIME

    def swing(self, side):
        """side=-1(왼다리) / +1(오른다리)의 발 들림 0~1."""
        return max(0.0, math.sin(self.phase + (0.0 if side < 0 else math.pi))) * self.amp

    def bob(self):
        return abs(math.sin(self.phase)) * self.amp


class View:
    """캐릭터 뒤에 놓인 카메라. flip=True면 반대편(골키퍼)에서 골대 반대쪽을 봐요."""

    def __init__(self, cam_x, cam_y, flip):
        self.cam_x, self.cam_y, self.flip = cam_x, cam_y, flip

    def depth(self, y):
        return (y - self.cam_y) if self.flip else (self.cam_y - y)

    def lat(self, x):
        return -(x - self.cam_x) if self.flip else (x - self.cam_x)

    def proj(self, x, y, z=0.0):
        """세계 좌표(x, y, 높이 z) -> (화면 x, 화면 y, 크기 배율). 카메라 뒤쪽이면 None."""
        f = self.depth(y)
        if f < NEAR - 1e-6:
            return None
        k = FOCAL / f
        return (VIEW_W / 2 + self.lat(x) * k, HORIZON + (CAM_H - z) * k, k)

    def ground_poly(self, pts):
        """바닥 위 다각형(세계 좌표)을 화면 좌표 목록으로. 카메라 뒤쪽은 잘라 내요."""
        out, n = [], len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            fa, fb = self.depth(a[1]), self.depth(b[1])
            ina, inb = fa >= NEAR, fb >= NEAR
            if ina:
                out.append(a)
            if ina != inb:
                t = (NEAR - fa) / (fb - fa)
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        flat = []
        for x, y in out:
            p = self.proj(x, y)
            if p:
                flat += [p[0], p[1]]
        return flat if len(flat) >= 6 else []


def line_quad(x1, y1, x2, y2, w):
    """바닥에 그리는 굵은 선을 사각형으로 (멀리 갈수록 자연스럽게 가늘어져요)."""
    dx, dy = x2 - x1, y2 - y1
    n = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / n * w / 2, dx / n * w / 2
    return [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]


def flip_keys(l, r, u, d):
    """골키퍼 화면은 180도 돌아가 있어서, 눌린 방향키를 세계 방향으로 바꿔요."""
    return r, l, d, u


def best_fraction(ratio, max_den=8, max_num=16):
    """이미지 크기를 (a/b)배로 맞추기 위한 정수 zoom(a), subsample(b) 조합."""
    best = (1, 1, 1e9)
    for b in range(1, max_den + 1):
        a = max(1, min(max_num, round(ratio * b)))
        err = abs(a / b - ratio) / ratio
        if err < best[2]:
            best = (a, b, err)
    return best[0], best[1]


def load_assets(tk, folder=None):
    """assets/ 폴더의 PNG 캐릭터 그림을 읽어요. 없는 그림은 코드로 그린 캐릭터를 써요."""
    import os
    folder = folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), ASSET_DIR)
    found = {}
    for name in ALL_ASSET_NAMES:
        path = os.path.join(folder, name + ".png")
        if os.path.isfile(path):
            try:
                found[name] = tk.PhotoImage(file=path)
            except tk.TclError:
                pass
    return found


SHARE_MAX_BYTES = 300_000          # 상대에게 보낼 그림 한 장의 최대 크기 (넘으면 줄여서 보내요)
SHARE_MAX_B64 = 520_000            # 받을 때 허용하는 글자 수
SHARE_MAX_PX = 2000                # 받을 그림의 최대 가로/세로


def png_size(raw):
    """PNG 파일 앞부분에서 (가로, 세로)를 읽어요. PNG가 아니면 None."""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", raw[16:24])


def read_share_bytes(tk, path):
    """보낼 PNG 내용. 너무 크면 Tk로 줄여서 만들어요. 못 보내는 그림이면 None."""
    with open(path, "rb") as f:
        raw = f.read()
    size = png_size(raw)
    if not size:
        return None
    big = max(size)
    if len(raw) <= SHARE_MAX_BYTES and big <= SHARE_MAX_PX:
        return raw
    try:
        img = tk.PhotoImage(file=path)
    except tk.TclError:
        return None
    start = max(1, -(-big // 1600))
    for f in range(start, start + 8):
        tmp = os.path.join(tempfile.gettempdir(), "penalty_share_%d.png" % os.getpid())
        try:
            img.subsample(f).write(tmp, format="png")
            with open(tmp, "rb") as fh:
                data = fh.read()
        except (tk.TclError, OSError):
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if len(data) <= SHARE_MAX_BYTES:
            return data
    return None


def changed_names(old_sig, new_sig):
    return [n for (n, m1, s1), (_, m2, s2) in zip(old_sig, new_sig) if (m1, s1) != (m2, s2)]


def resolve_names(role, kind, suffix=""):
    """역할별 그림이 없으면 공통 그림(player_*)을 써요."""
    return ["%s_%s%s" % (role, kind, suffix), "player_%s%s" % (kind, suffix)]


def assets_signature(folder=None):
    """assets 폴더의 그림 파일이 바뀌었는지 알아보기 위한 (이름, 수정시각, 크기) 목록."""
    import os
    folder = folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), ASSET_DIR)
    sig = []
    for name in ALL_ASSET_NAMES:
        path = os.path.join(folder, name + ".png")
        try:
            st = os.stat(path)
            sig.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((name, None, None))
    return tuple(sig)


class App:
    def __init__(self, tk, root, link, is_host, asset_dir=None):
        self.tk, self.root, self.link, self.is_host = tk, root, link, is_host
        self.me = "host" if is_host else "guest"
        self.match = Match() if is_host else None
        self.state = None
        self.remote_in = {"l": 0, "r": 0, "u": 0, "d": 0, "kick": 0, "restart": 0, "swap": 0}
        self.keys, self.kicks, self.restarts, self.swaps = set(), 0, 0, 0
        self.closing = False
        self.fullscreen = False
        self.last = time.monotonic()
        self.prev_phase, self.prev_round = None, -1
        self.s, self.ox, self.oy = 1.0, 0.0, 0.0
        self.asset_dir = asset_dir
        self.assets = load_assets(tk, asset_dir)
        self.asset_sig = assets_signature(asset_dir)
        self.peer_assets = {}                                    # 상대가 보내 준 캐릭터 그림
        self.share_queue = collections.deque()
        self.img_cache, self.img_refs = {}, []
        self.frame_n, self.toast, self.toast_until = 0, "", 0.0
        self.queue_share([n for n, m, _ in self.asset_sig if m is not None])
        self.walkers = {"kicker": Walker(), "keeper": Walker()}
        self.prev_shot, self.anim_t = False, time.monotonic()
        rng = random.Random(5)
        self.crowd = [(rng.uniform(0, VIEW_W), rng.uniform(0, 52), rng.choice(
            ("#d9534f", "#f0ad4e", "#5bc0de", "#f7f7f7", "#9b59b6", "#2ecc71"))) for _ in range(160)]

        sc = pick_scale(root.winfo_screenwidth(), root.winfo_screenheight())
        root.title("페널티 대결 ⚽🧤")
        root.geometry("%dx%d" % (int(VIEW_W * sc), int(VIEW_H * sc)))
        root.minsize(320, 400)
        root.resizable(True, True)                               # 키우고 줄이고 최대화 모두 돼요
        self.c = tk.Canvas(root, bg="#10261a", highlightthickness=0)
        self.c.pack(fill="both", expand=True)
        root.bind("<KeyPress>", self.on_press)
        root.bind("<KeyRelease>", self.on_release)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.focus_force()
        cs.prepare_fanfare()

    # --- 키 입력
    def on_press(self, e):
        k = e.keysym
        if k == "Escape":
            return self.quit()
        if k == "F5":
            self.reload_assets(force=True)
        elif k == "F11":
            self.fullscreen = not self.fullscreen
            self.root.attributes("-fullscreen", self.fullscreen)
        elif k in ("Up", "Down", "Left", "Right"):
            self.keys.add(k)
        elif k == "space" and "space" not in self.keys:     # 누르고 있어도 한 번만
            self.keys.add("space")
            self.kicks += 1
        elif k in ("r", "R") and "r" not in self.keys:
            self.keys.add("r")
            self.restarts += 1
        elif k in ("s", "S") and "s" not in self.keys:
            self.keys.add("s")
            self.swaps += 1

    def on_release(self, e):
        self.keys.discard({"r": "r", "R": "r", "s": "s", "S": "s"}.get(e.keysym, e.keysym))

    def queue_share(self, names):
        """바뀐 내 그림을 상대에게 보낼 목록에 넣어요. 지운 그림은 지웠다고 알려요."""
        folder = self.asset_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), ASSET_DIR)
        for name in names:
            path = os.path.join(folder, name + ".png")
            data = None
            if os.path.isfile(path):
                try:
                    raw = read_share_bytes(self.tk, path)
                except OSError:
                    raw = None
                if raw is None:
                    continue
                data = base64.b64encode(raw).decode("ascii")
            self.share_queue = collections.deque(x for x in self.share_queue if x[0] != name)
            self.share_queue.append((name, data))

    def recv_asset(self, m):
        """상대가 보낸 캐릭터 그림을 검사하고 받아요."""
        name, data = m.get("name"), m.get("data")
        if name not in ALL_ASSET_NAMES:
            return
        if data is None:
            self.peer_assets.pop(name, None)
        else:
            if not isinstance(data, str) or len(data) > SHARE_MAX_B64:
                return
            try:
                raw = base64.b64decode(data, validate=True)
            except ValueError:
                return
            size = png_size(raw)
            if not size or not (0 < size[0] <= SHARE_MAX_PX and 0 < size[1] <= SHARE_MAX_PX):
                return
            try:
                self.peer_assets[name] = self.tk.PhotoImage(master=self.root, data=data)
            except self.tk.TclError:
                return
            self.toast, self.toast_until = "상대 캐릭터 그림을 받았어요", time.monotonic() + 2.5
        for key in [k for k in self.img_cache if k[0] == "peer:" + name]:
            self.img_cache.pop(key)

    def reload_assets(self, force=False):
        """캐릭터 그림 파일이 바뀌었으면(또는 F5) 다시 불러오고 상대에게도 보내요."""
        sig = assets_signature(self.asset_dir)
        if sig == self.asset_sig and not force:
            return False
        self.queue_share([n for n, m, _ in sig] if force else changed_names(self.asset_sig, sig))
        self.asset_sig = sig
        self.assets = load_assets(self.tk, self.asset_dir)
        self.img_cache.clear()
        self.img_refs.clear()
        self.toast = "캐릭터 그림을 다시 불러왔어요 (%d/%d)" % (
            sum(1 for n in self.assets if n in ASSET_NAMES), len(ASSET_NAMES))
        self.toast_until = time.monotonic() + 2.5
        return True

    def i_am_kicker(self):
        st = self.state
        return bool(st) and "kh" in st and st["kh"] == self.is_host

    def my_input(self):
        k = self.keys
        l, r, u, d = ("Left" in k, "Right" in k, "Up" in k, "Down" in k)
        if self.state and "kh" in self.state and not self.i_am_kicker():
            l, r, u, d = flip_keys(l, r, u, d)               # 골키퍼는 화면이 뒤집혀 있어요
        return {"l": int(l), "r": int(r), "u": int(u), "d": int(d),
                "kick": self.kicks, "restart": self.restarts, "swap": self.swaps}

    def quit(self):
        if self.closing:
            return
        self.closing = True
        self.link.send(t="bye")
        self.link.close()
        self.root.destroy()

    # --- 매 프레임
    def frame(self):
        if self.closing:
            return
        now = time.monotonic()
        dt, self.last = min(now - self.last, 0.05), now
        self.frame_n += 1
        if self.frame_n % 45 == 0:                               # 0.7초마다 그림 파일이 바뀌었는지 봐요
            self.reload_assets()
        for m in self.link.poll():
            t = m.get("t")
            if t == "bye":
                self.state = self.state or {}
                self.root.after(100, self.opponent_left)
            elif t == "asset":
                self.recv_asset(m)
            elif t == "in" and self.is_host:
                for key in self.remote_in:
                    try:
                        self.remote_in[key] = int(m.get(key, 0))
                    except (TypeError, ValueError):
                        pass
            elif t == "st" and not self.is_host:
                self.state = m
        if self.is_host:
            self.match.step(dt, {"host": self.my_input(), "guest": dict(self.remote_in)})
            self.state = self.match.to_state()
            self.link.send(t="st", **self.state)
        else:
            self.link.send(t="in", **self.my_input())
        if self.share_queue and self.frame_n % 3 == 0:            # 그림은 조금씩 나눠서 보내요
            name, data = self.share_queue.popleft()
            self.link.send(t="asset", name=name, data=data)
        if self.state and "ph" in self.state:
            self.react(self.state)
            self.draw(self.state)
        self.root.after(16, self.frame)

    def opponent_left(self):
        w, h = self.c.winfo_width(), self.c.winfo_height()
        self.c.create_text(w / 2, h / 2, text="상대가 나갔어요", fill="white", font=(cs.FONT, 28, "bold"))
        self.root.after(1500, self.quit)

    def react(self, st):
        """라운드 결과가 나온 순간 소리를 내요."""
        if st["ph"] == "result" and (self.prev_phase != "result" or self.prev_round != st["rd"]):
            kind = (st.get("res") or {}).get("kind")
            if kind == "goal":
                cs.play_fanfare()
            else:
                beep_sad()
        self.prev_phase, self.prev_round = st["ph"], st["rd"]

    # --- 그리기 도구 (기준 좌표 640x800 -> 지금 창 크기)
    def sx(self, x):
        return self.ox + x * self.s

    def sy(self, y):
        return self.oy + y * self.s

    def flat(self, pts):
        return [self.sx(v) if i % 2 == 0 else self.sy(v) for i, v in enumerate(pts)]

    def poly(self, pts, fill, outline=""):
        if pts:
            self.c.create_polygon(*self.flat(pts), fill=fill, outline=outline)

    def line(self, pts, fill, width=1.0, **kw):
        self.c.create_line(*self.flat(pts), fill=fill, width=max(1, width * self.s), **kw)

    def rect(self, x1, y1, x2, y2, fill, outline="", width=1.0):
        self.c.create_rectangle(self.sx(x1), self.sy(y1), self.sx(x2), self.sy(y2), fill=fill,
                                outline=outline, width=max(1, width * self.s) if outline else 0)

    def oval(self, cx, cy, rx, ry, fill, outline="", width=1.0):
        self.c.create_oval(self.sx(cx - rx), self.sy(cy - ry), self.sx(cx + rx), self.sy(cy + ry),
                           fill=fill, outline=outline, width=max(1, width * self.s) if outline else 0)

    def text(self, x, y, s, size=14, color="white", anchor="center", outline=True):
        f = (cs.FONT, -max(8, int(size * 1.35 * self.s)), "bold")           # 음수 = 픽셀 크기
        if outline:
            for dx, dy in ((-1, -1), (1, 1), (-1, 1), (1, -1)):
                self.c.create_text(self.sx(x) + dx * 2, self.sy(y) + dy * 2, text=s, font=f, fill="#123",
                                   anchor=anchor)
        self.c.create_text(self.sx(x), self.sy(y), text=s, font=f, fill=color, anchor=anchor)

    # --- 3인칭 장면
    def scene(self, v, st, me_kicker):
        c = self.c
        # 하늘 + 관중석
        self.rect(0, 0, VIEW_W, HORIZON, "#8ecbff")
        self.rect(0, HORIZON - 52, VIEW_W, HORIZON, "#46586b")
        for x, y, col in self.crowd:
            self.rect(x, HORIZON - 52 + y, x + 5, HORIZON - 52 + y + 4, col)
        self.rect(0, HORIZON - 3, VIEW_W, HORIZON + 1, "#2a3948")
        self.rect(0, HORIZON, VIEW_W, VIEW_H, "#27703a")                      # 경기장 밖 잔디
        # 잔디 줄무늬
        for i, y0 in enumerate(range(GOAL_Y - 80, 790 + 80, 80)):
            self.poly(v.ground_poly([(10, max(GOAL_Y, y0)), (630, max(GOAL_Y, y0)),
                                     (630, min(790, y0 + 80)), (10, min(790, y0 + 80))])
                      if min(790, y0 + 80) > max(GOAL_Y, y0) else [],
                      "#2e8b3d" if i % 2 == 0 else "#37994a")
        # 경기장 선
        white = "#f4f4f4"
        segs = [(10, GOAL_Y, 630, GOAL_Y), (630, GOAL_Y, 630, 790), (630, 790, 10, 790), (10, 790, 10, GOAL_Y),
                (GOAL_L - 60, GOAL_Y, GOAL_L - 60, 250), (GOAL_L - 60, 250, GOAL_R + 60, 250),
                (GOAL_R + 60, 250, GOAL_R + 60, GOAL_Y), (GOAL_L - 20, GOAL_Y, GOAL_L - 20, 165),
                (GOAL_L - 20, 165, GOAL_R + 20, 165), (GOAL_R + 20, 165, GOAL_R + 20, GOAL_Y)]
        for x1, y1, x2, y2 in segs:
            self.poly(v.ground_poly(line_quad(x1, y1, x2, y2, 3.5)), white)
        yl = KICKER_MIN_Y - KICKER_R                                          # 슛 라인 (노란 점선)
        for x in range(10, 630, 36):
            self.poly(v.ground_poly(line_quad(x, yl, min(x + 20, 630), yl, 3)), "#ffe14a")
        self.goal(v)

        # 멀리 있는 것부터 그려요
        k, g, b = st["k"], st["g"], st["b"]
        items = [("keeper", g[0], g[1]), ("kicker", k[0], k[1]), ("ball", b[0], b[1])]
        items.sort(key=lambda it: -v.depth(it[2]))
        for kind, x, y in items:
            p = v.proj(x, y)
            if not p:
                continue
            if kind == "ball":
                self.ball(v, x, y)
            else:
                mine = (kind == "kicker") == me_kicker
                self.person(v, x, y, kind, "back" if mine else "front", dashing=(kind == "keeper" and g[2]))
        if me_kicker and st["ph"] == "play" and not st.get("shot"):
            self.aim_arrow(v, st)

    def goal(self, v):
        net = "#cfd8d2"
        z = GOAL_TOP
        if not v.flip:                                                        # 골키퍼 눈높이에선 그물이 화면을 덮어서 생략
            for x in range(GOAL_L + 20, GOAL_R, 20):
                a, bb = v.proj(x, GOAL_BACK, 0), v.proj(x, GOAL_BACK, z)
                if a and bb:
                    self.line([a[0], a[1], bb[0], bb[1]], net)
            for zz in range(10, int(z), 10):
                a, bb = v.proj(GOAL_L, GOAL_BACK, zz), v.proj(GOAL_R, GOAL_BACK, zz)
                if a and bb:
                    self.line([a[0], a[1], bb[0], bb[1]], net)
            for x in (GOAL_L, GOAL_R):
                a, bb = v.proj(x, GOAL_BACK, z), v.proj(x, GOAL_Y, z)
                if a and bb:
                    self.line([a[0], a[1], bb[0], bb[1]], net, 2)
        post = "#ffffff"
        tops = {}
        for x in (GOAL_L, GOAL_R):
            a, bb = v.proj(x, GOAL_Y, 0), v.proj(x, GOAL_Y, z)
            if a and bb:
                self.line([a[0], a[1], bb[0], bb[1]], post, 6 * a[2] ** 0.5 + 1)
                tops[x] = bb
        if len(tops) == 2:
            self.line([tops[GOAL_L][0], tops[GOAL_L][1], tops[GOAL_R][0], tops[GOAL_R][1]], post,
                      6 * tops[GOAL_L][2] ** 0.5 + 1)

    def ball(self, v, x, y):
        p, g = v.proj(x, y, BALL_R), v.proj(x, y, 0)
        if not (p and g):
            return
        r = BALL_R * p[2]
        self.oval(g[0], g[1], r * 0.95, r * 0.28, "#1d5a26")                 # 그림자
        self.oval(p[0], p[1], r, r, "white", "#111", 2)
        self.oval(p[0], p[1], r * 0.36, r * 0.36, "#111")
        for i in range(5):
            a = i * 2 * math.pi / 5 + 0.3
            self.line([p[0] + math.cos(a) * r * 0.36, p[1] + math.sin(a) * r * 0.36,
                       p[0] + math.cos(a) * r * 0.92, p[1] + math.sin(a) * r * 0.92], "#111", 1.5)

    def aim_arrow(self, v, st):
        kx, ky, fx, fy = st["k"]
        bx, by = st["b"]
        if math.hypot(bx - kx, by - ky) > KICK_REACH + 6:
            return
        dx, dy = held_dir({"l": "Left" in self.keys, "r": "Right" in self.keys,
                           "u": "Up" in self.keys, "d": "Down" in self.keys})
        if not (dx or dy):
            dx, dy = fx, fy
        ex, ey = bx + dx * 95, by + dy * 95
        self.poly(v.ground_poly(line_quad(bx, by, ex, ey, 7)), "#ffe14a")
        px, py = -dy, dx
        self.poly(v.ground_poly([(ex + dx * 26, ey + dy * 26), (ex + px * 14, ey + py * 14),
                                 (ex - px * 14, ey - py * 14)]), "#ffe14a")

    # --- 캐릭터 (assets/ 그림이 있으면 그 그림, 없으면 코드로 그린 캐릭터)
    def pick_image(self, role, kind, suffix=""):
        """이 캐릭터에 쓸 그림. 내 캐릭터(뒷모습)는 내 그림, 상대(앞모습)는 상대가 보낸 그림을 먼저 써요."""
        names = resolve_names(role, kind, suffix)
        if kind == "back":
            sources = (("me:", self.assets),)
        else:
            sources = (("peer:", self.peer_assets), ("me:", self.assets))
        for prefix, pool in sources:
            for n in names:
                if n in pool:
                    return prefix + n, pool[n]
        return None

    def sprite(self, pick, target_h):
        if not pick:
            return None
        key, base = pick
        a, b = best_fraction(max(0.05, target_h / max(1, base.height())))
        ck = (key, a, b)
        img = self.img_cache.get(ck)
        if img is None:
            img = base.zoom(a) if a > 1 else base
            img = img.subsample(b) if b > 1 else img
            if len(self.img_cache) > 80:
                self.img_cache.clear()
                self.img_refs.clear()
            self.img_cache[ck] = img
            self.img_refs.append(img)
        return img

    def person(self, v, x, y, role, kind, dashing=False):
        p = v.proj(x, y)
        if not p:
            return
        px, py, k = p
        h = PERSON_H * k
        w = self.walkers[role]
        bob = w.bob() * h * 0.03                                               # 걸을 때 몸이 통통 튀어요
        self.oval(px, py, h * 0.3, h * 0.06, "#1d5a26")                        # 그림자
        if dashing:                                                            # 다이빙 잔상
            for i in (1, 2, 3):
                self.line([px - i * h * 0.16, py - h * 0.6, px - i * h * 0.16 - h * 0.2, py - h * 0.6], "#ffffff", 2)
        pick = None
        if w.amp > 0.35:                                                       # 걷는 동작 그림이 있으면 번갈아 써요
            pick = self.pick_image(role, kind, "_walk%d" % (1 if math.sin(w.phase) > 0 else 2))
        img = self.sprite(pick or self.pick_image(role, kind), h * self.s)
        if img:
            self.c.create_image(self.sx(px), self.sy(py - bob), image=img, anchor="s")
            return
        keeper = role == "keeper"
        shirt = KEEPER_COLOR if keeper else KICKER_COLOR
        shirt_dark = "#7a4b00" if keeper else "#12336e"
        shorts = "#222222" if keeper else "#f2f2f2"
        skin, hair = "#f0c9a0", "#3b2a1c"
        kick = math.sin(math.pi * (1 - w.kick / KICK_TIME)) if w.kick > 0 else 0.0
        top = py - bob                                                         # 몸통 기준선 (다리는 땅에 붙어요)
        for sxl in (-1, 1):                                                    # 다리 + 양말
            lift = w.swing(sxl) * h * 0.1 + (kick * h * 0.2 if sxl == 1 else 0.0)
            lx = px + sxl * h * 0.1
            self.rect(lx - h * 0.05, top - h * 0.3, lx + h * 0.05, py - h * 0.08 - lift, skin)
            self.rect(lx - h * 0.055, py - h * 0.12 - lift, lx + h * 0.055, py - lift, shirt)
        self.rect(px - h * 0.2, top - h * 0.4, px + h * 0.2, top - h * 0.26, shorts, "#666", 1)   # 반바지
        self.poly([px - h * 0.2, top - h * 0.4, px + h * 0.2, top - h * 0.4, px + h * 0.26, top - h * 0.72,
                   px - h * 0.26, top - h * 0.72], shirt, shirt_dark)                      # 상의
        for sxl in (-1, 1):                                                    # 팔 (다리와 반대로 흔들려요)
            ax = px + sxl * h * 0.31
            sw = w.swing(-sxl) * h * 0.06
            self.rect(ax - h * 0.05, top - h * 0.72, ax + h * 0.05, top - h * 0.44 - sw, shirt, shirt_dark)
            if keeper:
                self.oval(ax, top - h * 0.42 - sw, h * 0.075, h * 0.075, "white", "#555", 1)   # 장갑
            else:
                self.oval(ax, top - h * 0.42 - sw, h * 0.05, h * 0.05, skin)
        hy = top - h * 0.85
        self.oval(px, hy, h * 0.135, h * 0.135, hair if kind == "back" else skin, "#222", 1.5)
        if kind == "back":
            self.text(px, top - h * 0.56, "1" if keeper else "9", max(8, int(h * 0.2)), "white", outline=False)
            for sxl in (-1, 1):                                                # 귀
                self.oval(px + sxl * h * 0.135, hy + h * 0.01, h * 0.03, h * 0.04, skin)
        else:
            self.c.create_arc(self.sx(px - h * 0.135), self.sy(hy - h * 0.135), self.sx(px + h * 0.135),
                              self.sy(hy + h * 0.135), start=15, extent=150, fill=hair, outline=hair)
            for sxl in (-1, 1):
                self.oval(px + sxl * h * 0.05, hy + h * 0.01, h * 0.018, h * 0.024, "#222")
            self.c.create_arc(self.sx(px - h * 0.05), self.sy(hy + h * 0.03), self.sx(px + h * 0.05),
                              self.sy(hy + h * 0.10), start=200, extent=140, style="arc", outline="#a33",
                              width=max(1, 1.5 * self.s))
        if kind == "back":                                                     # 내 캐릭터 표시
            self.text(px, py + h * 0.13, "▲ 나", max(8, int(h * 0.14)), "#ffe14a")

    def animate(self, st):
        """상태가 바뀔 때마다 걷기/차기 애니메이션 값을 갱신해요."""
        now = time.monotonic()
        dt, self.anim_t = min(now - self.anim_t, 0.1), now
        self.walkers["kicker"].update(st["k"][0], st["k"][1], dt)
        gw = self.walkers["keeper"]
        gw.update(st["g"][0], st["g"][1], dt)
        if st["g"][2]:
            gw.amp = 1.0
        if st.get("shot") and not self.prev_shot:
            self.walkers["kicker"].kicked()
        self.prev_shot = bool(st.get("shot"))

    # --- 전체 그리기
    def draw(self, st):
        c = self.c
        cw, ch = max(2, c.winfo_width()), max(2, c.winfo_height())
        self.s = min(cw / VIEW_W, ch / VIEW_H)
        self.ox, self.oy = (cw - VIEW_W * self.s) / 2, (ch - VIEW_H * self.s) / 2
        c.delete("all")
        self.animate(st)
        me_kicker = self.i_am_kicker()
        k, g = st["k"], st["g"]
        if me_kicker:
            v = View(k[0], k[1] + BACK_KICKER, False)
        else:
            v = View(g[0], g[1] - BACK_KEEPER, True)
        self.scene(v, st, me_kicker)
        cd = g[3]
        my_score, op_score = (st["sc"][0], st["sc"][1]) if self.is_host else (st["sc"][1], st["sc"][0])
        self.text(VIEW_W / 2, 18, "나 %d  :  %d 상대" % (my_score, op_score), 17)
        rd_label = "라운드 %d/%d" % (st["rd"] + 1, st["tot"]) if st["rd"] < st["tot"] else "연장전"
        self.text(12, 18, rd_label, 11, anchor="w")
        self.text(VIEW_W - 12, 18, "나: " + ("키커 ⚽" if me_kicker else "골키퍼 🧤"), 11, anchor="e")
        hint = ("방향키: 이동   스페이스: 차기 (누른 방향으로 날아가요)" if me_kicker
                else "방향키: 이동 (화면 기준)   스페이스: 다이빙 (%s)" % ("준비됨" if cd <= 0 else "충전 중"))
        self.text(VIEW_W / 2, VIEW_H - 22, hint, 11)
        mine_i = 0 if self.is_host else 1
        kt = st.get("kt", [0, 0])
        self.text(12, 42, "내 슛 %d/%d  ·  상대 슛 %d/%d" % (kt[mine_i], st["tot"] // 2, kt[1 - mine_i], st["tot"] // 2),
                  10, "#dfe", anchor="w")
        if time.monotonic() < self.toast_until:
            self.text(VIEW_W / 2, VIEW_H - 50, self.toast, 12, "#9dffb0")
        ph, res = st["ph"], st.get("res")
        swap_note = "S 키: 공수교대" + ("  (다음 판 교대 예약됨)" if st.get("sw") else "")
        if ph == "ready":
            self.banner("라운드 %d" % (st["rd"] + 1), "당신은 %s!   ·   %s" % (
                "키커 ⚽" if me_kicker else "골키퍼 🧤", "S 키: 공수교대"))
        elif ph == "result" and res:
            colors = {"goal": "#ffdd33", "save": "#7fd6ff", "miss": "#dddddd"}
            self.banner(res["text"], swap_note, colors.get(res["kind"], "white"), big=res["kind"] == "goal")
        elif ph == "over":
            mine = (st.get("win") == "host") == self.is_host
            self.banner("승리! 🏆" if mine else "패배…", "%d : %d   ·   R 키로 다시 하기, Esc로 종료" % (
                my_score, op_score), "#ffdd33" if mine else "#ff8080", big=True)

    def banner(self, title, sub=None, color="white", big=False):
        y = VIEW_H * 0.46
        self.rect(30, y - 70, VIEW_W - 30, y + (60 if sub else 40), "#0d2f18", "#ffffff", 2)
        self.text(VIEW_W / 2, y - (14 if sub else 0), title, 44 if big else 34, color)
        if sub:
            self.text(VIEW_W / 2, y + 34, sub, 15)


def beep_sad():
    try:
        import winsound
    except ImportError:
        return

    def run():
        for f, ms in ((330, 160), (262, 260)):
            try:
                winsound.Beep(f, ms)
            except RuntimeError:
                return
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- 시작 화면

def lobby(tk):
    """방 만들기 / 참가하기. 연결되면 (Link, is_host), 창을 닫으면 None."""
    import queue
    import re
    root = tk.Tk()
    root.title("페널티 대결")
    root.resizable(False, False)
    root.configure(padx=24, pady=18)
    q, st = queue.Queue(), {"room": None, "result": None}
    bold = (cs.FONT, 11, "bold")

    tk.Label(root, text="⚽ 페널티 대결 🧤", font=(cs.FONT, 20, "bold")).pack()
    tk.Label(root, text="키커 vs 골키퍼 · 한 판씩 번갈아 · 골이 많은 쪽이 승리",
             fg="#666", font=(cs.FONT, 10)).pack(pady=(0, 14))
    host_btn = tk.Button(root, text="방 만들기 (먼저 한 명만)", font=bold, pady=6, cursor="hand2")
    host_btn.pack(fill="x")
    info = tk.Label(root, text="", font=(cs.FONT, 16, "bold"), fg="#1e6fe0")
    info.pack(pady=(6, 0))
    jf = tk.LabelFrame(root, text=" 또는 옆사람이 만든 방에 참가 ", font=(cs.FONT, 9), padx=8, pady=8)
    jf.pack(fill="x", pady=(10, 0))
    tk.Label(jf, text="IP", font=(cs.FONT, 10)).grid(row=0, column=0, sticky="w")
    ip_e = tk.Entry(jf, width=16, font=(cs.FONT, 11))
    ip_e.grid(row=0, column=1, padx=(4, 10))
    tk.Label(jf, text="코드", font=(cs.FONT, 10)).grid(row=0, column=2, sticky="w")
    code_e = tk.Entry(jf, width=6, font=(cs.FONT, 11))
    code_e.grid(row=0, column=3, padx=4)
    join_btn = tk.Button(jf, text="참가하기", font=bold, cursor="hand2")
    join_btn.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
    status = tk.Label(root, text="", font=(cs.FONT, 10), wraplength=360, justify="left", fg="#444")
    status.pack(fill="x", pady=(10, 0))

    def say(t, color="#444"):
        status.config(text=t, fg=color)

    def lock(on):
        for w in (join_btn, ip_e, code_e):
            w.config(state="disabled" if on else "normal")

    def cancel():
        if st["room"]:
            st["room"].close()
            st["room"] = None
        info.config(text="")
        host_btn.config(text="방 만들기 (먼저 한 명만)", command=start_host)
        lock(False)
        say("방을 닫았어요.")

    def start_host():
        try:
            room = cs.HostRoom("left")            # 방장 = 'left', 참가자 = 'right' (게임에서는 의미 없어요)
        except OSError:
            return say("방을 열 수 없어요. 이 컴퓨터에서 이미 켜져 있는지 확인해 주세요.", "#d00")
        st["room"] = room
        lock(True)
        host_btn.config(text="취소", command=cancel)
        info.config(text="IP %s    코드 %s" % (room.ip, room.code))
        say("옆사람에게 이 IP와 코드를 알려 주세요. 방화벽 창이 뜨면 '허용'을 눌러 주세요.")

        def work():
            try:
                q.put(("ok", room.wait(on_info=lambda m: q.put(("info", m))), True))
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def start_join():
        ip, code = ip_e.get().strip(), code_e.get().strip()
        if not ip or not re.fullmatch(r"\d{4}", code):
            return say("옆사람 화면에 나온 IP와 4자리 코드를 넣어 주세요.", "#d00")
        lock(True)
        host_btn.config(state="disabled")
        say("접속하는 중…")

        def work():
            try:
                link, _ = cs.join_to(ip, code, "right")
                q.put(("ok", link, False))
            except cs.JoinError as e:
                q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "ok":
                    st["result"] = (m[1], m[2])
                    return root.destroy()
                if m[0] == "info":
                    say(m[1], "#d06000")
                else:
                    lock(False)
                    host_btn.config(state="normal")
                    say(m[1], "#d00")
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
    ap = argparse.ArgumentParser(description="페널티 대결")
    sub = ap.add_subparsers(dest="mode")
    sub.add_parser("host", help="방 만들기")
    j = sub.add_parser("join", help="참가하기")
    j.add_argument("ip")
    j.add_argument("code")
    args = ap.parse_args()

    import tkinter as tk
    if args.mode is None:
        res = lobby(tk)
        if not res:
            return
        link, is_host = res
    elif args.mode == "host":
        try:
            room = cs.HostRoom("left")
        except OSError:
            sys.exit("방을 열 수 없어요. 이 컴퓨터에서 이미 켜져 있는지 확인해 주세요.")
        print("옆사람에게 이 명령을 알려 주세요:")
        print("  python penalty_duel.py join %s %s" % (room.ip, room.code))
        print("(처음 실행하면 Windows 방화벽 창이 뜨는데 '허용'을 눌러 주세요)")
        link, is_host = room.wait(), True
    else:
        try:
            link, _ = cs.join_to(args.ip, args.code, "right")
        except cs.JoinError as e:
            sys.exit(str(e))
        is_host = False
    root = tk.Tk()
    App(tk, root, link, is_host).frame()
    root.mainloop()


if __name__ == "__main__":
    main()
