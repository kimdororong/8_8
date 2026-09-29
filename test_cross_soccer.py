import json
import random
import threading
import time
import unittest

import cross_soccer
from cross_soccer import (BALL_TTL, GOAL_LEN, MAX_BALLS, MAX_R, MIN_R, Field, HostRoom, JoinError,
                          doodle_radius, join_to, make_doodle, sanitize_strokes)

W, H = 1920, 1000
DT = 1 / 60
NOWHERE = (-999, -999)
DOODLE = [[[-20, 0], [0, 20], [20, 0]]]           # 반지름 약 23짜리 작은 낙서
TEST_PORT = 48199


def run(field, seconds, cur=NOWHERE):
    for _ in range(int(seconds / DT)):
        field.update(DT, cur, cur)


def ball(x, y, vx, vy, strokes=DOODLE):
    return {"x": x, "y": y, "vx": vx, "vy": vy, "r": doodle_radius(strokes), "ttl": BALL_TTL,
            "col": "#1e6fe0", "strokes": strokes}


def wire(msg):
    """실제 네트워크처럼 JSON을 한 번 거쳐요."""
    return json.loads(json.dumps(msg, separators=(",", ":")))


class DoodleTest(unittest.TestCase):
    def test_keeps_drawn_size(self):
        raw = [[(100, 100), (160, 100), (160, 140)]]            # 60 x 40 짜리 그림
        s, r, (cx, cy) = make_doodle(raw)
        xs = [p[0] for st in s for p in st]
        ys = [p[1] for st in s for p in st]
        self.assertEqual(max(xs) - min(xs), 60)                 # 늘리거나 줄이지 않아요
        self.assertEqual(max(ys) - min(ys), 40)
        self.assertEqual((cx, cy), (130, 120))                  # 그린 자리의 가운데
        self.assertAlmostEqual(r, (30 ** 2 + 20 ** 2) ** 0.5 + 3, delta=1)

    def test_huge_doodle_is_shrunk_to_fit_goal(self):
        s, r, _ = make_doodle([[(0, 0), (300, 300)]])
        self.assertLessEqual(r, MAX_R + 3)
        self.assertLess(r, GOAL_LEN)

    def test_tiny_and_empty(self):
        self.assertEqual(make_doodle([])[0], [])
        s, r, _ = make_doodle([[(5, 5)]])
        self.assertEqual(s, [[[0, 0]]])
        self.assertEqual(r, MIN_R)

    def test_limits_points(self):
        raw = [[(i * 0.1 % 300, (i % 7) * 5.0) for i in range(3000)]]
        self.assertLessEqual(sum(len(x) for x in make_doodle(raw)[0]), 410)

    def test_sanitize_drops_garbage(self):
        bad = [["x"], [[1, 2, 3]], [[1, "a"]], "zzz", [[9999, -9999], [1, 2]], [[float("nan"), 1]]]
        self.assertEqual(sanitize_strokes(bad), [[[MAX_R, -MAX_R], [1, 2]]])
        self.assertEqual(sanitize_strokes(None), [])
        self.assertLessEqual(sum(len(s) for s in sanitize_strokes([[[0, 0]] * 500] * 10)), 600)


