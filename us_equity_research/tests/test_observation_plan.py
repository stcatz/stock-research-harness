import unittest

from us_equity_research.core.observation_plan import build_plan, render_plan


def metric(symbol, r1=0.01, r5=-0.01, r20=-0.02):
    return {
        "symbol": symbol,
        "returns": {"1": r1, "5": r5, "20": r20},
        "relative_20d": 0.05,
        "volume_ratio": 0.8,
    }


class ObservationPlanTests(unittest.TestCase):
    def test_rebound_is_not_a_trend_confirmation(self):
        metrics = {s: metric(s) for s in ["US.SPY", "US.RSP", "US.IWM", "US.QQQ", "US.MSFT"]}
        plan = build_plan(metrics, [], 9, 11, [], "2026-09-11")
        self.assertEqual(plan["regime"], "短期反弹，中期修复待确认")
        self.assertEqual(plan["cards"][-1]["state"], "exclude")
        self.assertIn("UNKNOWN", "\n".join(render_plan(plan)))
        self.assertIn("仅从本计划之后计数", plan["expiry"])

    def test_missing_window_does_not_become_zero_or_positive(self):
        metrics = {"US.SPY": metric("US.SPY", r20=None)}
        plan = build_plan(metrics, [], None, 11, [], "2026-09-11")
        self.assertEqual(plan["regime"], "UNKNOWN")
        self.assertIn("UNKNOWN", plan["cards"][0]["baseline"])
        self.assertEqual(len(plan["cards"]), 1)

    def test_sector_cards_use_actual_inputs_and_do_not_invent_stocks(self):
        xle, xlk = metric("US.XLE", r5=0.01, r20=0.06), metric("US.XLK")
        plan = build_plan({}, [xle, xlk], 9, 11, [], "2026-09-11")
        self.assertEqual([c["object"] for c in plan["cards"][1:]], ["US.XLE", "US.XLK"])
        self.assertIn("强势延续", plan["cards"][1]["question"])
        self.assertIn("相对抗跌", plan["cards"][2]["question"])
        for card in plan["cards"]:
            self.assertTrue(
                card["confirm"] and card["invalidate"] and card["follow_up"] and card["refs"]
            )

    def test_environment_changes_with_prices(self):
        for r1, r5, r20, expected in [
            (0.01, 0.02, 0.03, "多窗口同向上涨"),
            (-0.01, -0.02, 0.01, "短期走弱"),
        ]:
            plan = build_plan(
                {"US.SPY": metric("US.SPY", r1, r5, r20)}, [], None, 11, [], "2026-09-11"
            )
            self.assertEqual(plan["regime"], expected)
