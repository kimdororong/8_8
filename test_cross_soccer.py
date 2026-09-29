import random
import unittest

from cross_soccer import BALL_R, Field

W, H = 1920, 1080
DT = 1 / 60


def run(field, seconds, cur=(-999, -999)):
    for _ in range(int(seconds / DT)):
        field.update(DT, cur, cur)


class FieldTest(unittest.TestCase):
    def setUp(self):
        self.a = Field(W, H, "right", random.Random(1))   # 왼쪽 사람
        self.b = Field(W, H, "left", random.Random(2))    # 오른쪽 사람

    def test_ball_crosses_to_other_screen(self):
        self.a.ball = {"x": W - 50, "y": 500, "vx": 400, "vy": 0}
        run(self.a, 1.0)
        self.assertIsNone(self.a.ball)
        ev = [e for e in self.a.out if e[0] == "ball"][0]
        self.b.receive_ball(*ev[1:])
        self.assertEqual(self.b.ball["x"], 0)
        self.assertGreater(self.b.ball["vx"], 0)
        self.assertAlmostEqual(self.b.ball["y"], 500, delta=5)

    def test_wall_bounce(self):
        self.a.ball = {"x": 800, "y": H - 30, "vx": 0, "vy": 500}
        run(self.a, 0.5)
        self.assertLess(self.a.ball["vy"], 0)
        self.assertLessEqual(self.a.ball["y"], H - BALL_R)

    def test_goal_scored_and_synced(self):
        self.a.ball = {"x": 60, "y": 100, "vx": -500, "vy": 0}   # 골대 구간(0~216)
        run(self.a, 1.0)
        self.assertEqual(self.a.score_op, 1)
        self.assertIn(("goal",), self.a.out)
        self.b.goal_for_me()
        self.assertEqual(self.b.score_me, 1)
        self.assertIsNone(self.a.ball)
        run(self.a, 2.5)                                          # 실점한 쪽이 다시 서브
        self.assertIsNotNone(self.a.ball)

    def test_outside_goal_zone_bounces(self):
        self.a.ball = {"x": 60, "y": 600, "vx": -500, "vy": 0}
        run(self.a, 0.5)
        self.assertEqual(self.a.score_op, 0)
        self.assertGreater(self.a.ball["vx"], 0)

    def test_right_player_goal_on_right_edge(self):
        self.b.ball = {"x": W - 60, "y": 100, "vx": 500, "vy": 0}
        run(self.b, 1.0)
        self.assertEqual(self.b.score_op, 1)

    def test_cursor_bounces_ball_even_when_still(self):
        self.a.ball = {"x": 900, "y": 500, "vx": 300, "vy": 0}
        cur = (960, 500)
        run(self.a, 0.4, cur)
        self.assertLess(self.a.ball["vx"], 0)
        self.assertGreaterEqual(abs(self.a.ball["vx"]), 600)

    def test_fast_cursor_does_not_tunnel(self):
        self.a.ball = {"x": 500, "y": 500, "vx": 0, "vy": 0.1}
        self.a.update(DT, (600, 500), (400, 500))       # 한 프레임에 200px 이동
        self.assertGreater(self.a.ball["vx"], 700)

    def test_speed_never_stops(self):
        self.a.ball = {"x": 900, "y": 500, "vx": 1, "vy": 1}
        for _ in range(int(20 / DT)):
            self.a.update(DT, (-999, -999), (-999, -999))
            b = self.a.ball
            if b is None:                       # 상대 화면으로 넘어갔거나 골
                break
            self.assertGreater((b["vx"] ** 2 + b["vy"] ** 2) ** 0.5, 199)


if __name__ == "__main__":
    unittest.main()
