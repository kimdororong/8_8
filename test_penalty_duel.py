import json
import math
import random
import unittest

import penalty_duel as pd
from penalty_duel import Match, held_dir

DT = 1 / 60
NONE = {"l": 0, "r": 0, "u": 0, "d": 0, "kick": 0, "restart": 0, "swap": 0}


def inp(**kw):
    d = dict(NONE)
    d.update(kw)
    return d


class Driver:
    """두 사람의 입력(누른 키와 스페이스 횟수)을 흉내 내요."""

    def __init__(self, m):
        self.m = m
        self.i = {"host": dict(NONE), "guest": dict(NONE)}

    def hold(self, who, **keys):
        for k in "lrud":
            self.i[who][k] = int(keys.get(k, 0))

    def tap(self, who, key="kick"):
        self.i[who][key] += 1

    def run(self, secs):
        for _ in range(int(secs / DT)):
            self.m.step(DT, {p: dict(v) for p, v in self.i.items()})

    def until(self, phase, limit=30):
        for _ in range(int(limit / DT)):
            if self.m.phase == phase:
                return True
            self.m.step(DT, {p: dict(v) for p, v in self.i.items()})
        return self.m.phase == phase


def new_match():
    m = Match(random.Random(1))
    return m, Driver(m)


def start_play(d):
    assert d.until("play", 5)


class HeldDirTest(unittest.TestCase):
    def test_dirs(self):
        self.assertEqual(held_dir(inp()), (0.0, 0.0))
        self.assertEqual(held_dir(inp(u=1)), (0.0, -1.0))
        self.assertEqual(held_dir(inp(d=1)), (0.0, 1.0))
        self.assertEqual(held_dir(inp(l=1)), (-1.0, 0.0))
        dx, dy = held_dir(inp(r=1, u=1))
        self.assertAlmostEqual(math.hypot(dx, dy), 1.0)
        self.assertEqual(held_dir(inp(l=1, r=1)), (0.0, 0.0))


