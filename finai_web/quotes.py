"""股价：从公开行情源取基准日收盘价，两个源交叉核对。

为什么不用 Wind/Choice
    那两个是终端数据，有版权、要人工导出 —— 能自动跑的部分就自动跑。
    `market.py` 里「行情表只能人工导出」那条说的是**整张表**，其中真正只能人工
    拿到的其实只有价格：股本、有息负债、现金、少数股东权益都在年报里，
    抽取层已经给出了。所以这一层只补价格这一个缺口。

为什么必须两个源
    2026-10-12 记过一次：东方财富接口限流，单源取价当场失败。
    而且这是要写进报告的对外数字，一个源错了没法自查 ——
    两个源独立取，对不上就报错并把两个值都摆出来，让人自己看。

接口形状（2026-10-14 实测）
    腾讯  GET https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
          ?param=<sh600519|sz000661|bj430047|hk00700>,day,<起>,<止>,<条数>,qfq
          → {"code":0,"data":{"sz000661":{"qfqday":[["2026-09-30","70.560","71.830",...],...]}}}
          每行是 [日期, 开, 收, 高, 低, 量]，收盘价取下标 2。
          港股请求前复权时返回的键还是 `day`、每行末尾多一个对象 —— 解析不能假定列数。
    东财  GET https://push2his.eastmoney.com/api/qt/stock/kline/get
          ?secid=<0.000661|1.600519|116.00700>&klt=101&fqt=1&beg=YYYYMMDD&end=YYYYMMDD
          → {"data":{"klines":["2026-09-30,70.56,71.83,72.56,70.21,51879,372782467.99",...]}}
          逗号分隔：日期, 开, 收, 高, 低, 量, 额。收盘价取下标 2。

网络调用全部经过 `transport` 参数（默认 `http.get_json`），测试里换成假实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from .http import HttpError, get_json

TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

Transport = Callable[..., Any]

# 两个源允许的相差幅度（相对值）。同一交易日的收盘价本应逐分一致，
# 留一点余量只是避免「0.01 元级」的舍入差把整条流程卡住。
DEFAULT_TOLERANCE = 0.005


class QuoteError(RuntimeError):
    """取价失败。消息要能直接讲给使用者听。"""


def tencent_symbol(code: str, market: str = "CN") -> str:
    """腾讯的证券标识：沪 sh、深 sz、北 bj、港 hk，后面接代码。"""
    text = str(code or "").strip()
    if str(market).upper() == "HK":
        return f"hk{text.zfill(5)}"
    if text.startswith(("4", "8")) or text.startswith("92"):
        return f"bj{text}"
    if text.startswith(("6", "9")):
        return f"sh{text}"
    return f"sz{text}"


def eastmoney_secid(code: str, market: str = "CN") -> str:
    """东财的 secid：1 开头是沪市，0 是深市与北交所，116 是港股。"""
    text = str(code or "").strip()
    if str(market).upper() == "HK":
        return f"116.{text.zfill(5)}"
    if text.startswith("92"):  # 北交所新号段：属于 0 那一侧，不能当成沪市
        return f"0.{text}"
    if text.startswith(("6", "9")):
        return f"1.{text}"
    return f"0.{text}"


def _parse_date(raw: Any) -> date | None:
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_tencent(payload: Any, symbol: str) -> list[tuple[date, float]]:
    """腾讯的 K 线 → [(日期, 收盘价)]，按日期升序。

    键名不固定：请求前复权时是 `qfqday`，港股返回的是 `day`。所以不硬编码键名，
    而是找第一个「值是非空列表、且每行都解析得出日期与收盘价」的键。
    """
    if not isinstance(payload, Mapping):
        raise QuoteError(f"腾讯行情返回的不是对象：{str(payload)[:120]}")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise QuoteError("腾讯行情返回里没有 data")
    block = data.get(symbol)
    if not isinstance(block, Mapping):
        raise QuoteError(f"腾讯行情里没有 {symbol} 这个标的（代码或市场写错了？）")

    rows: list[tuple[date, float]] = []
    for value in block.values():
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
            continue
        parsed: list[tuple[date, float]] = []
        for row in value:
            # 每行至少 [日期, 开, 收]；港股多一列对象，所以只判下限、按下标取
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 3:
                continue
            day = _parse_date(row[0])
            if day is None:
                continue
            try:
                close = float(row[2])
            except (TypeError, ValueError):
                continue
            parsed.append((day, close))
        if parsed:
            rows = parsed
            break
    if not rows:
        raise QuoteError(f"腾讯行情里 {symbol} 没有可解析的日线数据")
    rows.sort(key=lambda item: item[0])
    return rows


def parse_eastmoney(payload: Any) -> list[tuple[date, float]]:
    """东财的 klines → [(日期, 收盘价)]，按日期升序。"""
    if not isinstance(payload, Mapping):
        raise QuoteError(f"东财行情返回的不是对象：{str(payload)[:120]}")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        # 代码不存在与接口限流都长这样：data 是 null
        raise QuoteError("东财行情返回里没有 data（代码不存在，或接口限流）")
    rows: list[tuple[date, float]] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 3:
            continue
        day = _parse_date(parts[0])
        if day is None:
            continue
        try:
            close = float(parts[2])
        except (TypeError, ValueError):
            continue
        rows.append((day, close))
    if not rows:
        raise QuoteError("东财行情里没有可解析的日线数据")
    rows.sort(key=lambda item: item[0])
    return rows


def pick_on_or_before(
    rows: Sequence[tuple[date, float]], as_of: date | None
) -> tuple[date, float]:
    """取「基准日当天或之前最近一个交易日」的收盘价。

    基准日落在周末或假期时不能报错 —— 取它前面最近的那个交易日是行业惯例。
    但要说清实际用的是哪一天：`as_of` 返回真实交易日，否则报告里写的基准日与价格对不上。
    """
    if not rows:
        raise QuoteError("行情数据是空的")
    if as_of is None:
        return rows[-1]
    eligible = [row for row in rows if row[0] <= as_of]
    if not eligible:
        raise QuoteError(f"{as_of.isoformat()} 之前没有行情数据（取的区间太窄？）")
    return eligible[-1]


def _fetch_tencent(
    symbol: str, *, start: date, end: date, transport: Transport
) -> list[tuple[date, float]]:
    param = f"{symbol},day,{start.isoformat()},{end.isoformat()},120,qfq"
    try:
        payload = transport(f"{TENCENT_URL}?param={param}", expected=(200,))
    except HttpError as err:
        raise QuoteError(f"腾讯行情取失败：{err}") from err
    return parse_tencent(payload, symbol)


def _fetch_eastmoney(
    secid: str, *, start: date, end: date, transport: Transport
) -> list[tuple[date, float]]:
    url = (
        f"{EASTMONEY_URL}?secid={secid}"
        "&fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&beg={start.strftime('%Y%m%d')}"
        f"&end={end.strftime('%Y%m%d')}&lmt=200"
    )
    try:
        payload = transport(url, expected=(200,))
    except HttpError as err:
        raise QuoteError(f"东财行情取失败：{err}") from err
    return parse_eastmoney(payload)


@dataclass(frozen=True)
class Quote:
    """一次取价的结果，含交叉核对的那一份。"""

    code: str
    price: float
    as_of: str
    source: str
    cross_price: float
    cross_source: str
    queried_at: str
    market: str = "CN"
    # 备源不可用（限流、断网）时为真。**两个源都有但数值打架**是另一回事 ——
    # 那种情况会直接报错，绝不降级成单源。
    single_source: bool = False

    @property
    def agreement(self) -> float:
        """两个源的相对差。0 就是逐分一致。"""
        if not self.cross_price:
            return 0.0
        return abs(self.price - self.cross_price) / self.cross_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "price": self.price,
            "as_of": self.as_of,
            "source": self.source,
            "cross_price": self.cross_price,
            "cross_source": self.cross_source,
            "agreement": self.agreement,
            "queried_at": self.queried_at,
            "market": self.market,
            "single_source": self.single_source,
        }


def latest_close(
    code: str,
    *,
    market: str = "CN",
    as_of: str | date | None = None,
    transport: Transport | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Quote:
    """取基准日（或最近交易日）的收盘价，两个源交叉核对。

    两个源对不上就抛 `QuoteError`，消息里同时给出两个值 —— 不静默取其一。
    这是要写进报告的对外数字，宁可停下来让人看一眼。
    """
    sender = transport or get_json
    base = _parse_date(as_of) if isinstance(as_of, str) else as_of
    if base is None:
        base = date.today()
    # 往前多取一段：基准日可能是周末或假期，得让「当天或之前」有得挑
    start = base - timedelta(days=40)

    symbol = tencent_symbol(code, market)
    secid = eastmoney_secid(code, market)
    primary_rows = _fetch_tencent(symbol, start=start, end=base, transport=sender)
    primary_day, primary_price = pick_on_or_before(primary_rows, base)

    try:
        secondary_rows = _fetch_eastmoney(secid, start=start, end=base, transport=sender)
    except QuoteError as err:
        # 备源不可用（限流、断网）不该让整条自动取数死在这里 —— 2026-10-14 实测东财
        # 对连续请求会直接断连接。但也不能假装核对过了：把「只有一个源」明确标出来，
        # 报告与行情表里都要写清，让人知道这个价还没交叉核对。
        return Quote(
            code=str(code),
            price=primary_price,
            as_of=primary_day.isoformat(),
            source="腾讯证券历史行情接口",
            cross_price=0.0,
            cross_source=f"备源不可用：{err}",
            queried_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            market=str(market),
            single_source=True,
        )
    secondary_day, secondary_price = pick_on_or_before(secondary_rows, base)

    if secondary_day != primary_day:
        raise QuoteError(
            f"{code} 两个行情源取到的交易日不同："
            f"腾讯 {primary_day.isoformat()}、东财 {secondary_day.isoformat()}。"
            "停在这里 —— 用哪一天的价会改变结论，得有人看一眼。"
        )
    if primary_price <= 0 or secondary_price <= 0:
        raise QuoteError(f"{code} 取到非正的价格：腾讯 {primary_price}、东财 {secondary_price}")
    spread = abs(primary_price - secondary_price) / secondary_price
    if spread > tolerance:
        raise QuoteError(
            f"{code} 两个行情源对不上（相差 {spread:.2%}）："
            f"腾讯 {primary_price:.2f}、东财 {secondary_price:.2f}，"
            f"交易日都是 {primary_day.isoformat()}。停在这里 —— 不静默取其中一个。"
        )

    return Quote(
        code=str(code),
        price=primary_price,
        as_of=primary_day.isoformat(),
        source="腾讯证券历史行情接口",
        cross_price=secondary_price,
        cross_source="东方财富行情接口",
        queried_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        market=str(market),
    )


# ---------------------------------------------------------------------------
# beta：60 个月月度收益率对沪深300 的回归
# ---------------------------------------------------------------------------

INDEX_SYMBOL = "sh000300"
MIN_BETA_MONTHS = 24


@dataclass(frozen=True)
class BetaEstimate:
    """一次 beta 回归的结果。窗口与相关系数都要留痕 —— 报告里要写口径。"""

    beta: float
    correlation: float
    months: int
    window_start: str
    window_end: str
    index_symbol: str = INDEX_SYMBOL

    def describe(self) -> str:
        return (
            f"{self.beta:.4f}；用 {self.months} 个月月度收益率（前复权）对沪深300 回归，"
            f"窗口 {self.window_start}~{self.window_end}，相关系数 {self.correlation:.2f}"
        )


def monthly_returns(rows: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """[(日期, 收盘价)] → [(日期, 月收益率)]。最后一条没有下一期，所以少一个点。"""
    ordered = sorted(rows, key=lambda item: item[0])
    out: list[tuple[date, float]] = []
    for index in range(1, len(ordered)):
        previous = ordered[index - 1][1]
        if previous <= 0:
            continue
        out.append((ordered[index][0], ordered[index][1] / previous - 1.0))
    return out


def estimate_beta(
    code: str,
    *,
    market: str = "CN",
    months: int = 60,
    index: str = INDEX_SYMBOL,
    transport: Transport | None = None,
) -> BetaEstimate:
    """用月度收益率对沪深300 做一元回归，取斜率作为 beta。

    口径与 Wind/Choice 一致（月度、60 期、对比沪深300），终端导出会有小数位差异。
    这条口径要写进报告 —— beta 直接决定 WACC，进而决定每股价值，不能只说「取 1.2」。
    """
    sender = transport or get_json
    symbol = tencent_symbol(code, market)
    # 多取两根：收益率要前一期做分母
    param_count = max(MIN_BETA_MONTHS, int(months)) + 2

    def fetch(symbol_text: str) -> list[tuple[date, float]]:
        param = f"{symbol_text},month,,,{param_count},qfq"
        try:
            payload = sender(f"{TENCENT_URL}?param={param}", expected=(200,))
        except HttpError as err:
            raise QuoteError(f"取月线失败（{symbol_text}）：{err}") from err
        return parse_tencent(payload, symbol_text)

    stock = monthly_returns(fetch(symbol))
    bench = monthly_returns(fetch(index))
    # 按「年-月」对齐：两个市场的月末日期可能差一天（假期），按日期对齐会全错开
    by_month = {(item[0].year, item[0].month): item[1] for item in bench}
    pairs = [
        (value, by_month[(day.year, day.month)])
        for day, value in stock
        if (day.year, day.month) in by_month
    ]
    if len(pairs) < MIN_BETA_MONTHS:
        raise QuoteError(
            f"可用于回归的月份只有 {len(pairs)} 个（至少需要 {MIN_BETA_MONTHS} 个）—— "
            "可能是新上市公司，或行情接口返回的月线不够"
        )

    count = len(pairs)
    mean_stock = sum(item[0] for item in pairs) / count
    mean_bench = sum(item[1] for item in pairs) / count
    cov = sum((a - mean_stock) * (b - mean_bench) for a, b in pairs)
    var_stock = sum((a - mean_stock) ** 2 for a, _ in pairs)
    var_bench = sum((b - mean_bench) ** 2 for _, b in pairs)
    if var_bench <= 0:
        raise QuoteError("沪深300 的月度收益率没有波动，回归做不了（数据有问题）")
    beta = cov / var_bench
    correlation = (
        cov / ((var_stock**0.5) * (var_bench**0.5)) if var_stock > 0 else 0.0
    )

    window = sorted(day for day, value in stock if (day.year, day.month) in by_month)
    return BetaEstimate(
        beta=beta,
        correlation=correlation,
        months=count,
        window_start=window[0].isoformat(),
        window_end=window[-1].isoformat(),
    )
