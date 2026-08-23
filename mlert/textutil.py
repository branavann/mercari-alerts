"""
Japanese-aware text normalisation and term extraction.

Two normalised forms are used throughout:

  tight(s)  - aggressive form used for *matching*. NFKC, lowercased, katakana
              folded to hiragana, and every separator / punctuation character
              removed. This is what makes all of these compare equal:
                  "C-E2" == "CE-2" == "CE2" == "Ｃ－Ｅ２"
                  "ゴーイング・メリー号" == "ゴーイングメリー号"
              The katakana prolonged-sound mark (ー) is deliberately KEPT,
              because dropping it would wrongly merge distinct words
              (ビル / ビール).

  spaced(s) - same normalisation but separators collapse to a single space.
              Handy for display and for query building.

Term extraction uses "script-run segmentation": Japanese text is chopped at
boundaries between katakana / hiragana / kanji / alphanumeric. It is a cheap,
dependency-free approximation of a morphological analyser, and for product
listings (which are mostly nouns and product codes) it works very well:

    ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号 F626
      -> ワンピース | 旧 | カードゲーム | ルフィ | 海賊団 | 出航 |
         ゴーイング・メリー | 号 | F626
    plus adjacent-run bigrams: ルフィ海賊団, ゴーイング・メリー号, ...
"""

import re
import unicodedata

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# Characters we keep in the "tight" form: latin alnum, hiragana, kanji,
# the katakana prolonged sound mark, and the kanji iteration mark.
_KEEP_TIGHT = re.compile(r"[^0-9a-zぁ-ゖ一-鿿ー々]")
_SEP_RUN = re.compile(r"\s+")

_KATA_START, _KATA_END = 0x30A1, 0x30F6
_KANA_OFFSET = 0x60


