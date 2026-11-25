"""核心逻辑测试（无需 pytest，直接调用函数）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.market_data import add_indicators, _mock_kline, FALLBACK_POOL, _attach_momentum
from analysis.scorer import score_stock
from data.arti_client import get_client
from analysis.ai_analysis import analyze_all


def test_indicators():
    df = add_indicators(_mock_kline("600519", 100))
    for c in ("ma5", "ma20", "dif", "dea", "macd", "boll_up", "boll_mid",
              "boll_low", "rsi", "k", "d", "j"):
        assert c in df.columns, f"missing {c}"
    assert df["rsi"].between(0, 100).all(), "RSI 越界"
    print("  [OK] 技术指标计算")


def test_score():
    df = add_indicators(_mock_kline("601398", 100))
    s = score_stock(df)
    assert 0 <= s["total"] <= 100, "评分区间"
    assert isinstance(s["signals"], list)
    print("  [OK] 技术评分")


def test_momentum():
    rows = [
        {"name": "A", "change_pct": 2, "net_inflow": 10, "up_count": 80, "down_count": 20},
        {"name": "B", "change_pct": -1, "net_inflow": -5, "up_count": 30, "down_count": 70},
    ]
    _attach_momentum(rows)
    assert rows[0]["momentum_score"] > rows[1]["momentum_score"], "轮动评分排序错误"
    print("  [OK] 板块轮动评分")


def test_ai():
    client = get_client(mock=True)
    stock = {"code": "600519", "name": "贵州茅台", "close": 1485, "ret20": 3,
             "score": 70, "signals": ["均线多头排列"]}
    r = analyze_all(stock, "白酒", client)
    assert len(r) == 3 and all(len(v["content"]) > 20 for v in r.values())
    print("  [OK] AI 三维分析(mock)")


def test_fallback_pool():
    assert len(FALLBACK_POOL) >= 10
    assert all(len(v) >= 4 for v in FALLBACK_POOL.values())
    print("  [OK] 内置降级股票池")


if __name__ == "__main__":
    print("运行核心测试:")
    test_indicators()
    test_score()
    test_momentum()
    test_ai()
    test_fallback_pool()
    print("全部测试通过")
