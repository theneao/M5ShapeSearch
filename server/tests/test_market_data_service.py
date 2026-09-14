# -*- coding: utf-8 -*-
"""
市场数据配置、来源隔离和周线缓存测试。

创建时间：2026-08-28
作用：验证参数边界、AKShare+Binance Public 缓存来源清单，以及 1w 连续序列持久化加载。
使用方式：cd server && python -m pytest tests/test_market_data_service.py -q

修改时间：2026-08-29
修改作用：验证分钟周期白名单、1h→60m 兼容归一和旧缓存桶迁移。

修改时间：2026-08-29
修改作用：验证数量 0 保留为“全部”语义，以及 A 股/Crypto 独立排序指标白名单。

修改时间：2026-08-29
修改作用：验证缺失周期按需加入后台队列，以及缓存最新 K 线时间看板。

修改时间：2026-08-30
修改作用：验证失败刷新退避、缓存 A 股标的池复用，以及活动桶彻底排除期货。

修改时间：2026-08-30
修改作用：验证匹配轮询在失败冷却中不会重复唤醒刷新线程，并返回建议等待时间。

修改时间：2026-08-30
修改作用：验证用户按需请求可越过一次旧退避、只下载指定缺失品类，并在五分钟内去重。

修改时间：2026-08-31
修改作用：验证目标周期首次建库时可复用其他 A 股周期的缓存标的池。
"""
from __future__ import annotations

import json

import numpy as np

from core.cpu_shape_search_manager import BUCKETS, CpuShapeSearchManager
from core.market_data_service import MarketDataService, normalize_config
from core.mock_shape_generator import ShapeSample
from core.shape_features import full_pipeline


def _sample() -> ShapeSample:
    close = np.linspace(100.0, 112.0, 80) + np.sin(np.linspace(0.0, 10.0, 80)) * 3.0
    normalized = (close - close.min()) / np.ptp(close)
    points = np.column_stack([np.linspace(0.0, 1.0, len(close)), normalized]).tolist()
    sequence, _stats, vector = full_pipeline(points)
    return ShapeSample(
        sample_id=1,
        category="stock",
        symbol="SZ:000001",
        symbol_name="测试股票",
        shape_type="feature_match",
        raw_points=points,
        seq_128=sequence,
        vector=vector,
        start_ts=1,
        end_ts=80,
        close_price=float(close[-1]),
        change_pct=1.0,
        market_timestamps=list(range(1, 81)),
        close_series=close.tolist(),
        ohlc_series=np.column_stack([close, close + 1, close - 1, close]).tolist(),
        volume_series=np.linspace(1000.0, 2000.0, len(close)).tolist(),
        turnover_series=np.linspace(10_000_000.0, 20_000_000.0, len(close)).tolist(),
    )


def test_normalize_config_keeps_latest_bars_and_independent_categories():
    config = normalize_config({
        "timeframes": ["1w", "1h", "5m", "bad", "1d", "1w"],
        "feature_window": 120,
        "fetch_bars": 30,
        "stock": {"count": 15, "rank_metric": "volume_ratio", "rank_order": "bottom"},
        "crypto": {"count": 7, "rank_metric": "invalid", "rank_order": "invalid"},
    })
    assert config["data_source"] == "AKSHARE+BINANCE_PUBLIC"
    assert config["timeframes"] == ["1w", "60m", "5m", "1d"]
    assert config["fetch_bars"] == 120
    assert config["stock"]["count"] == 15
    assert config["crypto"]["count"] == 7
    assert config["crypto"]["rank_metric"] == "volume"
    assert config["crypto"]["rank_order"] == "top"


def test_normalize_config_keeps_zero_as_all_and_crypto_metric():
    config = normalize_config({
        "stock": {"count": 0, "rank_metric": "volume"},
        "crypto": {"count": 0, "rank_metric": "trade_count"},
    })
    assert config["stock"]["count"] == 0
    assert config["crypto"]["count"] == 0
    assert config["stock"]["rank_metric"] == "volume"
    assert config["crypto"]["rank_metric"] == "trade_count"


def test_unverified_legacy_cache_is_not_loaded(tmp_path, monkeypatch):
    writer = CpuShapeSearchManager(str(tmp_path))
    assert writer.build_from_samples([_sample()], timeframe="1w") == 1

    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    reader = CpuShapeSearchManager(str(tmp_path))
    service = MarketDataService(reader)
    assert service.start() == []
    status = service.status()
    assert status["legacy_cache_ignored"] is True
    assert status["buckets"]["stock_1w"] == 0