def kata_to_hira(s: str) -> str:
    """Fold full-width katakana to hiragana, leaving ー and ・ untouched."""
    out = []
    for ch in s:
        o = ord(ch)
        if _KATA_START <= o <= _KATA_END:
            out.append(chr(o - _KANA_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def nfkc(s: str) -> str:
    """NFKC-normalise: full-width latin/digits -> ASCII, half-width kana -> full."""
    return unicodedata.normalize("NFKC", s or "")


def tight(s: str) -> str:
    """Aggressive matching form (see module docstring)."""
    s = kata_to_hira(nfkc(s).lower())
    return _KEEP_TIGHT.sub("", s)


def spaced(s: str) -> str:
    """Normalised but separator-preserving form (separators -> single space)."""
    s = kata_to_hira(nfkc(s).lower())
    s = _KEEP_TIGHT.sub(" ", s)
    return _SEP_RUN.sub(" ", s).strip()


_ALNUM_ONLY = re.compile(r"^[0-9a-z]+$")


def _boundary_ok(term_t: str, text_t: str, idx: int) -> bool:
    """
    Guard against short latin/number codes matching inside longer runs:
    "f626" must not match inside "af6261". Only applied to short pure-alnum
    terms; Japanese script provides its own boundaries in practice.
    """
    if not (_ALNUM_ONLY.match(term_t) and len(term_t) <= 6):
        return True
    before = text_t[idx - 1] if idx > 0 else ""
    after_i = idx + len(term_t)
    after = text_t[after_i] if after_i < len(text_t) else ""
    return not (before.isascii() and before.isalnum()) and not (
        after.isascii() and after.isalnum()
    )


class Prepped:
    """
    A piece of text in both normalised forms.

    Both are needed. The tight form is what makes "C-E2" match a listing
    that writes "CE2" - separators are gone on both sides. But that same
    space-stripping turns "ONE PIECE" into "onepiece", which would hide the
    word "ONE" behind the boundary guard below. The spaced form keeps
    separators as spaces so ordinary word matching still works.
    """
    __slots__ = ("t", "s")

    def __init__(self, text=""):
        self.t = tight(text)
        self.s = " " + spaced(text) + " "

    def __repr__(self):
        return f"Prepped({self.t[:40]!r})"


def prep(text) -> "Prepped":
    return text if isinstance(text, Prepped) else Prepped(text)


_WORD_RE = {}


def _spaced_hit(term_t: str, spaced_text: str) -> bool:
    r = _WORD_RE.get(term_t)
    if r is None:
        r = _WORD_RE[term_t] = re.compile(
            r"(?<![0-9a-z])" + re.escape(term_t) + r"(?![0-9a-z])")
    return bool(r.search(spaced_text))


def contains(term: str, text) -> bool:
    """
    Is `term` present in `text`? `text` may be a Prepped object or, for
    convenience, an already-tightened string (tight form only).
    """
    t = tight(term)
    if not t:
        return False
    tight_text = text.t if isinstance(text, Prepped) else text

    idx = tight_text.find(t)
    while idx != -1:
        if _boundary_ok(t, tight_text, idx):
            return True
        idx = tight_text.find(t, idx + 1)

    # A short latin term rejected above may still be a real word that simply
    # sits next to another latin word ("ONE" in "ONE PIECE").
    if isinstance(text, Prepped) and _ALNUM_ONLY.match(t) and len(t) <= 6:
        return _spaced_hit(t, text.s)
    return False


def contains_any(terms, text):
    """Return the first term from `terms` found in text, else None."""
    for term in terms:
        if contains(term, text):
            return term
    return None


# --------------------------------------------------------------------------
# Script-run segmentation
# --------------------------------------------------------------------------

_C_KATA, _C_HIRA, _C_KANJI, _C_ALNUM, _C_OTHER = "K", "H", "J", "A", "O"


def _char_class(ch: str) -> str:
    o = ord(ch)
    if 0x30A1 <= o <= 0x30FA or o in (0x30FC, 0x30FD, 0x30FE):
        return _C_KATA
    if o == 0x30FB:  # ・ acts as a katakana-internal joiner (ゴーイング・メリー)
        return _C_KATA
    if 0x3041 <= o <= 0x3096:
        return _C_HIRA
    if 0x4E00 <= o <= 0x9FFF or o in (0x3005, 0x3007, 0x303B):
        return _C_KANJI
    if ch.isascii() and ch.isalnum():
        return _C_ALNUM
    if ch in "-‐‑–—_":  # keep hyphens inside codes like C-E2
        return _C_ALNUM
    return _C_OTHER


def script_runs(text: str):
    """
    Split text into (surface, class, start, end) runs by script type.
    Operates on the NFKC form so full-width latin behaves like ASCII.
    """
    s = nfkc(text)
    runs = []
    cur, cur_cls, start = [], None, 0
    for i, ch in enumerate(s):
        cls = _char_class(ch)
        if cls == cur_cls:
            cur.append(ch)
        else:
            if cur and cur_cls != _C_OTHER:
                runs.append(("".join(cur), cur_cls, start, i))
            cur, cur_cls, start = [ch], cls, i
    if cur and cur_cls != _C_OTHER:
        runs.append(("".join(cur), cur_cls, start, len(s)))

    cleaned = []
    for surface, cls, a, b in runs:
        if cls == _C_ALNUM:
            surface2 = surface.strip("-‐‑–—_")
            if not surface2:
                continue
            a += surface.index(surface2)
            b = a + len(surface2)
            surface = surface2
        elif cls == _C_KATA:
            surface2 = surface.strip("・")
            if not surface2:
                continue
            a += surface.index(surface2)
            b = a + len(surface2)
            surface = surface2
        cleaned.append((surface, cls, a, b))
    return cleaned


# --------------------------------------------------------------------------
# Stop words
# --------------------------------------------------------------------------

# Mercari listing boilerplate: shipping, condition disclaimers, payment terms.
# These are everywhere in descriptions and carry no identity information.
_STOP_SURFACE = """
商品 発送 状態 購入 即購入 ご購入 御購入 梱包 包装 返品 返金 交換 キャンセル 不可
神経質 完璧 完品 中古 中古品 送料 込み 無料 値下 値下げ 交渉 専用 コメント 質問
画像 写真 参考 判断 保管 保存 環境 喫煙 喫煙者 ペット 素人 自宅 当方 当時
使用 未使用 未開封 開封 開封済 新品 美品 良品 難あり 傷 汚れ スレ 折れ 剥がれ 白カケ
方法 発送方法 ネコポス ゆうパケット ゆうパケットポスト メルカリ メルカリ便 ゆうゆう
らくらく 匿名 匿名配送 配送 段ボール ダンボール 封筒 厚紙 スリーブ ローダー 補強
水濡れ 防止 すり替え 対応 以上 以下 場合 可能 出品 出品者 早い者勝ち バラ売り
到着 日時 時間 翌日 翌々日 以内 予定 連絡 総合 評価 全体 種別 詳細 内容 記載 説明
納得 理解 了承 遠慮 検討 幸い 注意 注意点 その他 こちら どうぞ お願い 宜しく
よろしく 致します 頂きます ください 下さい 思います 大丈夫 気軽 追加 限り 範囲
番号 枚 個 点 本 冊 組 束 円 円分 税込 期間 現在 実物 本物 全て 上記 下記
ありがとう ございます お手数 恐れ入り プロフィール 一読 必読 サイズ 重量
綺麗 きれい 時間 時間以内 以内 プレイ用 収納 暗所 保存環境 講習会 定員 開催 参加
主観 過度 割 反り 初期 判断 総合評価 交渉中 現状 現状渡し 動作 動作確認 未確認
折り 曲げ 日焼け 経年 経年劣化 汚 小傷 擦れ 個人 個人保管 完璧主義
"""
STOPWORDS = {tight(w) for w in _STOP_SURFACE.split() if tight(w)}

# Words that are real but too generic to anchor a search on their own. They
# still count as weak signals; the learner just won't build queries from them.
_GENERIC_SURFACE = """
カード シングルカード セット まとめ まとめ売り 大量 コレクション レア 限定 希少
当時物 レトロ ビンテージ ヴィンテージ 昭和 平成 ジャンク 訳あり 訳有り
ゲーム グッズ アイテム 本体 付属 付属品 おまけ 特典
"""
GENERIC = {tight(w) for w in _GENERIC_SURFACE.split() if tight(w)}


def is_stopword(term: str) -> bool:
    return tight(term) in STOPWORDS


def is_generic(term: str) -> bool:
    return tight(term) in GENERIC


# --------------------------------------------------------------------------
# Candidate term extraction
# --------------------------------------------------------------------------

_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[a-z]")


def extract_terms(text: str, max_len: int = 24):
    """
    Candidate identity terms from a title or description: script runs plus
    adjacent-run bigrams. Returns a de-duplicated list of surface forms.
    """
    runs = script_runs(text)
    out, seen = [], set()

    def add(surface):
        t = tight(surface)
        if not t or len(t) < 2 or len(t) > max_len:
            return
        if t.isdigit():
            return
        if t in STOPWORDS:
            return
        if t in seen:
            return
        seen.add(t)
        out.append(surface)

    for surface, cls, _a, _b in runs:
        if cls == _C_HIRA:
            continue  # hiragana-only runs are almost always grammar
        if cls == _C_KANJI and len(surface) < 2:
            continue  # bare single kanji (号, 枚, 巻) - kept only via bigrams
        add(surface)

    # Adjacent runs with nothing between them form a compound:
    #   ルフィ + 海賊団 -> ルフィ海賊団 ;  2000 + 年 -> 2000年 ;  34 + 号 -> 34号
    for (s1, c1, _a1, b1), (s2, c2, a2, _b2) in zip(runs, runs[1:]):
        if b1 != a2:
            continue
        if c1 == _C_HIRA or c2 == _C_HIRA:
            continue
        add(s1 + s2)

    return out


def term_weight_hint(term: str) -> float:
    """
    Heuristic "how identifying is this term" multiplier, used for ranking
    candidates when no corpus statistics are available.
    """
    t = tight(term)
    w = 1.0
    if not t:
        return 0.0
    w += min(len(t), 10) * 0.12           # longer = more specific
    if _HAS_DIGIT.search(t) and _HAS_ALPHA.search(t):
        w += 1.4                          # product codes: C-E2, PSA10, SFC-01
    elif _HAS_DIGIT.search(t):
        w += 0.3                          # 2000年, 34号
    if is_generic(term):
        w -= 1.0
    return max(w, 0.1)