class RoundTest(unittest.TestCase):
    def test_ready_then_play_and_roles(self):
        m, d = new_match()
        self.assertEqual(m.phase, "ready")
        self.assertEqual((m.kicker_key, m.keeper_key), ("host", "guest"))
        start_play(d)
        self.assertEqual(m.phase, "play")

    def test_players_move_with_arrow_keys_inside_their_zones(self):
        m, d = new_match()
        start_play(d)
        x0 = m.keeper["x"]
        d.hold("guest", r=1)                                   # 골키퍼(guest) 오른쪽
        d.run(0.5)
        self.assertGreater(m.keeper["x"], x0 + 100)
        d.run(5)
        self.assertLessEqual(m.keeper["x"], pd.KEEPER_ZONE[1])
        d.hold("guest", u=1)
        d.run(3)
        self.assertGreaterEqual(m.keeper["y"], pd.KEEPER_ZONE[2])
        d.hold("host", u=1)                                    # 키커는 골키퍼 구역에 못 들어가요
        d.run(6)
        self.assertGreaterEqual(m.kicker["y"], pd.KICKER_MIN_Y)

    def test_kick_up_flies_up(self):
        m, d = new_match()
        start_play(d)
        m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold("host", u=1)
        d.tap("host")
        d.run(DT * 2)
        self.assertLess(m.ball["vy"], -500)
        self.assertAlmostEqual(m.ball["vx"], 0, delta=60)
        self.assertTrue(m.shot)

    def test_kick_directions(self):
        cases = {"d": (0, 1), "l": (-1, 0), "r": (1, 0)}
        for key, (ex, ey) in cases.items():
            m, d = new_match()
            start_play(d)
            m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
            d.hold("host", **{key: 1})
            d.tap("host")
            d.run(DT * 2)
            b = m.ball
            if ex:
                self.assertGreater(b["vx"] * ex, 400, key)
            if ey:
                self.assertGreater(b["vy"] * ey, 300, key)
        m, d = new_match()                                     # 대각선
        start_play(d)
        m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold("host", u=1, l=1)
        d.tap("host")
        d.run(DT * 2)
        self.assertLess(m.ball["vx"], -300)
        self.assertLess(m.ball["vy"], -300)

    def test_kick_without_arrow_uses_facing(self):
        m, d = new_match()
        start_play(d)
        m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold("host", l=1)
        d.run(DT * 1)
        m.kicker["x"], m.kicker["y"] = m.ball["x"] + 34, m.ball["y"]
        d.hold("host")                                         # 손 떼도 바라보는 방향(왼쪽)
        d.tap("host")
        d.run(DT * 2)
        self.assertLess(m.ball["vx"], -400)

    def test_kick_too_far_does_nothing(self):
        m, d = new_match()
        start_play(d)
        d.hold("host", u=1)
        d.tap("host")
        d.run(DT * 2)
        self.assertFalse(m.shot)
        self.assertEqual(m.ball["vx"], 0)

    def test_dribbling_moves_the_ball(self):
        m, d = new_match()
        start_play(d)
        y0 = m.ball["y"]
        d.hold("host", u=1)
        d.run(1.0)
        self.assertLess(m.ball["y"], y0 - 40)
        self.assertFalse(m.shot)

    def test_straight_shot_hits_keeper_in_center_is_saved(self):
        m, d = new_match()
        start_play(d)
        m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold("host", u=1)
        d.tap("host")
        d.run(2.0)
        self.assertEqual(m.phase, "result")
        self.assertEqual(m.result["kind"], "save")
        self.assertEqual(m.score, {"host": 0, "guest": 0})

    def test_goal_when_keeper_is_far(self):
        m, d = new_match()
        start_play(d)
        d.hold("guest", r=1)
        d.run(2.0)                                             # 골키퍼가 오른쪽 끝으로
        m.kicker["x"], m.kicker["y"] = 260.0, 640.0
        m.ball.update(x=260.0, y=606.0)
        d.hold("host", u=1)
        d.tap("host")
        d.run(2.0)
        self.assertEqual(m.result["kind"], "goal")
        self.assertEqual(m.score, {"host": 1, "guest": 0})

    def test_wide_shot_is_a_miss(self):
        m, d = new_match()
        start_play(d)
        m.kicker["x"], m.kicker["y"] = 80.0, 640.0
        m.ball.update(x=80.0, y=606.0)
        d.hold("host", u=1)
        d.tap("host")
        self.assertTrue(d.until("result", 10))
        self.assertEqual(m.result["kind"], "miss")
        self.assertEqual(m.score, {"host": 0, "guest": 0})

    def test_timeout_when_nobody_kicks(self):
        m, d = new_match()
        start_play(d)
        d.run(pd.PLAY_TIMEOUT + 1)
        self.assertEqual(m.result["kind"], "miss")

    def test_keeper_dive_is_fast_and_has_cooldown(self):
        m, d = new_match()
        start_play(d)
        x0 = m.keeper["x"]
        d.hold("guest", l=1)
        d.tap("guest")
        d.run(pd.DIVE_TIME + 0.05)
        moved = x0 - m.keeper["x"]
        self.assertGreater(moved, pd.KEEPER_SPEED * (pd.DIVE_TIME + 0.05) + 30)
        self.assertGreater(m.keeper["cd"], 0.9)
        d.hold("guest")
        x1 = m.keeper["x"]
        d.tap("guest")
        d.run(0.1)
        self.assertEqual(m.keeper["x"], x1)                    # 충전 중엔 다이빙 안 돼요

    def test_hold_space_does_not_matter_only_new_presses(self):
        m, d = new_match()
        start_play(d)
        m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold("host", u=1)
        d.i["host"]["kick"] = 5                                # 몇 번 눌렀든 새로 늘어난 것만 인정
        d.run(DT * 2)
        self.assertTrue(m.shot)


class MatchFlowTest(unittest.TestCase):
    def play_round(self, d, kicker_score):
        """kicker_score=True면 골, 아니면 골키퍼가 막는 라운드를 진행해요."""
        m = d.m
        start_play(d)
        kp, gp = m.kicker_key, m.keeper_key
        if kicker_score:
            d.hold(gp, r=1)
            d.run(2.0)
            d.hold(gp)
            m.kicker.update(x=260.0, y=640.0)
            m.ball.update(x=260.0, y=606.0)
        else:
            m.kicker["x"], m.kicker["y"] = m.ball["x"], m.ball["y"] + 34
        d.hold(kp, u=1)
        d.tap(kp)
        d.run(2.0)
        d.hold(kp)
        d.until("ready", 5) or d.until("over", 1)

    def test_roles_alternate_and_full_game(self):
        m, d = new_match()
        order = []
        for i in range(10):
            order.append(m.kicker_key)
            self.play_round(d, kicker_score=(i % 4 == 0))      # 골: 0,4,8 라운드
        self.assertEqual(order, ["host", "guest"] * 5)
        self.assertEqual(m.phase, "over")
        self.assertEqual(m.score, {"host": 3, "guest": 0})     # 골이 난 라운드 0,4,8은 모두 짝수 = host가 찬 판
        self.assertEqual(m.winner, "host")

    def test_tie_goes_to_extra_rounds(self):
        m, d = new_match()
        for i in range(10):
            self.play_round(d, kicker_score=False)
        self.assertNotEqual(m.phase, "over")                   # 0:0 은 연장
        self.assertEqual(m.round, 10)
        self.play_round(d, kicker_score=True)                  # host 골
        self.assertNotEqual(m.phase, "over")                   # 한 명씩 다 차야 승부 판정
        self.play_round(d, kicker_score=False)
        self.assertEqual(m.phase, "over")
        self.assertEqual(m.winner, "host")

    def test_restart_after_game_over(self):
        m, d = new_match()
        for i in range(10):
            self.play_round(d, kicker_score=(i % 2 == 0))
        self.assertEqual(m.phase, "over")
        d.tap("guest", "restart")
        d.run(DT * 2)
        self.assertEqual(m.phase, "ready")
        self.assertEqual((m.round, m.score), (0, {"host": 0, "guest": 0}))