def test_explicitly_clearing_last_bucket_removes_npz(tmp_path):
    manager = CpuShapeSearchManager(str(tmp_path))
    assert manager.build_from_samples([_sample()], timeframe="1w") == 1
    assert manager.store_path.exists()
    assert manager.replace_from_samples([], timeframe="1w", categories={"stock"}) == 0
    assert not manager.store_path.exists()


def test_legacy_1h_cache_loads_as_60m(tmp_path):
    writer = CpuShapeSearchManager(str(tmp_path))
    assert writer.build_from_samples([_sample()], timeframe="1h") == 1
    reader = CpuShapeSearchManager(str(tmp_path))
    assert reader.load_all() == ["stock_60m"]
    assert reader.buckets_status()["stock_60m"] == 1


def test_verified_hybrid_weekly_cache_loads(tmp_path, monkeypatch):
    writer = CpuShapeSearchManager(str(tmp_path))
    assert writer.build_from_samples([_sample()], timeframe="1w") == 1
    (tmp_path / "market_data_manifest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "data_source": "AKSHARE+BINANCE_PUBLIC",
            "cache_committed": True,
            "period_tokens": {"1w": "test"},
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    reader = CpuShapeSearchManager(str(tmp_path))
    service = MarketDataService(reader)
    assert service.start() == ["stock_1w"]
    assert service.status()["buckets"]["stock_1w"] == 1
    snapshot = reader.market_snapshot("stock", "1w")
    assert snapshot[0].volume_values[-1] == 2000.0
    assert snapshot[0].turnover_values[-1] == 20_000_000.0
    service.update_config(
        {"adaptive_scale": False, "feature_window": 77, "fetch_bars": 160},
        refresh=False,
    )
    assert reader.config.min_length == 77
    assert reader.config.max_length == 77


def test_refresh_tokens_are_independent_by_category(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    token = service._period_token("1d")
    service._manifest = {"category_period_tokens": {"crypto_1d": token}}
    config = service.get_config()
    assert service._due_categories("1d", config, force=False) == {"stock"}
    assert service._due_categories("1d", config, force=True) == {"stock", "crypto"}


def test_refresh_failure_uses_backoff_but_force_can_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    now = [1000.0]
    monkeypatch.setattr("core.market_data_service.time.time", lambda: now[0])
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    service.update_config({"crypto": {"enabled": False}}, refresh=False)
    config = service.get_config()

    failures, delay, retry_at = service._schedule_retry_backoff("stock_1d")

    assert (failures, delay, retry_at) == (1, 900, 1900)
    assert service._due_categories("1d", config, force=False) == set()
    assert service._due_categories("1d", config, force=True) == {"stock"}
    assert service.status()["retry_backoff"]["stock_1d"]["remaining_seconds"] == 900

    now[0] = 1901.0
    assert service._due_categories("1d", config, force=False) == {"stock"}


def test_incomplete_refresh_logs_compact_error_and_schedules_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    service.update_config(
        {"timeframes": ["1d"], "crypto": {"enabled": False}},
        refresh=False,
    )

    def fake_build(_config, _timeframe, _log, _progress):
        return [], {
            "categories": {
                "stock": {
                    "source": "AKSHARE",
                    "selected": 0,
                    "built": 0,
                    "complete": False,
                    "error": "ProxyError: https://82.push2.eastmoney.com/a/very/long/url",
                }
            }
        }

    monkeypatch.setattr("core.market_data_service.build_market_dataset", fake_build)

    service._refresh_once(force=False)

    status = service.status()
    terminal_logs = "\n".join(status["logs"])
    assert status["retry_backoff"]["stock_1d"]["failures"] == 1
    assert "无法通过当前系统代理连接 A 股数据源" in terminal_logs
    assert "82.push2.eastmoney.com" not in terminal_logs
    assert service._due_categories("1d", service.get_config(), force=False) == set()


def test_ensure_timeframe_does_not_queue_during_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    service._schedule_retry_backoff("stock_1d")
    service._schedule_retry_backoff("crypto_1d")
    service._refresh_event.clear()

    result = service.ensure_timeframe("1d")

    assert result["state"] == "retry_backoff"
    assert result["queued"] is False
    assert result["retry_after_seconds"] >= 890
    assert not service._refresh_event.is_set()
    assert not any("[ON_DEMAND][QUEUE]" in line for line in service.status()["logs"])


def test_user_requested_stock_overrides_backoff_once_and_is_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    now = [1000.0]
    monkeypatch.setattr("core.market_data_service.time.time", lambda: now[0])
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    service._schedule_retry_backoff("stock_1d")
    service._schedule_retry_backoff("crypto_1d")

    first = service.ensure_timeframe("1d", categories=["stock"], user_requested=True)
    second = service.ensure_timeframe("1d", categories=["stock"], user_requested=True)

    assert first["state"] == "refresh_queued"
    assert first["requested_categories"] == ["stock"]
    assert second["state"] == "refresh_queued"
    assert service._pending_categories["1d"] == {"stock"}
    queue_logs = [line for line in service.status()["logs"] if "[ON_DEMAND][QUEUE]" in line]
    assert len(queue_logs) == 1


def test_pending_stock_refresh_does_not_build_crypto(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    service = MarketDataService(CpuShapeSearchManager(str(tmp_path)))
    captured = []

    def fake_build(config, timeframe, _log, _progress):
        captured.append((timeframe, config["_refresh_categories"]))
        return [], {
            "categories": {
                "stock": {
                    "source": "AKSHARE",
                    "selected": 0,
                    "built": 0,
                    "complete": False,
                    "error": "test",
                }
            }
        }

    monkeypatch.setattr("core.market_data_service.build_market_dataset", fake_build)
    service.ensure_timeframe("1d", categories=["stock"], user_requested=True)
    service._refresh_once(force=False)

    assert captured == [("1d", ["stock"])]


def test_active_buckets_do_not_include_futures():
    assert BUCKETS
    assert all(not bucket.startswith("futures_") for bucket in BUCKETS)


def test_ensure_timeframe_and_latest_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    manager = CpuShapeSearchManager(str(tmp_path))
    assert manager.build_from_samples([_sample()], timeframe="1d") == 1
    service = MarketDataService(manager)

    queued = service.ensure_timeframe("15m")

    assert queued["timeframe"] == "15m"
    assert queued["added"] is True
    assert "15m" in service.get_config()["timeframes"]
    status = service.status()
    assert status["last_kline_ts"]["stock_1d"] == 80


def test_periodic_refresh_keeps_cache_when_latest_kline_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    manager = CpuShapeSearchManager(str(tmp_path))
    sample = _sample()
    assert manager.build_from_samples([sample], timeframe="1d") == 1
    service = MarketDataService(manager)
    service.update_config({"crypto": {"enabled": False}}, refresh=False)

    def fake_build(_config, _timeframe, _log, _progress):
        assert _config["_fallback_stock_pool"] == [
            {"symbol": "SZ:000001", "name": "测试股票"}
        ]
        return [sample], {
            "categories": {"stock": {"complete": True, "built": 1, "clear": False}}
        }

    monkeypatch.setattr("core.market_data_service.build_market_dataset", fake_build)
    monkeypatch.setattr(
        manager,
        "replace_from_samples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应替换相同末根 K 线")),
    )

    service._refresh_once(force=False)

    token = service._period_token("1d", "stock")
    assert service._manifest["category_period_tokens"]["stock_1d"] == token
    assert "无需替换" in service.status()["message"]


def test_first_timeframe_build_reuses_stock_pool_from_other_timeframe(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_AUTO_REFRESH", "0")
    manager = CpuShapeSearchManager(str(tmp_path))
    assert manager.build_from_samples([_sample()], timeframe="1d") == 1
    service = MarketDataService(manager)
    service.update_config(
        {"timeframes": ["30m"], "crypto": {"enabled": False}}, refresh=False
    )

    def fake_build(config, timeframe, _log, _progress):
        assert timeframe == "30m"
        assert config["_fallback_stock_pool"] == [
            {"symbol": "SZ:000001", "name": "测试股票"}
        ]
        return [], {
            "categories": {
                "stock": {
                    "source": "AKSHARE_CACHED_POOL",
                    "selected": 1,
                    "built": 0,
                    "complete": False,
                    "error": "test",
                }
            }
        }

    monkeypatch.setattr("core.market_data_service.build_market_dataset", fake_build)

    service._refresh_once(force=False)
