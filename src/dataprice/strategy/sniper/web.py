"""本机网页：跑一击必杀图表，并把每次结果留在本地目录。

成交仍来自 ``load_stored_sniper_view`` 的那次顺序单仓，页面只负责表单、展示和落盘。
不另写一套回测，也不保存原始 K 线全集。没有登录和数据库。
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Callable, Mapping

from flask import Flask, redirect, request, send_file

from dataprice.contracts import q_cash
from dataprice.strategy.sniper.backtest import SniperResult, SniperTrade
from dataprice.strategy.sniper.chart import ChartNotes, parse_chart_time, resolve_chart_window
from dataprice.strategy.sniper.params import SniperParams, evaluated_params
from dataprice.strategy.sniper.report import format_sniper_report
from dataprice.timeutil import UTC

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_OUTPUT_DIR = "var/sniper-web"
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SECRET = re.compile(
    r"(password|secret|token|api[_-]?key|credential|traceback|clickhouse://|postgres://|\.env)",
    re.IGNORECASE,
)
RUN_FILES = {
    "chart.html": "text/html; charset=utf-8",
    "trades.json": "application/json; charset=utf-8",
    "trades.csv": "text/csv; charset=utf-8",
    "params.json": "application/json; charset=utf-8",
    "summary.json": "application/json; charset=utf-8",
}


@dataclass(frozen=True)
class ChartJob:
    """网页提交的看图范围。费用沿用锁死的 v0.1，不从这里改。"""

    symbol: str
    months: int | None
    limit: int
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class ChartArtifacts:
    """一次运行要落盘的内容。``trades`` 是 ``SniperResult`` 里的成交，不是另一套信号。"""

    html: str
    trades: tuple[dict[str, object], ...]
    summary: dict[str, object]
    params: dict[str, object]


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    symbol: str
    created_at: str
    trade_count: str
    pnl: str


Executor = Callable[[ChartJob], ChartArtifacts]


def parse_job(form: Mapping[str, str]) -> ChartJob:
    """校验首页表单。填了起始时间就按 start/end，月数不再参与。"""
    symbol = (form.get("symbol") or "").strip() or "BTC-USDT"
    if not _SYMBOL.fullmatch(symbol) or ".." in symbol:
        raise ValueError("品种只接受字母、数字、点、下划线和短横线，例如 BTC-USDT")
    start_text = (form.get("start") or "").strip()
    end_text = (form.get("end") or "").strip()
    start = parse_chart_time(start_text) if start_text else None
    end = parse_chart_time(end_text) if end_text else None
    if start is None:
        months = _int_field(form.get("months") or "", "月数", default=24, low=1, high=120)
    else:
        months = None
    limit = _int_field(form.get("limit") or "", "最近 4H 根数", default=400, low=1, high=10000)
    if start is not None and end is not None and start >= end:
        raise ValueError("start 必须早于 end")
    return ChartJob(symbol=symbol, months=months, limit=limit, start=start, end=end)


def new_run_id(now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex}"


def trade_rows(result: SniperResult) -> tuple[dict[str, object], ...]:
    """把真实 ``SniperTrade`` 收成可保存的行。价格用十进制字符串。"""
    return tuple(_trade_row(trade) for trade in result.trades)


def trades_csv(rows: tuple[dict[str, object], ...] | list[dict[str, object]]) -> str:
    fields = [
        "side",
        "bias",
        "reason",
        "entry_ts",
        "exit_ts",
        "entry",
        "exit",
        "stop",
        "target",
        "qty",
        "pnl",
        "price_r",
        "account_r",
        "planned_price_r",
        "planned_account_r",
        "leverage_used",
        "entry_index",
        "exit_index",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
    return buffer.getvalue()


def summary_document(
    result: SniperResult,
    params: SniperParams,
    notes: ChartNotes | None = None,
) -> dict[str, object]:
    """汇总来自这次 ``SniperResult`` 和图表附注，不另算一笔盈亏。"""
    reasons: dict[str, int] = {}
    for trade in result.trades:
        reasons[trade.reason] = reasons.get(trade.reason, 0) + 1
    wins = [trade for trade in result.trades if trade.pnl > 0]
    pnl = q_cash(sum((trade.pnl for trade in result.trades), Decimal("0")))
    account_rs = [trade.account_r for trade in result.trades if trade.account_r is not None]
    price_rs = [trade.price_r for trade in result.trades if trade.price_r is not None]
    count = len(result.trades)
    document: dict[str, object] = {
        "trade_count": count,
        "wins": len(wins),
        "win_rate": (len(wins) / count) if count else 0.0,
        "pnl": format(pnl, "f"),
        "avg_price_r": (sum(price_rs) / len(price_rs)) if price_rs else 0.0,
        "avg_account_r": (sum(account_rs) / len(account_rs)) if account_rs else 0.0,
        "skipped": result.skipped,
        "bars": result.bars,
        "first_open": result.first_open.astimezone(UTC).isoformat(),
        "last_open": result.last_open.astimezone(UTC).isoformat(),
        "reasons": reasons,
        "report": format_sniper_report(result, params),
    }
    if notes is not None:
        document["chart"] = {
            "trades_drawn": notes.trades_drawn,
            "volume_shown": notes.volume_shown,
            "gate_available": notes.gate_available,
            "zones": len(notes.zones),
            "extended": notes.extended,
        }
    return document


def execute_chart_job(job: ChartJob, *, now: datetime | None = None) -> ChartArtifacts:
    """读库并画图。和 ``sniper-chart`` 同一条 ``load_stored_sniper_view``。"""
    from dataclasses import replace

    from dataprice.strategy.sniper.backtest import load_stored_sniper_view
    from dataprice.strategy.sniper.chart import build_sniper_figure, render_sniper_html

    clock = now or datetime.now(UTC)
    start, end = resolve_chart_window(
        months=job.months,
        start=job.start,
        end=job.end,
        now=clock,
    )
    params = replace(evaluated_params())
    view = load_stored_sniper_view(
        symbol=job.symbol,
        start=start,
        end=end,
        params=params,
    )
    figure, notes = build_sniper_figure(
        view.bars_4h,
        view.daily,
        view.result,
        params=params,
        macro=view.macro,
        gates=view.gates,
        start=start,
        end=end,
        limit=job.limit,
        symbol=job.symbol,
    )
    return ChartArtifacts(
        html=render_sniper_html(figure, notes),
        trades=trade_rows(view.result),
        summary=summary_document(view.result, params, notes),
        params={
            "symbol": job.symbol,
            "months": job.months,
            "limit": job.limit,
            "start": _iso(job.start),
            "end": _iso(job.end),
            "resolved_start": start.astimezone(UTC).isoformat(),
            "resolved_end": end.astimezone(UTC).isoformat(),
            "fee_bps": params.fee_bps,
            "slippage_bps": params.slippage_bps,
            "macro_version": params.macro_version,
            "source": "sniper-web",
            "backtest": "load_stored_sniper_view",
        },
    )


def persist_run(output_dir: Path, run_id: str, artifacts: ChartArtifacts, *, created_at: datetime) -> Path:
    """写入一个运行目录。编号必须是时间戳加 uuid，避免路径穿越。"""
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("运行编号不合法")
    root = _root(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / run_id
    if dest.exists():
        raise ValueError("运行编号已存在")
    staging = root / f".{run_id}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        params = dict(artifacts.params)
        params["run_id"] = run_id
        params["created_at"] = created_at.astimezone(UTC).isoformat()
        _write_text(staging / "chart.html", artifacts.html)
        _write_text(staging / "trades.json", _dump(list(artifacts.trades)))
        _write_text(staging / "trades.csv", trades_csv(artifacts.trades))
        _write_text(staging / "params.json", _dump(params))
        _write_text(staging / "summary.json", _dump(artifacts.summary))
        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def list_runs(output_dir: Path) -> tuple[StoredRun, ...]:
    """只列出输出目录里编号合法、文件齐全的运行。"""
    root_path = Path(output_dir).expanduser()
    if not root_path.exists() or not root_path.is_dir():
        return ()
    root = root_path.resolve()
    found: list[StoredRun] = []
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink() or not RUN_ID.fullmatch(child.name):
            continue
        if child.resolve().parent != root:
            continue
        record = _read_run(child)
        if record is not None:
            found.append(record)
    found.sort(key=lambda item: item.run_id, reverse=True)
    return tuple(found)


def safe_run_file(output_dir: Path, run_id: str, name: str) -> Path:
    """按运行编号和文件白名单取文件。用户路径不会被拼进去。"""
    if not RUN_ID.fullmatch(run_id) or name not in RUN_FILES:
        raise LookupError("文件不存在")
    root = _root(output_dir)
    located = root / run_id
    if located.is_symlink():
        raise LookupError("文件不存在")
    run_dir = located.resolve()
    try:
        run_dir.relative_to(root)
    except ValueError as exc:
        raise LookupError("文件不存在") from exc
    if run_dir.parent != root or not run_dir.is_dir():
        raise LookupError("文件不存在")
    direct = run_dir / name
    if direct.is_symlink() or not direct.is_file():
        raise LookupError("文件不存在")
    path = direct.resolve()
    if path.parent != run_dir:
        raise LookupError("文件不存在")
    try:
        path.relative_to(run_dir)
    except ValueError as exc:
        raise LookupError("文件不存在") from exc
    return path


def public_error(exc: BaseException) -> str:
    """给浏览器的句子。不附带 traceback，也不回显凭据。"""
    if isinstance(exc, (ValueError, RuntimeError)):
        text = " ".join(str(exc).split())
        if text and len(text) <= 400 and _SECRET.search(text) is None:
            return text
    return "回测没有完成。终端日志只记了错误类型，页面不显示内部信息。"


def create_app(output_dir: Path | str, *, executor: Executor = execute_chart_job) -> Flask:
    """本地图表服务。``executor`` 测试里换成不读库的函数。"""
    root = Path(output_dir).expanduser()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.debug = False
    app.testing = False

    @app.get("/")
    def home():
        return _page("一击必杀图表", _home_body(list_runs(root), error=None))

    @app.post("/runs")
    def create_run():
        try:
            job = parse_job(
                {
                    "symbol": request.form.get("symbol", ""),
                    "months": request.form.get("months", ""),
                    "limit": request.form.get("limit", ""),
                    "start": request.form.get("start", ""),
                    "end": request.form.get("end", ""),
                }
            )
        except ValueError as exc:
            body = _home_body(list_runs(root), error=public_error(exc))
            return _page("一击必杀图表", body), 400
        try:
            artifacts = executor(job)
            created = datetime.now(UTC)
            run_id = new_run_id(created)
            persist_run(root, run_id, artifacts, created_at=created)
        except Exception as exc:
            _log_failure(exc)
            body = _home_body(list_runs(root), error=public_error(exc))
            return _page("一击必杀图表", body), 400
        return redirect(f"/runs/{run_id}", code=303)

    @app.get("/runs/<run_id>")
    def show_run(run_id: str):
        try:
            safe_run_file(root, run_id, "chart.html")
            record = _read_run(root / run_id)
        except LookupError:
            return _page("没有这次运行", "<p>没有这次运行。</p>"), 404
        if record is None:
            return _page("没有这次运行", "<p>没有这次运行。</p>"), 404
        return _page(f"运行 {run_id}", _run_body(record))

    @app.get("/runs/<run_id>/<filename>")
    def run_file(run_id: str, filename: str):
        try:
            path = safe_run_file(root, run_id, filename)
        except LookupError:
            return _page("文件不存在", "<p>文件不存在。</p>"), 404
        download = filename != "chart.html" or request.args.get("download") == "1"
        response = send_file(
            path,
            mimetype=RUN_FILES[filename],
            as_attachment=download,
            download_name=filename,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(404)
    def missing(_err):
        return _page("没有这个页面", "<p>没有这个页面。</p>"), 404

    @app.errorhandler(413)
    def too_big(_err):
        return _page("请求过大", "<p>表单太大。</p>"), 413

    return app


def serve_sniper_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> None:
    """监听本机。debug 和自动重载都关掉。"""
    app = create_app(output_dir)
    log.info("一击必杀图表 http://%s:%s/  结果目录 %s", host, port, output_dir)
    app.run(host=host, port=port, debug=False, use_reloader=False)


def _int_field(value: str, name: str, *, default: int, low: int, high: int) -> int:
    text = value.strip()
    if not text:
        return default
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"{name}要是整数")
    number = int(text)
    if number < low or number > high:
        raise ValueError(f"{name}要在 {low} 到 {high} 之间")
    return number


def _trade_row(trade: SniperTrade) -> dict[str, object]:
    return {
        "side": trade.side,
        "bias": trade.bias,
        "reason": trade.reason,
        "entry_ts": trade.entry_ts.astimezone(UTC).isoformat(),
        "exit_ts": trade.exit_ts.astimezone(UTC).isoformat(),
        "entry": format(trade.entry, "f"),
        "exit": format(trade.exit, "f"),
        "stop": format(trade.stop, "f"),
        "target": format(trade.target, "f"),
        "qty": format(trade.qty, "f"),
        "pnl": format(trade.pnl, "f"),
        "price_r": trade.price_r,
        "account_r": trade.account_r,
        "planned_price_r": trade.planned_price_r,
        "planned_account_r": trade.planned_account_r,
        "leverage_used": trade.leverage_used,
        "entry_index": trade.entry_index,
        "exit_index": trade.exit_index,
    }


def _read_run(path: Path) -> StoredRun | None:
    if path.is_symlink() or not path.is_dir():
        return None
    if not all((path / name).is_file() and not (path / name).is_symlink() for name in RUN_FILES):
        return None
    try:
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        params = json.loads((path / "params.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(summary, dict) or not isinstance(params, dict):
        return None
    return StoredRun(
        run_id=path.name,
        symbol=_plain(params.get("symbol"), "BTC-USDT"),
        created_at=_plain(params.get("created_at"), ""),
        trade_count=_plain(summary.get("trade_count"), "0"),
        pnl=_plain(summary.get("pnl"), "0"),
    )


def _plain(value: object, fallback: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, (int, float)):
        return str(value)
    return fallback


def _root(output_dir: Path) -> Path:
    return Path(output_dir).expanduser().resolve()


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _log_failure(exc: BaseException) -> None:
    if isinstance(exc, (ValueError, RuntimeError)) and _SECRET.search(str(exc)) is None:
        log.error("sniper-web: %s", " ".join(str(exc).split()))
        return
    log.error("sniper-web run failed: %s", type(exc).__name__)


def _home_body(runs: tuple[StoredRun, ...], *, error: str | None) -> str:
    error_html = ""
    if error:
        error_html = f'<p class="error">{html.escape(error)}</p>'
    rows = []
    for run in runs:
        rid = html.escape(run.run_id)
        rows.append(
            "<tr>"
            f"<td>{html.escape(run.created_at)}</td>"
            f"<td>{html.escape(run.symbol)}</td>"
            f"<td>{html.escape(run.trade_count)}</td>"
            f"<td>{html.escape(run.pnl)}</td>"
            f'<td><a href="/runs/{rid}">打开</a> '
            f'<a href="/runs/{rid}/chart.html?download=1">HTML</a> '
            f'<a href="/runs/{rid}/trades.csv">CSV</a> '
            f'<a href="/runs/{rid}/trades.json">JSON</a></td>'
            "</tr>"
        )
    history = (
        "<p>还没有运行。</p>"
        if not rows
        else (
            "<table><thead><tr><th>时间</th><th>品种</th><th>笔数</th><th>净盈亏</th><th>结果</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    )
    return f"""
{error_html}
<form method="post" action="/runs">
  <label>品种 <input name="symbol" value="BTC-USDT" required></label>
  <label>月数 <input name="months" type="number" min="1" max="120" value="24"></label>
  <label>最近 4H 根数 <input name="limit" type="number" min="1" max="10000" value="400"></label>
  <label>起始 UTC <input name="start" placeholder="2024-01-01，可空"></label>
  <label>结束 UTC <input name="end" placeholder="可空，默认现在"></label>
  <button type="submit">生成图表</button>
