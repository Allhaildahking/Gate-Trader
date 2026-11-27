import unittest
from unittest.mock import patch
from portfolio_backtest import SimConfig, simulate_portfolio, _stats

class PortfolioTests(unittest.TestCase):
    def test_empty_data_is_flat(self):
        out=simulate_portfolio({},[],{},SimConfig(starting_balance=250))
        self.assertEqual(out["summary"]["trades"],0)
        self.assertEqual(out["ending_balance"],250)
    def test_stats(self):
        rows=[{"pnl":10,"r":1},{"pnl":-5,"r":-.5}]
        out=_stats(rows,100,105,.1)
        self.assertEqual(out["win_rate_pct"],50)
        self.assertEqual(out["profit_factor"],2)
        self.assertEqual(out["expectancy"],2.5)
        self.assertEqual(out["max_drawdown_pct"],10)
    def test_no_signal(self):
        m=[{"t":i*900,"o":100,"h":101,"l":99,"c":100,"v":10} for i in range(80)]
        h=[{"t":i*3600,"o":100,"h":101,"l":99,"c":100,"v":10} for i in range(30)]
        with patch("portfolio_backtest.strategy.breakout_signal",return_value=None):
            out=simulate_portfolio({"TEST":{"m15":m,"h1":h}},h,
                {"TEST":{"quanto_multiplier":"1","order_size_min":1}})
        self.assertEqual(out["summary"]["trades"],0)

if __name__=="__main__": unittest.main()