class SwapTest(unittest.TestCase):
    def test_swap_in_ready_flips_this_round(self):
        m, d = new_match()
        self.assertEqual(m.kicker_key, "host")
        d.tap("guest", "swap")                       # 누구든 누를 수 있어요
        d.run(DT * 2)
        self.assertEqual((m.kicker_key, m.keeper_key), ("guest", "host"))
        self.assertEqual(m.phase, "ready")

    def test_swap_ignored_during_play(self):
        m, d = new_match()
        start_play(d)
        d.tap("host", "swap")
        d.run(DT * 2)
        self.assertEqual(m.kicker_key, "host")
        self.assertFalse(m.swap_next)

    def test_swap_in_result_reserves_next_round(self):
        m, d = new_match()
        start_play(d)
        d.run(pd.PLAY_TIMEOUT + 0.5)                 # 시간 초과로 결과 화면
        self.assertEqual(m.phase, "result")
        d.tap("host", "swap")
        d.run(DT * 2)
        self.assertTrue(m.swap_next)
        d.tap("guest", "swap")                       # 한 번 더 누르면 예약 취소
        d.run(DT * 2)
        self.assertFalse(m.swap_next)
        d.tap("host", "swap")
        d.run(DT * 2)
        d.until("ready", 5)
        self.assertEqual(m.kicker_key, "host")      # 원래는 guest 차례지만 교대 예약 때문에 host가 또 차요
        self.assertFalse(m.swap_next)                # 예약은 한 번 쓰면 사라져요

    def test_game_ends_after_each_kicked_equally(self):
        m, d = new_match()
        d.tap("host", "swap")                        # 처음부터 guest가 먼저 킥
        d.run(DT * 2)
        self.assertEqual(m.kicker_key, "guest")
        rounds = 0
        while m.phase != "over" and rounds < 40:
            assert d.until("play", 5)
            d.run(pd.PLAY_TIMEOUT + 0.3)
            if rounds % 3 == 0:
                m.score[m.kicker_key] += 1           # 골이 난 것처럼 점수를 준다
            d.until("ready", 6) or d.until("over", 1)
            rounds += 1
        self.assertEqual(m.phase, "over")
        self.assertEqual(m.kicks_taken["host"], m.kicks_taken["guest"])
        self.assertGreaterEqual(m.kicks_taken["host"], 5)

    def test_state_has_swap_info(self):
        m, d = new_match()
        s = m.to_state()
        self.assertEqual(s["kt"], [0, 0])
        self.assertFalse(s["sw"])


