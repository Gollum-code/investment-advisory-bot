"""配置校验（类型强转/范围钳制）的单元测试。"""

import copy
import unittest

from iabot.config import DEFAULTS, validate_config


class TestValidateConfig(unittest.TestCase):
    def test_defaults_pass_through(self):
        out = validate_config(copy.deepcopy(DEFAULTS))
        self.assertEqual(out["account"]["equity_usdt"], 450.0)
        self.assertEqual(out["account"]["preferred_leverage"], 10)

    def test_string_numbers_are_coerced(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["account"] = {"equity_usdt": "500", "risk_pct_per_trade": "3",
                          "max_leverage": "20", "preferred_leverage": "20"}
        out = validate_config(cfg)
        self.assertEqual(out["account"]["equity_usdt"], 500.0)
        self.assertEqual(out["account"]["risk_pct_per_trade"], 3.0)
        self.assertEqual(out["account"]["max_leverage"], 20)

    def test_bad_value_falls_back_to_default(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["account"]["equity_usdt"] = "abc"
        out = validate_config(cfg)
        self.assertEqual(out["account"]["equity_usdt"], 450.0)

    def test_preferred_leverage_capped_by_max(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["account"] = {"max_leverage": 5, "preferred_leverage": 10}
        out = validate_config(cfg)
        self.assertEqual(out["account"]["preferred_leverage"], 5)

    def test_interval_floor(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["schedule"]["interval_sec"] = 1  # 低于强制下限 30
        out = validate_config(cfg)
        self.assertEqual(out["schedule"]["interval_sec"], 30)

    def test_symbols_non_list_reset(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["symbols"] = "BTC-USDT"
        out = validate_config(cfg)
        self.assertIsInstance(out["symbols"], list)


if __name__ == "__main__":
    unittest.main()