class FieldTest(unittest.TestCase):
    def setUp(self):
        self.a = Field(W, H, "right", random.Random(1))   # 왼쪽 사람
        self.b = Field(W, H, "left", random.Random(2))    # 오른쪽 사람

    def test_no_ball_until_launch(self):
        run(self.a, 2)
        self.assertEqual(self.a.balls, [])

    def test_launch_from_where_it_was_drawn(self):
        self.assertTrue(self.a.launch(DOODLE, "#1e6fe0", (700, 650)))
        b = self.a.balls[0]
        self.assertEqual((b["x"], b["y"]), (700, 650))
        self.assertGreater(b["vx"], 0)
        self.assertEqual(b["strokes"], DOODLE)
        self.assertTrue(self.b.launch(DOODLE, "#e02020", (700, 650)))
        self.assertLess(self.b.balls[0]["vx"], 0)

    def test_launch_cooldown_and_limit(self):
        self.assertTrue(self.a.launch(DOODLE, "#111111", (960, 800)))
        self.assertFalse(self.a.launch(DOODLE, "#111111", (960, 800)))    # 쿨다운
        self.a.balls = [ball(500, 500, 100, 0) for _ in range(MAX_BALLS)]
        self.a.cool = 0
        self.assertFalse(self.a.launch(DOODLE, "#111111", (960, 800)))
        self.assertFalse(self.a.launch([], "#111111", (960, 800)))

    def test_doodle_survives_trip_unchanged(self):
        self.a.balls = [ball(W - 50, 500, 400, 0)]
        run(self.a, 1.0)
        self.assertEqual(self.a.balls, [])
        ev = [e for e in self.a.out if e[0] == "ball"][0]
        self.b.receive_ball(wire(ev[1]))
        got = self.b.balls[0]
        self.assertEqual(got["x"], 0)
        self.assertGreater(got["vx"], 0)
        self.assertEqual(got["strokes"], DOODLE)
        self.assertEqual(got["col"], "#1e6fe0")
        self.assertAlmostEqual(got["r"], doodle_radius(DOODLE))
        self.assertAlmostEqual(got["y"], 500, delta=5)

    def test_big_doodle_bounces_on_its_own_edge(self):
        big = [[[-100, 0], [100, 0]]]
        self.a.balls = [ball(800, 150, 0, -500, big)]
        run(self.a, 0.3)
        self.assertGreater(self.a.balls[0]["vy"], 0)
        self.assertGreaterEqual(self.a.balls[0]["y"], doodle_radius(big) - 1)

    def test_only_neighbor_side_is_open(self):
        self.a.balls = [ball(800, 40, 0, -500)]
        run(self.a, 0.3)
        self.assertGreater(self.a.balls[0]["vy"], 0)
        self.a.balls = [ball(900, H - 40, 0, 500)]
        run(self.a, 0.3)
        self.assertLess(self.a.balls[0]["vy"], 0)
        self.a.balls = [ball(60, 300, -500, 0)]           # 바깥쪽 벽, 골대 위쪽
        run(self.a, 0.3)
        self.assertGreater(self.a.balls[0]["vx"], 0)
        self.assertEqual(self.a.out, [])

    def test_goal_is_200px_tall_at_bottom_of_far_wall(self):
        self.assertEqual(self.a.goal_zone, (H - 200, float(H)))
        self.assertEqual((self.a.goal_x, self.b.goal_x), (0.0, float(W)))

    def test_goal_starts_fanfare_on_conceding_screen(self):
        self.a.balls = [ball(150, H - 80, -600, 0)]
        run(self.a, 1.0)
        self.assertEqual(self.a.score_op, 1)
        self.assertIn(("goal",), self.a.out)
        self.assertTrue(self.a.fanfare_new)
        self.assertGreater(self.a.fanfare, 0)
        self.assertGreater(len(self.a.confetti), 100)
        self.b.goal_for_me()
        self.assertEqual(self.b.score_me, 1)
        self.assertEqual(self.b.fanfare, 0)                  # 넣은 쪽은 팡파레 없음
        run(self.a, 6)
        self.assertEqual(self.a.fanfare, 0)
        self.assertEqual(self.a.confetti, [])

    def test_big_doodle_can_still_score(self):
        big = [[[-MAX_R, 0], [MAX_R, 0]]]
        self.a.balls = [ball(300, H - 160, -900, 0, big)]
        run(self.a, 1.5)
        self.assertEqual(self.a.score_op, 1)

    def test_far_wall_above_goal_bounces(self):
        self.a.balls = [ball(150, H - 300, -600, 0)]
        run(self.a, 1.0)
        self.assertEqual(self.a.score_op, 0)

    def test_right_player_goal_on_right_wall(self):
        self.b.balls = [ball(W - 150, H - 80, 600, 0)]
        run(self.b, 1.0)
        self.assertEqual(self.b.score_op, 1)

    def test_cursor_bounces_ball_even_when_still(self):
        self.a.balls = [ball(900, 500, 300, 0)]
        run(self.a, 0.4, (960, 500))
        self.assertLess(self.a.balls[0]["vx"], 0)
        self.assertGreaterEqual(abs(self.a.balls[0]["vx"]), 600)

    def test_fast_cursor_does_not_tunnel(self):
        self.a.balls = [ball(500, 500, 0, 0.1)]
        self.a.update(DT, (600, 500), (400, 500))
        self.assertGreater(self.a.balls[0]["vx"], 700)

    def test_cursor_in_pad_does_not_kick(self):
        self.a.pad_rect = (800, 400, 1100, 600)
        self.a.balls = [ball(900, 500, 300, 0)]
        self.a.update(DT, (900, 500), (900, 500))
        self.assertGreater(self.a.balls[0]["vx"], 0)

    def test_ball_pops_after_ttl(self):
        self.a.balls = [ball(900, 500, 100, 0)]
        self.a.balls[0]["ttl"] = 0.5
        run(self.a, 1.0)
        self.assertEqual(self.a.balls, [])

    def test_malformed_peer_message(self):
        with self.assertRaises((KeyError, TypeError, ValueError)):
            self.a.receive_ball({"y": "x"})
        with self.assertRaises(ValueError):
            self.a.receive_ball({"y": 0.5, "vx": float("inf"), "vy": 0})
        self.a.receive_ball({"y": 0.5, "vx": 0.1, "vy": 0, "col": "red);evil", "s": "zzz"})
        self.assertEqual(self.a.balls[0]["col"], "#333333")
        self.assertEqual(self.a.balls[0]["r"], MIN_R)


