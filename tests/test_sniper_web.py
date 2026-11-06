"""本机图表服务。不连接数据库，默认执行器在测试里换成假的。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataprice.cli import main
from dataprice.contracts import q_cash
from dataprice.strategy.candles import Candle
from dataprice.strategy.sniper.backtest import SniperResult, SniperTrade, SniperView
from dataprice.strategy.sniper.params import evaluated_params
from dataprice.strategy.sniper.web import (
    RUN_ID,
    ChartArtifacts,
    ChartJob,
    create_app,
    execute_chart_job,
    list_runs,
    new_run_id,
    persist_run,
    public_error,
    safe_run_file,
    serve_sniper_web,
    summary_document,
    trade_rows,
    trades_csv,
)
from dataprice.timeutil import UTC


def test_home_lists_saved_run_after_restart(tmp_path: Path):
    calls = {"n": 0}

    def executor(job: ChartJob) -> ChartArtifacts:
        calls["n"] += 1
        assert job.symbol == "BTC-USDT"
        assert job.months == 24
        assert job.limit == 400
        assert job.start is None
        return _artifacts()

    app = create_app(tmp_path, executor=executor)
    assert app.debug is False
    client = app.test_client()
    home = client.get("/")
    assert home.status_code == 200
    assert "BTC-USDT" in home.text
    assert 'value="24"' in home.text
    assert 'value="400"' in home.text
    assert "还没有运行" in home.text

    created = client.post("/runs", data={"symbol": "", "months": "", "limit": ""})
    assert created.status_code == 303
    run_id = created.headers["Location"].rsplit("/", 1)[-1]
    assert RUN_ID.fullmatch(run_id)
    page = client.get(created.headers["Location"])
    assert page.status_code == 200
    assert "iframe" in page.text
    assert run_id in page.text
    chart = client.get(f"/runs/{run_id}/chart.html")
    assert chart.status_code == 200
    assert "MOCK-CHART" in chart.text
    assert "attachment" not in chart.headers.get("Content-Disposition", "")
    downloaded = client.get(f"/runs/{run_id}/trades.csv")
    assert "attachment" in downloaded.headers["Content-Disposition"]
    assert "100.5" in downloaded.text
    saved = json.loads((tmp_path / run_id / "trades.json").read_text(encoding="utf-8"))
    assert saved[0]["entry"] == "100.5"
    assert saved[0]["reason"] == "target"
    names = sorted(path.name for path in (tmp_path / run_id).iterdir())
    assert names == ["chart.html", "params.json", "summary.json", "trades.csv", "trades.json"]

    again = create_app(tmp_path, executor=executor)
    reopened = again.test_client().get("/")
    assert run_id in reopened.text
    assert again.test_client().get(f"/runs/{run_id}/chart.html").status_code == 200
    assert calls["n"] == 1


def test_validation_does_not_run_the_backtest(tmp_path: Path):
    def executor(_job: ChartJob) -> ChartArtifacts:
        raise AssertionError("不该跑回测")

    client = create_app(tmp_path, executor=executor).test_client()
    rejected = (
        {"symbol": "../BTC", "months": "24", "limit": "10"},
        {"symbol": "BTC/USDT", "months": "24", "limit": "10"},
        {"symbol": "BTC-USDT", "months": "0", "limit": "10"},
        {"symbol": "BTC-USDT", "months": "abc", "limit": "10"},
        {"symbol": "BTC-USDT", "months": "24", "limit": "0"},
        {"symbol": "BTC-USDT", "months": "24", "limit": "-3"},
        {"symbol": "BTC-USDT", "months": "24", "limit": "10", "start": "昨天"},
        {
            "symbol": "BTC-USDT",
            "months": "24",
            "limit": "10",
            "start": "2026-02-01",
            "end": "2026-01-01",
        },
    )
    for form in rejected:
        response = client.post("/runs", data=form)
        assert response.status_code == 400
        assert "Traceback" not in response.text
        assert "Error" not in response.text or "品种" in response.text or "月数" in response.text or "start" in response.text


def test_start_overrides_months_and_errors_stay_readable(tmp_path: Path, caplog):
    seen: dict[str, ChartJob] = {}

    def executor(job: ChartJob) -> ChartArtifacts:
        seen["job"] = job
        return _artifacts()

    client = create_app(tmp_path, executor=executor).test_client()
    response = client.post(
        "/runs",
        data={
            "symbol": "ETH-USDT",
            "months": "nope",
            "limit": "12",
            "start": "2026-01-01",
            "end": "2026-02-01",
        },
    )
    assert response.status_code == 303
    assert seen["job"].symbol == "ETH-USDT"
    assert seen["job"].months is None
    assert seen["job"].limit == 12
    assert seen["job"].start == datetime(2026, 1, 1, tzinfo=UTC)

    def boom(_job: ChartJob) -> ChartArtifacts:
        raise RuntimeError("没有宏观日频，也没有 factor_macro_gate v0.1")

    missing = create_app(tmp_path, executor=boom).test_client().post("/runs", data={"limit": "10"})
    assert missing.status_code == 400
    assert "没有宏观日频" in missing.text
    assert "Traceback" not in missing.text

    def secret(_job: ChartJob) -> ChartArtifacts:
        raise RuntimeError("clickhouse password=hunter2")

    with caplog.at_level("ERROR"):
        hidden = create_app(tmp_path, executor=secret).test_client().post("/runs", data={"limit": "10"})
    assert hidden.status_code == 400
    assert "hunter2" not in hidden.text
    assert "password" not in hidden.text.lower()
    assert "Traceback" not in hidden.text
    assert "hunter2" not in caplog.text

    def crash(_job: ChartJob) -> ChartArtifacts:
        raise KeyError("internal")

    broken = create_app(tmp_path, executor=crash).test_client().post("/runs", data={"limit": "10"})
    assert broken.status_code == 400
    assert "KeyError" not in broken.text
    assert "internal" not in broken.text


def test_paths_stay_inside_the_run_directory(tmp_path: Path):
    output = tmp_path / "out"
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret-token")
    created = datetime(2026, 3, 2, tzinfo=UTC)
    run_id = new_run_id(created)
    persist_run(output, run_id, _artifacts(), created_at=created)
    with pytest.raises(ValueError):
        persist_run(output, "../escape", _artifacts(), created_at=created)
    with pytest.raises(LookupError):
        safe_run_file(output, "../escape", "chart.html")
    with pytest.raises(LookupError):
        safe_run_file(output, run_id, "../secret.txt")
    with pytest.raises(LookupError):
        safe_run_file(output, run_id, "secret.txt")

    run_dir = output / run_id
    trades = run_dir / "trades.json"
    trades.unlink()
    trades.symlink_to(secret)
    client = create_app(output, executor=_refuse).test_client()
    leaked = client.get(f"/runs/{run_id}/trades.json")
    assert leaked.status_code == 404
    assert "top-secret-token" not in leaked.text

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chart.html").write_text("top-secret-token")
    link_id = "20260101T000000Z-" + "ab" * 16
    (output / link_id).symlink_to(outside, target_is_directory=True)
    linked = client.get(f"/runs/{link_id}/chart.html")
    assert linked.status_code == 404
    assert "top-secret-token" not in linked.text
    assert link_id not in client.get("/").text
    for path in (
        "/runs/../secret.txt",
        "/runs/%2e%2e/secret.txt",
        f"/runs/{run_id}/../../secret.txt",
        f"/runs/{run_id}/secret.txt",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert "top-secret-token" not in response.text
    assert list_runs(output) == ()


def test_summary_and_csv_copy_the_sniper_trade():
    trade = _trade()
    result = _result((trade,))
    rows = trade_rows(result)
    assert rows[0]["entry"] == "100.5"
    assert rows[0]["stop"] == "90"
    assert rows[0]["target"] == "130"
    assert rows[0]["pnl"] == "12.34"
    assert rows[0]["reason"] == "target"
    assert rows[0]["entry_ts"] == trade.entry_ts.isoformat()
    csv_text = trades_csv(rows)
    assert "100.5" in csv_text and "target" in csv_text and "buy" in csv_text
    summary = summary_document(result, evaluated_params())
    assert summary["trade_count"] == 1
    assert summary["pnl"] == format(q_cash(trade.pnl), "f")
    assert "12.3400" in str(summary["report"])
    assert public_error(RuntimeError("时间窗内没有 4H K 线")) == "时间窗内没有 4H K 线"
    assert "hunter2" not in public_error(RuntimeError("password=hunter2"))


def test_execute_chart_job_uses_the_stored_view(monkeypatch):
    bars = _bars()
    trade = _trade(bars)
    result = _result(bars, (trade,))
    view = SniperView(bars_4h=tuple(bars), daily=(), result=result)
    seen: dict[str, object] = {}

    def fake_load(**kwargs):
        seen.update(kwargs)
        return view

    monkeypatch.setattr("dataprice.strategy.sniper.backtest.load_stored_sniper_view", fake_load)
    now = datetime(2026, 4, 1, tzinfo=UTC)
    job = ChartJob(symbol="BTC-USDT", months=1, limit=4, start=None, end=None)
    artifacts = execute_chart_job(job, now=now)
    assert seen["symbol"] == "BTC-USDT"
    assert seen["start"] == now - timedelta(days=30)
    assert seen["end"] == now
    assert seen["params"].fee_bps == 5.0
    assert seen["params"].macro_version == "v0.1"
    assert artifacts.trades[0]["entry"] == "100.5"
    assert artifacts.trades[0]["exit"] == "110.25"
    assert "100.5" in artifacts.html
    assert artifacts.summary["trade_count"] == 1
    assert artifacts.params["limit"] == 4
    assert artifacts.params["backtest"] == "load_stored_sniper_view"
    source = Path(execute_chart_job.__code__.co_filename).read_text(encoding="utf-8")
    assert "load_stored_sniper_view" in source
    assert "build_sniper_figure" in source
    assert "run_sniper(" not in source


def test_cli_help_and_server_flags(capsys, monkeypatch, tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        main(["sniper-web", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--host" in text
    assert "127.0.0.1" in text
    assert "--port" in text
    assert "--output-dir" in text
    assert "debug" in text
    with pytest.raises(SystemExit) as bad:
        main(["sniper-web", "--port", "0"])
    assert bad.value.code == 2

    captured: dict[str, object] = {}

    def fake_run(self, host=None, port=None, debug=None, load_dotenv=True, **options):
        captured["host"] = host
        captured["port"] = port
        captured["debug"] = debug
        captured["use_reloader"] = options.get("use_reloader")

    monkeypatch.setattr("flask.Flask.run", fake_run)
    serve_sniper_web(output_dir=tmp_path)
    assert captured == {"host": "127.0.0.1", "port": 8765, "debug": False, "use_reloader": False}


def _refuse(_job: ChartJob) -> ChartArtifacts:
    raise AssertionError("不该跑回测")


def _artifacts() -> ChartArtifacts:
    trade = _trade()
    rows = trade_rows(_result((trade,)))
    return ChartArtifacts(
        html="<!DOCTYPE html><html><body>MOCK-CHART 100.5</body></html>",
        trades=rows,
        summary=summary_document(_result((trade,)), evaluated_params()),
        params={"symbol": "BTC-USDT", "months": 24, "limit": 400, "fee_bps": 5.0},
    )


def _bars() -> list[Candle]:
    start = datetime(2026, 3, 2, tzinfo=UTC)
    return [Candle(start + timedelta(hours=4 * index), 40, 42, 38, 41, 1) for index in range(4)]


def _trade(bars: list[Candle] | None = None) -> SniperTrade:
    if bars is None:
        opened = datetime(2026, 3, 2, tzinfo=UTC)
        entry_ts = opened
        exit_ts = opened + timedelta(hours=4)
        entry_index = 1
        exit_index = 2
    else:
        entry_ts = bars[1].open_ts
        exit_ts = bars[2].open_ts
        entry_index = 1
        exit_index = 2
    return SniperTrade(
        side="buy",
        entry_index=entry_index,
        exit_index=exit_index,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry=Decimal("100.5"),
        exit=Decimal("110.25"),
        qty=Decimal("1.5"),
        stop=Decimal("90"),
        target=Decimal("130"),
        reason="target",
        pnl=Decimal("12.34"),
        price_r=1.25,
        account_r=0.5,
        planned_price_r=3.0,
        planned_account_r=3.0,
        leverage_used=20.0,
        bias="long",
    )


def _result(bars_or_trades, trades: tuple[SniperTrade, ...] | None = None) -> SniperResult:
    if trades is None:
        trades = bars_or_trades
        opened = trades[0].entry_ts
        closed = trades[0].exit_ts
        count = 4
    else:
        bars = bars_or_trades
        opened = bars[0].open_ts
        closed = bars[-1].open_ts
        count = len(bars)
    return SniperResult(
        trades=trades,
        bars=count,
        skipped=0,
        first_open=opened,
        last_open=closed,
        first_close=40.0,
        last_close=41.0,
    )
