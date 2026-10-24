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
from dataclasses import dataclass
from datetime import date

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


def _check(root: exp.Expression, table: str, database: str) -> tuple[date, date]:
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
        overall_lo = lo if overall_lo is None else min(overall_lo, lo)
        overall_hi = hi if overall_hi is None else max(overall_hi, hi)
    if not found:
        raise SqlRejected(f"{table} テーブルを参照してください")
    return overall_lo, overall_hi  # type: ignore[return-value]


def guard(sql: str, *, table: str = "detections", database: str = "", max_rows: int = MAX_ROWS) -> GuardResult:
    """検査して、実行してよい SQL(LIMIT を強制したもの)を返す。だめなら SqlRejected。

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
    _check(root, table, database)
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
    return GuardResult(sql=text, limit=limit, dt_from=lo.isoformat(), dt_to=hi.isoformat())