</form>
<p class="note">填写起始时间后按起始和结束跑，月数不再参与。页面会等到这次顺序单仓画完。策略参数沿用 v0.1。</p>
<h2>历史</h2>
{history}
"""


def _run_body(run: StoredRun) -> str:
    rid = html.escape(run.run_id)
    return f"""
<p>{html.escape(run.symbol)} · {html.escape(run.created_at)} · {html.escape(run.trade_count)} 笔 · 净盈亏 {html.escape(run.pnl)}</p>
<p>
  <a href="/runs/{rid}/chart.html?download=1">下载 HTML</a>
  <a href="/runs/{rid}/trades.csv">下载 CSV</a>
  <a href="/runs/{rid}/trades.json">下载 JSON</a>
  <a href="/runs/{rid}/params.json">参数</a>
  <a href="/runs/{rid}/summary.json">汇总</a>
  <a href="/">返回</a>
</p>
<iframe title="sniper chart" src="/runs/{rid}/chart.html" width="100%" height="960"></iframe>
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: sans-serif; margin: 24px auto; max-width: 1080px; line-height: 1.5; color: #1c1c1c; }}
  header a {{ color: inherit; font-weight: 600; text-decoration: none; }}
  form {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; align-items: end; }}
  label {{ display: flex; flex-direction: column; gap: 4px; font-size: 14px; }}
  input {{ font: inherit; padding: 6px 8px; }}
  button {{ font: inherit; padding: 8px 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border-bottom: 1px solid #ddd; text-align: left; padding: 6px 8px; font-size: 14px; }}
  .error {{ background: #fdecea; border: 1px solid #f5c2c0; padding: 8px 12px; }}
  .note {{ color: #444; font-size: 14px; }}
  iframe {{ border: 1px solid #ddd; }}
</style>
</head>
<body>
<header><a href="/">一击必杀图表</a></header>
{body}
</body>
</html>"""
