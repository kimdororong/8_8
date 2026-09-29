import json
import random
import unittest

from cross_soccer import (BALL_R, BALL_TTL, GOAL_LEN, MAX_BALLS, STROKE_SCALE, Field,
                          normalize_strokes, sanitize_strokes)

W, H = 1920, 1000
DT = 1 / 60
NOWHERE = (-999, -999)
DOODLE = [[[-100, 0], [0, 100], [100, 0]]]


def run(field, seconds, cur=NOWHERE):
    for _ in range(int(seconds / DT)):
        field.update(DT, cur, cur)


def ball(x, y, vx, vy):
    return {"x": x, "y": y, "vx": vx, "vy": vy, "rot": 0.0, "ttl": BALL_TTL,
            "col": "#1e6fe0", "strokes": DOODLE}


def wire(msg):
    """실제 네트워크처럼 JSON을 한 번 거쳐요."""
    return json.loads(json.dumps(msg, separators=(",", ":")))


class StrokeTest(unittest.TestCase):
    def test_normalize_fits_in_circle_and_centers(self):
        raw = [[(10, 10), (30, 10), (30, 50)], [(20, 30)]]
        s = normalize_strokes(raw)
        pts = [p for stroke in s for p in stroke]
        self.assertTrue(all((x * x + y * y) ** 0.5 <= STROKE_SCALE + 1 for x, y in pts))
        xs = [p[0] for p in pts]
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, 0, delta=2)

    def test_normalize_limits_points(self):
        raw = [[(i * 3.0, (i % 7) * 5.0) for i in range(3000)]]
        total = sum(len(s) for s in normalize_strokes(raw))
        self.assertLessEqual(total, 410)

    def test_single_dot_and_empty(self):
        self.assertEqual(normalize_strokes([]), [])
        self.assertEqual(normalize_strokes([[(5, 5)]]), [[[0, 0]]])

    def test_sanitize_drops_garbage(self):
        bad = [["x"], [[1, 2, 3]], [[1, "a"]], "zzz", [[9999, -9999], [1, 2]]]
        out = sanitize_strokes(bad)
        self.assertEqual(out, [[[STROKE_SCALE, -STROKE_SCALE], [1, 2]]])
        self.assertEqual(sanitize_strokes(None), [])
        self.assertLessEqual(sum(len(s) for s in sanitize_strokes([[[0, 0]] * 500] * 10)), 600)


class FieldTest(unittest.TestCase):
    def setUp(self):
        self.a = Field(W, H, "right", random.Random(1))   # 왼쪽 사람
        self.b = Field(W, H, "left", random.Random(2))    # 오른쪽 사람

    def test_no_ball_until_launch(self):
        run(self.a, 2)
        self.assertEqual(self.a.balls, [])

    def test_launch_makes_doodle_ball_flying_toward_neighbor(self):
        self.assertTrue(self.a.launch(DOODLE, "#1e6fe0", (960, 800)))
        self.assertGreater(self.a.balls[0]["vx"], 0)
        self.assertTrue(self.b.launch(DOODLE, "#e02020", (960, 800)))
        self.assertLess(self.b.balls[0]["vx"], 0)

    def test_launch_cooldown_and_limit(self):
        self.assertTrue(self.a.launch(DOODLE, "#111111", (960, 800)))
        self.assertFalse(self.a.launch(DOODLE, "#111111", (960, 800)))    # 쿨다운
        self.a.balls = [ball(500, 500, 100, 0) for _ in range(MAX_BALLS)]
        self.a.cool = 0
        self.assertFalse(self.a.launch(DOODLE, "#111111", (960, 800)))
        self.assertFalse(self.a.launch([], "#111111", (960, 800)))

    def test_doodle_survives_trip_to_other_screen(self):
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
        self.assertAlmostEqual(got["y"], 500, delta=5)

    def test_only_neighbor_side_is_open(self):
        # 위·아래·바깥쪽 벽(골대 밖)은 튕기고, 옆사람 쪽 면만 통과
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
        self.assertEqual(len(self.a.balls), 1)

    def test_goal_is_200px_tall_at_bottom_of_far_wall(self):
        self.assertEqual(self.a.goal_zone, (H - 200, float(H)))
        self.assertEqual((self.a.goal_x, self.b.goal_x), (0.0, float(W)))
        self.assertEqual(GOAL_LEN, 200)

    def test_goal_scored_and_synced(self):
        self.a.balls = [ball(150, H - 80, -600, 0)]         # 왼쪽 벽 아래쪽 골대로
        run(self.a, 1.0)
        self.assertEqual(self.a.score_op, 1)
        self.assertIn(("goal",), self.a.out)
        self.assertEqual(self.a.balls, [])
        self.b.goal_for_me()
        self.assertEqual(self.b.score_me, 1)

    def test_far_wall_above_goal_bounces(self):
        self.a.balls = [ball(150, H - 300, -600, 0)]        # 골대 바로 위: 튕김
        run(self.a, 1.0)
        self.assertEqual(self.a.score_op, 0)
        self.assertEqual(len(self.a.balls), 1)

    def test_ball_in_goal_zone_cannot_leave_through_bottom(self):
        self.a.balls = [ball(30, H - 40, 0, 600)]
        run(self.a, 0.5)
        self.assertEqual(self.a.score_op, 0)
        self.assertEqual(len(self.a.balls), 1)

    def test_right_player_goal_bottom_right(self):
        self.b.balls = [ball(W - 150, H - 80, 600, 0)]
        run(self.b, 1.0)
        self.assertEqual(self.b.score_op, 1)
        self.b.balls = [ball(150, H - 80, -600, 0)]         # 왼쪽은 골대가 아니라 옆사람과 이어진 통로
        run(self.b, 1.0)
        self.assertEqual(self.b.score_op, 1)
        self.assertEqual(self.b.balls, [])
        self.assertEqual([e[0] for e in self.b.out].count("ball"), 1)

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
        self.assertEqual(self.a.hits, [])

    def test_ball_pops_after_ttl(self):
        self.a.balls = [ball(900, 500, 100, 0)]
        self.a.balls[0]["ttl"] = 0.5
        run(self.a, 1.0)
        self.assertEqual(self.a.balls, [])

    def test_speed_never_stops(self):
        self.a.balls = [ball(900, 500, 1, 1)]
        for _ in range(int(20 / DT)):
            self.a.update(DT, NOWHERE, NOWHERE)
            for b in self.a.balls:
                self.assertGreater((b["vx"] ** 2 + b["vy"] ** 2) ** 0.5, 199)
            if not self.a.balls:
                break

    def test_malformed_peer_message(self):
        with self.assertRaises((KeyError, TypeError, ValueError)):
            self.a.receive_ball({"y": "x"})
        self.a.receive_ball({"y": 0.5, "vx": 0.1, "vy": 0, "col": "red);evil", "s": "zzz"})
        self.assertEqual(self.a.balls[0]["col"], "#333333")
        self.assertEqual(self.a.balls[0]["strokes"], [])


if __name__ == "__main__":
    unittest.main()
