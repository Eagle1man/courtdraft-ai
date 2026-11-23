"""Сумма прописью по-русски. Только целые рубли; копейки — цифрами."""
from __future__ import annotations

_ONES = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_ONES_F = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
          "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
         "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот",
             "семьсот", "восемьсот", "девятьсот")
_SCALES = (
    (1_000_000_000, ("миллиард", "миллиарда", "миллиардов"), False),
    (1_000_000, ("миллион", "миллиона", "миллионов"), False),
    (1_000, ("тысяча", "тысячи", "тысяч"), True),
)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 рубль / 2 рубля / 5 рублей."""
    tail100, tail10 = n % 100, n % 10
    if 11 <= tail100 <= 14:
        return forms[2]
    if tail10 == 1:
        return forms[0]
    if 2 <= tail10 <= 4:
        return forms[1]
    return forms[2]


def _under_thousand(n: int, feminine: bool) -> list[str]:
    ones = _ONES_F if feminine else _ONES
    out: list[str] = []
    if n >= 100:
        out.append(_HUNDREDS[n // 100])
    rest = n % 100
    if 10 <= rest <= 19:
        out.append(_TEENS[rest - 10])
    else:
        if rest >= 20:
            out.append(_TENS[rest // 10])
        if rest % 10:
            out.append(ones[rest % 10])
    return out


def rubles_to_words(value: int) -> str:
    """Целое число рублей словами со строчной буквы."""
    if value < 0:
        raise ValueError("отрицательная сумма")
    if value == 0:
        return "ноль"
    words: list[str] = []
    rest = value
    for scale, forms, feminine in _SCALES:
        chunk = rest // scale
        rest %= scale
        if not chunk:
            continue
        words.extend(_under_thousand(chunk, feminine))
        words.append(plural(chunk, forms))
    if rest:
        words.extend(_under_thousand(rest, False))
    return " ".join(w for w in words if w)


def capitalize_words(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def words_equal(value: int, written: str) -> bool:
    """Сверка прописи с цифрами: регистр, пробелы и ё/е не значимы."""
    norm = " ".join((written or "").lower().replace("ё", "е").split())
    return norm == rubles_to_words(value).replace("ё", "е")
