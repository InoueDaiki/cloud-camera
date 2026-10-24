"""モデルが生成した SQL の検査。

- 1 文だけ、SELECT(UNION などの集合演算を含む)のみ
- 参照できるテーブルは detections だけ(WITH の別名は可)
- detections を FROM に持つ SELECT は、WHERE の AND 条件で `dt` の上下限を必ず指定する
  (dt = '...'、dt IN (...)、dt BETWEEN ... AND ...、dt >= ... AND dt <= ...)。範囲は MAX_DT_DAYS 日まで
- いちばん外側の LIMIT を MAX_ROWS 以下に強制する(なければ付ける)

最後の砦は Athena ワークグループのスキャン上限(BytesScannedCutoffPerQuery)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DIALECT = "athena"
MAX_ROWS = 100
MAX_DT_DAYS = 31
MAX_SQL_LENGTH = 4000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SqlRejected(ValueError):
    """ガードで拒否した。メッセージはモデルに返して書き直させる。"""


@dataclass
class GuardResult:
    sql: str
    limit: int
    dt_from: str
    dt_to: str
    # 実行はするが、モデルに知らせて直させたいこと(ts の条件がない等)
    warnings: list[str] = field(default_factory=list)


_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2}|[A-Za-z_]+/[A-Za-z_]+|UTC)?$")


def _ts_value(e: exp.Expression) -> datetime | None:
    """ts と比べているリテラルを UTC の datetime にする。読めない形なら None(検査しない)。"""
    if isinstance(e, exp.Cast) and isinstance(e.this, exp.Literal) and e.this.is_string:
        text = e.this.this
    elif isinstance(e, exp.FromISO8601Timestamp) and isinstance(e.this, exp.Literal):
        text = e.this.this
    elif isinstance(e, exp.Literal) and e.is_string:
        text = e.this
    else:
        return None
    m = _TS_RE.match(text.strip())
    if not m:
        return None
    day, hh, mm, ss, frac, zone = m.groups()
    if int(hh) >= 24:
        raise SqlRejected(f"時刻 {text!r} は不正です。24 時は翌日の '00:00:00' と書いてください")
    try:
        dt = datetime.fromisoformat(f"{day} {int(hh):02d}:{mm}:{ss or '00'}{frac or ''}")
    except ValueError as e:
        raise SqlRejected(f"時刻 {text!r} は不正です") from e
    if not zone or zone in ("Z", "UTC"):
        return dt.replace(tzinfo=timezone.utc)
    if zone[0] in "+-":
        z = zone.replace(":", "")
        off = timedelta(hours=int(z[1:3]), minutes=int(z[3:5]))
        return dt.replace(tzinfo=timezone(off if z[0] == "+" else -off)).astimezone(timezone.utc)
    try:
        return dt.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _is_ts(e: exp.Expression) -> bool:
    return isinstance(e, exp.Column) and e.name.lower() == "ts"


def _ts_bounds(where: exp.Where | None) -> tuple[datetime | None, datetime | None, bool]:
    """WHERE の AND 条件から ts の [下限, 上限) を求める(UTC)。3 つ目は ts の条件があったか。"""
    lo: datetime | None = None
    hi: datetime | None = None
    seen = False
    if where is None:
        return lo, hi, seen
    for c in _conjuncts(where.this):
        if isinstance(c, exp.Between) and _is_ts(c.this):
            seen = True
            a, b = _ts_value(c.args["low"]), _ts_value(c.args["high"])
            if a:
                lo = a if lo is None else max(lo, a)
            if b:
                b = b + timedelta(milliseconds=1)
                hi = b if hi is None else min(hi, b)
        elif isinstance(c, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
            if _is_ts(c.left):
                v, op = c.right, type(c)
            elif _is_ts(c.right):
                flip = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT}
                v, op = c.left, flip[type(c)]
            else:
                continue
            seen = True
            t = _ts_value(v)
            if t is None:
                continue
            if op in (exp.GTE, exp.GT):
                lo = t if lo is None else max(lo, t)
            else:
                t = t + timedelta(milliseconds=1) if op is exp.LTE else t
                hi = t if hi is None else min(hi, t)
    return lo, hi, seen


def _date(lit: exp.Expression) -> date:
    if not isinstance(lit, exp.Literal) or not lit.is_string or not _DATE_RE.match(lit.this):
        raise SqlRejected(f"dt は 'YYYY-MM-DD' の文字列リテラルで比較してください: {lit.sql(DIALECT)}")
    try:
        return date.fromisoformat(lit.this)
    except ValueError as e:
        raise SqlRejected(f"dt の日付が不正です: {lit.this}") from e


def _is_dt(e: exp.Expression) -> bool:
    return isinstance(e, exp.Column) and e.name.lower() == "dt"


def _conjuncts(e: exp.Expression | None) -> list[exp.Expression]:
    if e is None:
        return []
    while isinstance(e, exp.Paren):
        e = e.this
    if isinstance(e, exp.And):
        return _conjuncts(e.left) + _conjuncts(e.right)
    return [e]


def _dt_bounds(where: exp.Where | None) -> tuple[date, date] | None:
    """WHERE の AND 条件から dt の [下限, 上限](両端含む)を求める。片方でも欠ければ None。"""
    if where is None:
        return None
    lo: date | None = None
    hi: date | None = None

    def tighten_lo(d: date):
        nonlocal lo
        lo = d if lo is None else max(lo, d)

    def tighten_hi(d: date):
        nonlocal hi
        hi = d if hi is None else min(hi, d)

    for c in _conjuncts(where.this):
        if isinstance(c, exp.EQ) and (_is_dt(c.left) or _is_dt(c.right)):
            d = _date(c.right if _is_dt(c.left) else c.left)
            tighten_lo(d)
            tighten_hi(d)
        elif isinstance(c, exp.In) and _is_dt(c.this) and c.expressions and not c.args.get("query"):
            ds = [_date(x) for x in c.expressions]
            tighten_lo(min(ds))
            tighten_hi(max(ds))
        elif isinstance(c, exp.Between) and _is_dt(c.this):
            tighten_lo(_date(c.args["low"]))
            tighten_hi(_date(c.args["high"]))
        elif isinstance(c, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
            if _is_dt(c.left):
                d, op = _date(c.right), type(c)
            elif _is_dt(c.right):
                # '2026-10-24' <= dt を dt >= '2026-10-24' に読み替える
                flip = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT}
                d, op = _date(c.left), flip[type(c)]
            else:
                continue
            if op in (exp.GTE, exp.GT):
                tighten_lo(d)
            else:
                tighten_hi(d)
    if lo is None or hi is None:
        return None
    return lo, hi


def _outer_limit(root: exp.Expression) -> int | None:
    lim = root.args.get("limit")
    if lim is None:
        return None
    val = lim.expression if isinstance(lim, exp.Limit) else lim
    if isinstance(val, exp.Literal) and not val.is_string:
        try:
            return int(val.this)
        except ValueError:
            pass
    raise SqlRejected("LIMIT には整数リテラルを指定してください")


def _parse_one(sql: str) -> exp.Expression:
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except ParseError as e:
        raise SqlRejected(f"SQL を解析できません: {str(e).splitlines()[0]}") from e
    if len(stmts) != 1:
        raise SqlRejected("SQL は 1 文だけにしてください")
    return stmts[0]


def _check_ts(sel: exp.Select, lo: date, hi: date, warnings: list[str]) -> None:
    """ts の条件と dt の条件の整合を見る。dt が ts の範囲を覆っていなければ拒否、ts の条件がなければ警告。"""
    ts_lo, ts_hi, seen = _ts_bounds(sel.args.get("where"))
    if not seen:
        warnings.append(
            f"ts の条件がありません。dt は UTC の日付なので、この検索は日本時間の {lo:%m/%d} 9:00 〜 "
            f"{hi + timedelta(days=1):%m/%d} 9:00 を対象にしています。「今日」「昨日」や時間帯の質問なら、"
            "ts の範囲(TIMESTAMP '... Asia/Tokyo')を足して検索し直してください。")
        return
    need_lo = ts_lo.date() if ts_lo else None
    need_hi = (ts_hi - timedelta(milliseconds=1)).date() if ts_hi else None
    if need_lo and need_hi and need_hi < need_lo:
        return  # 空の範囲。実行しても 0 行
    if (need_lo and need_lo < lo) or (need_hi and need_hi > hi):
        want_lo = min(lo, need_lo) if need_lo else lo
        want_hi = max(hi, need_hi) if need_hi else hi
        raise SqlRejected(
            f"ts の範囲(UTC で {ts_lo:%Y-%m-%d %H:%M} 〜 {ts_hi:%Y-%m-%d %H:%M})に対して dt の範囲 "
            f"'{lo}'〜'{hi}' が足りません。dt BETWEEN '{want_lo}' AND '{want_hi}' にしてください"
            if ts_lo and ts_hi else
            f"ts の範囲に対して dt の範囲 '{lo}'〜'{hi}' が足りません。dt BETWEEN '{want_lo}' AND '{want_hi}' にしてください")


def _check(root: exp.Expression, table: str, database: str,
           warnings: list[str] | None = None) -> tuple[date, date]:
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise SqlRejected("SELECT 文だけ実行できます")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
                 exp.Merge, exp.Command, exp.TruncateTable)
    for node in root.walk():
        if isinstance(node, forbidden):
            raise SqlRejected("SELECT 以外の操作は実行できません")

    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    for t in root.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise SqlRejected("テーブル関数は使えません")
        name = t.name.lower()
        db = (t.db or "").lower()
        catalog = (t.catalog or "").lower()
        if not db and not catalog and name in cte_names:
            continue
        if name != table.lower():
            raise SqlRejected(f"参照できるテーブルは {table} だけです: {t.sql(DIALECT)}")
        if db and db != database.lower():
            raise SqlRejected(f"データベースは {database} だけです")
        if catalog and catalog != "awsdatacatalog":
            raise SqlRejected("カタログは指定しないでください")

    overall_lo: date | None = None
    overall_hi: date | None = None
    found = False
    for sel in root.find_all(exp.Select):
        sources = []
        if sel.args.get("from_"):
            sources.append(sel.args["from_"].this)
        elif sel.args.get("from"):
            sources.append(sel.args["from"].this)
        sources += [j.this for j in sel.args.get("joins") or []]
        direct = [s for s in sources if isinstance(s, exp.Table) and s.name.lower() == table.lower()
                  and not (not s.db and s.name.lower() in cte_names)]
        if not direct:
            continue
        found = True
        bounds = _dt_bounds(sel.args.get("where"))
        if bounds is None:
            raise SqlRejected(
                f"{table} を読む SELECT には WHERE で dt(UTC の日付、'YYYY-MM-DD')の範囲を必ず指定してください"
                "(例: dt BETWEEN '2026-10-23' AND '2026-10-24')")
        lo, hi = bounds
        if hi < lo:
            raise SqlRejected("dt の範囲が空です")
        if (hi - lo).days + 1 > MAX_DT_DAYS:
            raise SqlRejected(f"dt の範囲は {MAX_DT_DAYS} 日以内にしてください")
        if warnings is not None:
            _check_ts(sel, lo, hi, warnings)
        overall_lo = lo if overall_lo is None else min(overall_lo, lo)
        overall_hi = hi if overall_hi is None else max(overall_hi, hi)
    if not found:
        raise SqlRejected(f"{table} テーブルを参照してください")
    return overall_lo, overall_hi  # type: ignore[return-value]


def guard(sql: str, *, table: str = "detections", database: str = "", max_rows: int = MAX_ROWS,
          check_ts: bool = True) -> GuardResult:
    """検査して、実行してよい SQL(LIMIT を強制したもの)を返す。だめなら SqlRejected。

    check_ts: ts の条件と dt の条件の整合を見る(dt が足りなければ拒否、ts の条件がなければ warnings)。

    sqlglot で作り直した SQL ではなく、元の文字列に LIMIT だけ足して実行する
    (方言の変換で関数や型が変わるのを避けるため)。足した後にもう一度全部検査する。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("SQL が空です")
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if len(text) > MAX_SQL_LENGTH:
        raise SqlRejected(f"SQL が長すぎます({MAX_SQL_LENGTH} 文字まで)")

    root = _parse_one(text)
    warnings: list[str] = []
    _check(root, table, database, warnings if check_ts else None)
    limit = _outer_limit(root)
    if limit is None:
        text = f"{text}\nLIMIT {max_rows}"
    elif limit > max_rows:
        # いちばん後ろの LIMIT n を書き換える(外側の LIMIT は必ず末尾付近にある)
        matches = list(re.finditer(r"\bLIMIT\s+\d+", text, flags=re.IGNORECASE))
        if not matches:
            raise SqlRejected("LIMIT を書き換えられません")
        m = matches[-1]
        text = text[:m.start()] + f"LIMIT {max_rows}" + text[m.end():]

    # 書き換えた結果をもう一度検査する
    root = _parse_one(text)
    lo, hi = _check(root, table, database)
    limit = _outer_limit(root)
    if limit is None or limit > max_rows:
        raise SqlRejected(f"いちばん外側の LIMIT は {max_rows} 以下にしてください")
    return GuardResult(sql=text, limit=limit, dt_from=lo.isoformat(), dt_to=hi.isoformat(),
                       warnings=list(dict.fromkeys(warnings)))