class FanfareSoundTest(unittest.TestCase):
    def test_two_second_wav(self):
        import os
        import tempfile
        import wave
        path = os.path.join(tempfile.mkdtemp(), "f.wav")
        t0 = time.time()
        cross_soccer.make_fanfare_wav(path)
        with wave.open(path) as w:
            secs = w.getnframes() / w.getframerate()
            data = w.readframes(w.getnframes())
        self.assertAlmostEqual(secs, 2.0, delta=0.01)
        self.assertGreater(max(abs(int.from_bytes(data[i:i + 2], "little", signed=True))
                               for i in range(0, len(data), 2)), 20000)   # 충분히 큰 소리
        self.assertLess(time.time() - t0, 5)                               # 시작이 느려지지 않게


class HandshakeTest(unittest.TestCase):
    def open_room(self, me):
        room = HostRoom(me, port=TEST_PORT)
        box = {"info": []}

        def work():
            try:
                box["link"] = room.wait(on_info=box["info"].append)
            except OSError:
                box["closed"] = True
        th = threading.Thread(target=work, daemon=True)
        th.start()
        return room, box, th

    def test_sides_must_be_opposite(self):
        room, box, th = self.open_room("left")
        with self.assertRaises(JoinError) as e:
            join_to("127.0.0.1", room.code, "left", port=TEST_PORT)
        self.assertIn("오른쪽", str(e.exception))
        with self.assertRaises(JoinError):
            join_to("127.0.0.1", "9999" if room.code != "9999" else "0000", "right", port=TEST_PORT)
        link, me = join_to("127.0.0.1", room.code, "right", port=TEST_PORT)
        th.join(2)
        self.assertEqual(me, "right")
        self.assertEqual(len(box["info"]), 2)                 # 거절 안내 두 번
        link.close()
        box["link"].close()

    def test_joiner_without_side_gets_opposite(self):
        room, box, th = self.open_room("right")
        link, me = join_to("127.0.0.1", room.code, None, port=TEST_PORT)
        th.join(2)
        self.assertEqual(me, "left")
        link.close()
        box["link"].close()

    def test_cancel_room_stops_waiting(self):
        room, box, th = self.open_room("left")
        time.sleep(0.1)
        room.close()
        th.join(2)
        self.assertFalse(th.is_alive())
        self.assertTrue(box.get("closed"))

    def test_no_room(self):
        with self.assertRaises(JoinError):
            join_to("127.0.0.1", "1234", "left", port=TEST_PORT + 1)


if __name__ == "__main__":
    unittest.main()
