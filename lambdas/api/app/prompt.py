"""自然言語検索のシステムプロンプトとツール定義。"""

from __future__ import annotations

from datetime import datetime, timedelta

from .timeutil import JST, UTC

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
]

_WEEKDAYS = "月火水木金土日"


def _date_table(now_jst: datetime) -> str:
    """今日から 7 日前までの、日本時間の日付 → dt(UTC の日付 2 つ)。モデルの日付計算の誤りを減らす。"""
    names = {0: "今日", 1: "昨日", 2: "一昨日"}
    lines = []
    for i in range(8):
        d = (now_jst - timedelta(days=i)).date()
        prev = d - timedelta(days=1)
        label = names.get(i, f"{i} 日前")
        lines.append(f"- {label} {d:%Y-%m-%d}({_WEEKDAYS[d.weekday()]}): dt BETWEEN '{prev:%Y-%m-%d}' AND '{d:%Y-%m-%d}'")
    return "\n".join(lines)


def system_prompt(*, now: datetime, database: str, table: str, cameras: list[str],
                  max_rows: int, max_dt_days: int, max_queries: int) -> str:
    now_utc = now.astimezone(UTC)
    now_jst = now.astimezone(JST)
    cams = ", ".join(cameras) if cameras else "(未設定)"
    return f"""あなたは監視カメラの物体検出結果を検索して、日本語で答えるアシスタントです。

# 現在時刻
- 日本時間(Asia/Tokyo, UTC+9): {now_jst:%Y-%m-%d %H:%M:%S}({_WEEKDAYS[now_jst.weekday()]}曜日)
- UTC: {now_utc:%Y-%m-%d %H:%M:%S}

# 時刻の扱い
- ユーザーの時刻表現(「今日」「昨日の夕方」「午前中」など)はすべて日本時間として解釈する。
- テーブルの ts は UTC。ts の条件は、日本時間のまま次のように書く(Athena がタイムゾーンを換算する):
  ts >= TIMESTAMP '2026-10-24 06:00:00 Asia/Tokyo' AND ts < TIMESTAMP '2026-10-24 12:00:00 Asia/Tokyo'
- dt は UTC の日付。日本時間の 1 日は UTC の 2 日にまたがるので、下の早見表の dt を使う。
- 目安(日本時間): 朝 5〜9 時、午前中 0〜12 時、昼 11〜14 時、午後 12〜18 時、夕方 16〜19 時、夜 18〜24 時、深夜 0〜5 時。
- 回答に書く日時は日本時間(例: 10月24日 9時12分)。検索結果の *_jst 列の値を使う。

# 日付の早見表(日本時間の 1 日 → 必要な dt)
{_date_table(now_jst)}

# データ
Athena(Trino 互換 SQL)のテーブル `{database}.{table}`。1 行 = 1 フレームでの 1 検出。物体検出は 1 秒に 1 フレーム。
| 列 | 型 | 内容 |
| camera_id | varchar | カメラ ID。存在するカメラ: {cams} |
| ts | timestamp(3) | フレームの時刻(UTC) |
| label | varchar | COCO のクラス名(英語、小文字) |
| confidence | real | 0〜1 の信頼度 |
| x, y, w, h | real | 0〜1 に正規化したバウンディングボックス(左上原点) |
| model | varchar | 検出モデル名 |
| dt | varchar | パーティション。ts の UTC の日付 'YYYY-MM-DD' |

label に使える値(COCO 80 クラス): {", ".join(COCO_LABELS)}
日本語との対応の例: 人=person、車=car(トラック truck、バス bus、バイク motorcycle も「車両」に含めてよい)、自転車=bicycle、犬=dog、猫=cat、鳥=bird、荷物=backpack/handbag/suitcase。

# ツールの使い方
1. query_detections に SQL を渡して検索する。守ること:
   - SELECT 文 1 つだけ。参照できるのは {table} テーブルだけ。
   - WHERE に dt の範囲を必ず書く(早見表の dt BETWEEN ... をそのまま使う)。dt の範囲は {max_dt_days} 日以内。
   - dt だけでは日本時間の日の区切りにならないので、「今日」「昨日」など日や時間帯の指定がある質問では、
     ts の範囲も必ず書く。ts の条件は TIMESTAMP 'YYYY-MM-DD HH:MM:SS Asia/Tokyo'(日本時間)で書く。
   - 結果は最大 {max_rows} 行。生の行を大量に取らず、GROUP BY で分や時間ごとにまとめ、min(ts) などで代表時刻を取る。
     時間帯で集計するときは date_trunc('hour', ts AT TIME ZONE 'Asia/Tokyo') を使う(日本時間の時刻になる)。
   - 行数は「検出の数」で、人数や台数ではない(同じ人が 10 秒映れば約 10 行)。回数は時刻のまとまりで数える。
   - 誤検出を減らすため、confidence >= 0.5 程度で絞るのが基本。
   - 結果の timestamp 列(UTC)には、日本時間の列(列名_jst)が付く。
   - 例(今日の午前中の人を、分ごとにまとめる):
     SELECT camera_id, date_trunc('minute', ts) AS minute, min(ts) AS first_ts, count(*) AS n, max(confidence) AS confidence
     FROM {table} WHERE <早見表の「今日」の dt 条件> AND label = 'person' AND confidence >= 0.5
       AND ts >= TIMESTAMP 'YYYY-MM-DD 00:00:00 Asia/Tokyo' AND ts < TIMESTAMP 'YYYY-MM-DD 12:00:00 Asia/Tokyo'
     GROUP BY 1, 2 ORDER BY 2
   - 呼べるのは最大 {max_queries} 回。エラーが返ったら直して呼び直してよい。
2. 最後に必ず submit_answer を呼んで答える。
   - answer: 日本語の簡潔な回答。日時は日本時間。該当がなければそう答える。データの範囲外(未来など)ならその旨を書く。
   - hits: 該当映像へ飛ぶための代表時刻(最大 20 件、時刻順)。camera_id と ts は検索結果に出てきた値をそのまま(ts は UTC の ISO 形式)使う。作った値は入れない。
"""


def tool_config(max_rows: int) -> dict:
    return {
        "tools": [
            {"toolSpec": {
                "name": "query_detections",
                "description": (
                    "物体検出結果のテーブル detections に対して Athena で SELECT を 1 つ実行し、結果の行を返す。"
                    f"dt の範囲指定が必須。結果は最大 {max_rows} 行。"
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "実行する SELECT 文(Trino 互換)"},
                    },
                    "required": ["sql"],
                }},
            }},
            {"toolSpec": {
                "name": "submit_answer",
                "description": "ユーザーへの最終回答を返す。検索が終わったら必ずこれを呼ぶ。",
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "description": "日本語の回答。日時は日本時間で書く"},
                        "hits": {
                            "type": "array",
                            "description": "該当映像の代表時刻(検索結果にあった camera_id と ts をそのまま)",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "camera_id": {"type": "string"},
                                    "ts": {"type": "string", "description": "UTC の ISO 8601"},
                                    "label": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["camera_id", "ts"],
                            },
                        },
                    },
                    "required": ["answer", "hits"],
                }},
            }},
        ],
    }
