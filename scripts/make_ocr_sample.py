"""Собрать PDF без текстового слоя: картинка с русским текстом.

Нужен для проверки OCR: из такого файла обычное извлечение текста вернёт
пустоту, и только распознавание даст содержимое.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.environ.get("TEMP", r"C:\Users\diorl\AppData\Local\Temp"), "ocr_scan.pdf")
FONT = r"C:\Windows\Fonts\arial.ttf"

LINES = [
    "В СУД СУДЕБНОГО УЧАСТКА № 123",
    "",
    "ИСКОВОЕ ЗАЯВЛЕНИЕ",
    "",
    "Истец: Иванов Иван Иванович",
    "Ответчик: Петров Петр Петрович",
    "",
    "Прошу взыскать задолженность по договору займа",
    "в размере 150 000 рублей и расходы по госпошлине",
    "в размере 4 200 рублей.",
    "",
    "Приложение: договор займа, расписка.",
    "",
    "Дата: 01 ноября 2026 года",
]

image = Image.new("RGB", (1700, 2200), "white")
draw = ImageDraw.Draw(image)
font = ImageFont.truetype(FONT, 44)
y = 120
for line in LINES:
    draw.text((120, y), line, fill="black", font=font)
    y += 80
image.save(OUT, "PDF", resolution=150.0)
print(OUT, os.path.getsize(OUT))