class ViewTest(unittest.TestCase):
    def test_kicker_view_far_is_smaller_and_higher(self):
        v = pd.View(320, 865, False)
        near, far = v.proj(320, 690), v.proj(320, 90)
        self.assertGreater(near[2], far[2])
        self.assertGreater(near[1], far[1])            # 가까울수록 화면 아래
        self.assertAlmostEqual(near[0], pd.VIEW_W / 2)

    def test_lateral_direction_kicker_vs_keeper(self):
        k = pd.View(320, 865, False)
        g = pd.View(320, 0, True)
        self.assertGreater(k.proj(400, 500)[0], pd.VIEW_W / 2)     # 키커는 세계 +x가 화면 오른쪽
        self.assertLess(g.proj(400, 500)[0], pd.VIEW_W / 2)        # 골키퍼 화면은 뒤집혀서 왼쪽

    def test_behind_camera_is_none(self):
        self.assertIsNone(pd.View(320, 700, False).proj(320, 800))
        self.assertEqual(pd.View(320, 700, False).ground_poly([(0, 800), (10, 800), (10, 900)]), [])

    def test_ground_poly_clips_at_near_plane(self):
        v = pd.View(320, 700, False)
        flat = v.ground_poly([(200, 100), (440, 100), (440, 760), (200, 760)])   # 카메라 뒤로 걸친 사각형
        self.assertGreaterEqual(len(flat), 8)
        self.assertTrue(all(y < 2700 for y in flat[1::2]))

    def test_flip_keys_and_fractions(self):
        self.assertEqual(pd.flip_keys(1, 0, 1, 0), (0, 1, 0, 1))
        a, b = pd.best_fraction(0.5)
        self.assertAlmostEqual(a / b, 0.5)
        a, b = pd.best_fraction(2.4)
        self.assertAlmostEqual(a / b, 2.4, delta=0.15)


class AssetSignatureTest(unittest.TestCase):
    def test_signature_changes_when_file_added_or_changed(self):
        import os
        import tempfile
        folder = tempfile.mkdtemp()
        s0 = pd.assets_signature(folder)
        self.assertTrue(all(m is None for _, m, _ in s0))
        path = os.path.join(folder, "keeper_back.png")
        open(path, "wb").write(b"x")
        s1 = pd.assets_signature(folder)
        self.assertNotEqual(s0, s1)
        open(path, "wb").write(b"xyz")
        self.assertNotEqual(s1, pd.assets_signature(folder))
        open(os.path.join(folder, "other.png"), "wb").write(b"x")            # 이름이 다른 파일은 무시
        self.assertEqual(pd.assets_signature(folder), pd.assets_signature(folder))


class StateTest(unittest.TestCase):
    def test_state_is_json_and_small(self):
        m, d = new_match()
        start_play(d)
        s = json.dumps(m.to_state())
        self.assertLess(len(s), 400)
        self.assertEqual(json.loads(s)["ph"], "play")


class SimulationTest(unittest.TestCase):
    """봇끼리 여러 판 돌려서 오류 없이 끝나는지, 골도 나고 막기도 하는지 봐요."""

    def bot_game(self, seed):
        rng = random.Random(seed)
        m = Match(rng)
        d = Driver(m)
        kinds = []
        for _ in range(60 * 60 * 20):
            if m.phase == "over":
                break
            if m.phase == "ready":
                self.setup = None
            kp, gp = m.kicker_key, m.keeper_key
            if m.phase == "play":
                if getattr(self, "setup", None) is None:          # 라운드마다: 자리 잡고 곧게 차기
                    tx = rng.uniform(215, 425)
                    m.ball.update(x=tx, y=pd.KICKER_MIN_Y + 20)
                    m.kicker.update(x=tx, y=pd.KICKER_MIN_Y + 54)
                    self.setup = (m.play_t + rng.uniform(0.0, 0.8), [])
                fire_at, hist = self.setup
                if m.shot:                                         # 골키퍼는 슛을 본 뒤 0.25초 늦게 따라가요
                    hist.append(m.ball["x"])
                    lag = hist[max(0, len(hist) - 15)] if len(hist) > 15 else None
                    g = m.keeper
                    if lag is None:
                        d.hold(gp)
                    else:
                        d.hold(gp, l=int(g["x"] > lag + 8), r=int(g["x"] < lag - 8))
                elif m.play_t >= fire_at:
                    d.hold(kp, u=1)
                    d.tap(kp)
            elif m.phase == "result":
                d.hold("host")
                d.hold("guest")
                if m.timer < DT * 1.5:
                    kinds.append(m.result["kind"])
            m.step(DT, {p: dict(v) for p, v in d.i.items()})
            json.dumps(m.to_state())
        return m, kinds

    def test_bots_finish_games(self):
        all_kinds = []
        for seed in range(5):
            m, kinds = self.bot_game(seed)
            self.assertEqual(m.phase, "over", seed)
            self.assertIn(m.winner, ("host", "guest"))
            self.assertNotEqual(m.score["host"], m.score["guest"])
            all_kinds += kinds
        self.assertIn("goal", all_kinds)
        self.assertIn("save", all_kinds)
        self.assertGreater(all_kinds.count("goal"), len(all_kinds) * 0.15)   # 골이 너무 안 나도
        self.assertLess(all_kinds.count("goal"), len(all_kinds) * 0.95)      # 너무 잘 나도 안 돼요


if __name__ == "__main__":
    unittest.main()
