"""Build the Word report using the bundled document runtime, offline only."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


POSITIONS = {"low": "Нижний край", "center": "Центр", "high": "Верхний край"}


def number(value, digits=2, signed=False):
    if value is None:
        return "н/д"
    return format(value, ("+" if signed else "") + f".{digits}f").replace(".", ",").replace("-", "−")


def p1text(estimate):
    if not estimate:
        return "н/д"
    if estimate["status"] == "measured_uncalibrated":
        return number(estimate["input_generator_p1db_dbm"], 1)
    if estimate["status"] == "not_reached":
        return ">10"
    return "н/д"


def local_time(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--draft", action="store_true")
    args = parser.parse_args()
    folder = args.folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))
    report_path = folder / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    rows = [json.loads(line) for line in (folder / "journal.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    estimates = json.loads((folder / "p1db.json").read_text(encoding="utf-8"))
    base = [r for r in rows if r["phase"] == "matrix"]
    measured = [e for e in estimates if e["status"] == "measured_uncalibrated"]
    statuses = Counter(e["status"] for e in estimates)
    band_count = len(manifest["bands"])
    planned_matrix = band_count * 3 * len(manifest["matrix_rf_dbm"]) * len(manifest["lo_dbm"])
    planned_p1db = band_count * 3 * len(manifest["lo_dbm"])
    includes_50mhz = any(low >= 50000000 for _, low, _ in manifest["bands"])
    output_name = f"{manifest['sample']}_mixer_test_2026-11-16.docx"
    if not args.draft:
        assert report.get("state") == "complete", "The hardware run must finish before final authoring"
        assert len(base) == planned_matrix and len(estimates) == planned_p1db
        assert len({r["spectrum_id"] for r in rows}) == len(rows)
        expected = {(hz, rf, lo) for _, low, high in manifest["bands"]
                    for hz in (low, (low + high) // 2, high)
                    for rf in manifest["matrix_rf_dbm"] for lo in manifest["lo_dbm"]}
        assert {(r["rf_hz"], r["rf_dbm"], r["lo_dbm"]) for r in base} == expected
        assert {(e["rf_hz"], e["lo_dbm"]) for e in estimates} == {(hz, lo) for hz, _, lo in expected}
        assert {r["spectrum_id"] for r in report["rows"]} == {r["spectrum_id"] for r in rows}
        assert not report["errors"] and not any(r["if_overload"] for r in rows)
        final = report["final_generator"]
        assert final["known"] and all(not c["output"] for c in final["state"]["channels"].values())
        assert max(e["bracket_high_dbm"] - e["bracket_low_dbm"] for e in measured) <= 0.5
    db = sqlite3.connect(Path(manifest["database"]).as_uri() + "?mode=ro", uri=True)
    payloads = [json.loads(item[0]) for item in db.execute(
        "SELECT payload FROM rf_spectrum WHERE json_extract(payload, '$.context.full_test_id')=?", (manifest["run_id"],))]
    db.close()
    if not args.draft:
        assert {p["id"] for p in payloads} == {r["spectrum_id"] for r in rows}
    first = payloads[0]
    output = folder / "report"
    output.mkdir(exist_ok=True)
    figures = output / "figures"
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin, section.bottom_margin = Inches(0.65), Inches(0.65)
    section.left_margin, section.right_margin = Inches(0.7), Inches(0.7)
    section.header_distance, section.footer_distance = Inches(0.25), Inches(0.25)
    width = 7.1
    for name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3", "Header", "Footer", "Caption"):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(11)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing = 1.08
    document.styles["Title"].font.size = Pt(24)
    document.styles["Title"].font.bold = True
    title_properties = document.styles["Title"]._element.find(qn("w:pPr"))
    if title_properties is not None:
        for border in list(title_properties.findall(qn("w:pBdr"))):
            title_properties.remove(border)
    document.styles["Subtitle"].font.size = Pt(12)
    document.styles["Subtitle"].font.italic = False
    document.styles["Heading 1"].font.size = Pt(18)
    document.styles["Heading 1"].paragraph_format.space_after = Pt(10)
    document.styles["Heading 2"].font.size = Pt(13)
    document.styles["Heading 2"].paragraph_format.space_before = Pt(9)
    document.styles["Caption"].font.size = Pt(9)
    document.styles["Caption"].font.italic = False
    document.styles["Caption"].font.bold = False
    header = section.header.paragraphs[0]
    header.text = f"GSM7 20H10   •   Измерительный стенд   •   {manifest['sample']}"
    header.style = document.styles["Header"]
    header.runs[0].font.size = Pt(9)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("16 ноября 2026   •   ").font.size = Pt(9)
    page = OxmlElement("w:fldSimple")
    page.set(qn("w:instr"), "PAGE")
    footer._p.append(page)
    title = f"Испытания смесителя {manifest['sample']} " + ("на КВ и 50 МГц" if includes_50mhz else "в КВ диапазонах")
    document.core_properties.title = title
    document.core_properties.subject = f"Края и центры {band_count} диапазонов и компрессия 1 дБ"
    document.core_properties.author = "Измерительный стенд GSM7-20H10"

    def para(text, style=None):
        return document.add_paragraph(text, style)

    def heading(text, level=1):
        return document.add_heading(text, level)

    def new_page(text):
        document.add_page_break()
        heading(text)

    def table(headers, data, widths, font=10):
        tab = document.add_table(rows=1, cols=len(headers))
        tab.autofit = False
        for column, inches in zip(tab.columns, widths):
            column.width = Inches(inches)
        for index, values in enumerate([headers] + list(data)):
            cells = tab.rows[0].cells if index == 0 else tab.add_row().cells
            if index == 0:
                repeat = OxmlElement("w:tblHeader")
                tab.rows[0]._tr.get_or_add_trPr().append(repeat)
            no_split = OxmlElement("w:cantSplit")
            tab.rows[index]._tr.get_or_add_trPr().append(no_split)
            for col, (cell, value, inches) in enumerate(zip(cells, values, widths)):
                cell.width = Inches(inches)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                cell.text = str(value)
                properties = cell._tc.get_or_add_tcPr()
                borders = OxmlElement("w:tcBorders")
                for edge in ("top", "left", "bottom", "right"):
                    border = OxmlElement("w:" + edge)
                    for key, item in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
                        border.set(qn("w:" + key), item)
                    borders.append(border)
                properties.append(borders)
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "203E5A" if index == 0 else ("F0F5F9" if index % 2 else "FFFFFF"))
                properties.append(shade)
                margins = OxmlElement("w:tcMar")
                for edge, amount in (("top", 65), ("bottom", 65), ("left", 70), ("right", 70)):
                    margin = OxmlElement("w:" + edge)
                    margin.set(qn("w:w"), str(amount))
                    margin.set(qn("w:type"), "dxa")
                    margins.append(margin)
                properties.append(margins)
                p = cell.paragraphs[0]
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.line_spacing = 1.0
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER if col or len(headers) > 4 else WD_ALIGN_PARAGRAPH.LEFT
                for run in p.runs:
                    run.font.size = Pt(font)
                    run.font.bold = index == 0
                    run.font.color.rgb = RGBColor(255, 255, 255) if index == 0 else RGBColor(0, 0, 0)
        spacer = para("")
        spacer.paragraph_format.space_after = Pt(2)
        spacer.paragraph_format.space_before = Pt(0)
        spacer.paragraph_format.line_spacing = Pt(2)
        return tab

    def image(name, caption, inches=width):
        path = figures / name
        if path.exists():
            p = para("")
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.keep_with_next = True
            p.add_run().add_picture(str(path), width=Inches(inches))
            para(caption, "Caption")
        else:
            para("График будет сформирован после завершения измерений", "Caption")

    def selected(band, position, lo, power=-30):
        return next((r for r in base if r["band_m"] == band and r["position"] == position
                     and r["lo_dbm"] == lo and r["rf_dbm"] == power), None)

    def estimate(band, position, lo):
        return next((e for e in estimates if e["band_m"] == band and e["position"] == position and e["lo_dbm"] == lo), None)

    para(title, "Title")
    para("Потери преобразования тракта и компрессия 1 дБ", "Subtitle")
    para(f"Проверка охватывает нижнюю границу, центр и верхнюю границу {band_count} диапазонов. "
         "Для каждой частоты изменяются уровни RF от −60 до 0 dBm и LO от +10 до +20 dBm. "
         "Отдельный поиск P1dB расширяет RF до разрешённых +10 dBm. ПЧ — 11,0592 МГц.")
    small_signal = [r["observed_loss_db"] for r in base if r["rf_dbm"] == -30]
    if small_signal:
        para(f"При RF −30 dBm разность уровней между выходом генератора и входом анализатора составляет "
             f"{number(min(small_signal))}–{number(max(small_signal))} дБ для всех измеренных частот и уровней LO. "
             f"Компрессия 1 дБ найдена в {len(measured)} из {len(estimates)} проверенных режимов."
             + (f" Найденный входной P1dB составляет "
                f"{number(min(e['input_generator_p1db_dbm'] for e in measured), 1)}…"
                f"{number(max(e['input_generator_p1db_dbm'] for e in measured), 1)} dBm." if measured else ""))
    if args.draft:
        para(f"ПРОМЕЖУТОЧНЫЙ МАКЕТ   Основных точек {len(base)} из {planned_matrix}. Тест продолжается.")
    image("overview.png", "Рисунок 1. Разность уровней включает кабели и соединения; значения относятся к портам приборов.")
    summary = []
    for band, low, high in manifest["bands"]:
        values = [r["observed_loss_db"] for r in base if r["band_m"] == band and r["rf_dbm"] == -30 and r["lo_dbm"] == 13]
        summary.append([f"{band} м", number((low + high) / 2e6, 6),
                        f"{number(min(values))}–{number(max(values))}" if values else "н/д",
                        p1text(estimate(band, "center", 13)), p1text(estimate(band, "center", 20))])
    table(["Диапазон", "Центр RF\nМГц", "Потери при LO 13\nдБ по трём частотам", "P1dB LO 13\ndBm в центре", "P1dB LO 20\ndBm в центре"],
          summary, [0.75, 1.35, 1.9, 1.55, 1.55], font=9.5)
    para("Калибровка кабелей не выполнялась. Здесь и далее потери относятся ко всему тракту, "
         "входной P1dB — к выходному разъёму RF генератора. Это предварительные характеристики "
         "подключённого смесителя, а не метрологически скорректированные параметры его портов.", "Caption")

    new_page("Схема подключения и методика")
    para("Канал A генератора SMU200A подаёт гетеродин на вход LO смесителя. Канал B подаёт испытательный сигнал "
         "на вход RF. Выход ПЧ смесителя соединён с входом RSA6265. Использовано верхнее расположение "
         "гетеродина: его частота на 11,0592 МГц выше RF. Для сравнения диапазонов предполагается "
         "одинаковое подключение во всех частях серии, включая продолжения после пауз.")
    if manifest.get("sample_notes"):
        para(manifest["sample_notes"])
    table(["Оборудование", "Назначение", "Идентификация"], [
        ["Rohde & Schwarz SMU200A", "A / канал 1 — LO\nB / канал 2 — RF", first["generator"]["identity"]],
        ["RIGOL RSA6265", "Приём сигнала ПЧ", first.get("identity", first.get("instrument", "RSA6265"))],
    ], [1.7, 1.7, 3.7], font=10)
    heading("Режим измерения", 2)
    table(["Параметр", "Установка"], [
        ["Промежуточная частота", "11,0592 МГц"],
        ["Уровни основной матрицы", "RF −60…0 dBm через 10 дБ; LO +10…+20 dBm через 1 дБ"],
        ["RSA полоса и развёртка", "Span 100 кГц; RBW 1 кГц; VBW 1 кГц; 801 точка"],
        ["RSA вход и детектор", "Ослабление 20 дБ; Ref Level +10 dBm; предусилитель выключен; детектор POS"],
        ["Генератор", "Непрерывная несущая CW; модуляции выключены; ALC включена"],
        ["Пауза после смены режима", "0,1 с перед запуском отдельной развёртки RSA"],
        ["Поиск сигнала", "Максимум в окне ±3 кГц около ПЧ; фон — медиана точек дальше ±10 кГц"],
    ], [2.0, 5.1], font=10)
    heading("Поиск компрессии", 2)
    para("Опорный коэффициент передачи усредняется по RF −60, −50 и −40 dBm. Для принятия опоры "
         "превышение пика над фоном должно быть не менее 20 дБ, а разброс трёх коэффициентов — не более "
         "0,3 дБ. Компрессия определяется как уменьшение коэффициента передачи на 1 дБ относительно этой опоры.")
    para("Если пересечение не обнаружено до 0 dBm, RF повышается до +2, +4, +6, +8 и не выше +10 dBm. "
         "Интервал пересечения делится пополам до ширины не более 0,5 дБ; внутри него используется линейная "
         "интерполяция. Ширина интервала характеризует разрешение поиска, а не полную погрешность измерения.")
    para("Ослабление RSA установлено до опорного уровня: при ослаблении 10 дБ прибор ограничивал "
         "Ref Level значением 0 dBm. Значения 20 дБ и +10 dBm подтверждены ответами SCPI. "
         "Во время каждой развёртки проверяется индикатор перегрузки ПЧ.")
    for recheck in report.get("rechecks", []):
        para(f"Перепроверка {number(recheck['rf_hz'] / 1e6, 0)} МГц при LO +{recheck['lo_dbm']} dBm "
             f"выполнена {local_time(recheck['ended_at'])} МСК отдельной серией. "
             "Опора RF −40/−30/−20 dBm усреднена по 5 спектрам на уровень; точки поиска — по 3. "
             f"Разрешённый предел RF +{recheck['rf_limit_dbm']} dBm; RSA: аттенюатор 30 дБ, Ref +20 dBm. "
             "Одна ранее отклонённая оценка заменена результатом этой перепроверки.")

    new_page("Частоты и обзор компрессии")
    table(["Диапазон", "Нижняя граница RF\nМГц", "Центр RF\nМГц", "Верхняя граница RF\nМГц"],
          [[f"{band} м", number(low / 1e6, 6), number((low + high) / 2e6, 6), number(high / 1e6, 6)]
           for band, low, high in manifest["bands"]], [0.9, 2.05, 2.05, 2.1])
    para("Центр — арифметическая середина принятых границ. Это сетка лабораторного испытания; "
         f"включены все {band_count} согласованных диапазонов, включая 60 м"
         + (" и точки 50, 51, 52 МГц." if includes_50mhz else ".") + " Частоты гетеродина получены "
         "добавлением ПЧ к каждой из указанных частот RF.")
    image("p1db-overview.png", "Рисунок 2. Найденный P1dB на выходе RF генератора. Пропуски не заменяются экстраполяцией.")
    table(["Результат поиска", "Количество режимов"], [
        ["Найдено пересечение 1 дБ", len(measured)],
        ["Не достигнуто до RF +10 dBm", statuses["not_reached"]],
        ["Опорные точки не прошли критерии", statuses["invalid_reference"]],
    ], [5.4, 1.7])
    para("В таблицах P1dB значение >10 означает, что компрессия 1 дБ не достигнута в разрешённом диапазоне RF. "
         "Обозначение н/д означает отсутствие принятой оценки. Такие режимы не включаются в числовые диапазоны P1dB.")

    for band, low, high in manifest["bands"]:
        new_page(f"Диапазон {band} м")
        para(f"RF {number(low / 1e6, 6)} / {number((low + high) / 2e6, 6)} / {number(high / 1e6, 6)} МГц. "
             "В таблице потери приведены при RF −30 dBm; P1dB относится к разъёму RF генератора.")
        band_data = []
        for position in POSITIONS:
            values = [selected(band, position, lo) for lo in (13, 20)]
            band_data.append([POSITIONS[position],
                              *[number(r["observed_loss_db"]) if r else "н/д" for r in values],
                              p1text(estimate(band, position, 13)), p1text(estimate(band, position, 20))])
        table(["Частота", "Потери LO 13\nдБ", "Потери LO 20\nдБ", "P1dB LO 13\ndBm", "P1dB LO 20\ndBm"],
              band_data, [1.5, 1.4, 1.4, 1.4, 1.4])
        image(f"band-{band}-power.png", "Передаточная характеристика. Показаны основные точки и дополнительные точки поиска P1dB.")
        image(f"band-{band}-loss.png", "Разность уровней генератор → RSA включает потери кабелей. Все три частоты при одинаковом RF.")
        band_measured = [e["input_generator_p1db_dbm"] for e in measured if e["band_m"] == band]
        if band_measured:
            para(f"Для этого диапазона найденные значения входного P1dB составляют "
                 f"{number(min(band_measured), 1)}…{number(max(band_measured), 1)} dBm по всем частотам и уровням LO. "
                 "При выборе режима учитывайте одновременно потери, компрессию и требуемую мощность гетеродина.")

    chunks = [manifest["bands"][index:index + 5] for index in range(0, band_count, 5)]
    for part, bands in enumerate(chunks, 1):
        new_page(f"Полная таблица P1dB часть {part}")
        para("Входной уровень на выходе RF генератора, dBm. Столбцы соответствуют уровню LO генератора, dBm. "
             "Для каждого диапазона строки идут в порядке нижняя граница, центр, верхняя граница.")
        grid = []
        for band, low, high in bands:
            for position, hz in zip(POSITIONS, (low, (low + high) // 2, high)):
                grid.append([f"{band} м\n{number(hz / 1e6, 6)}", *[p1text(estimate(band, position, lo)) for lo in range(10, 21)]])
        table(["Диапазон\nRF МГц", *[f"+{lo}" for lo in range(10, 21)]], grid, [1.27] + [0.53] * 11, font=9)
        para("Округление до 0,1 дБ служит для сравнения режимов. Интервал поиска не шире 0,5 дБ; "
             "полная неопределённость, в том числе потери подводящих кабелей, не оценивалась. "
             ">10 — порог не достигнут; н/д — опора не прошла контроль"
             + (" или режим ещё не измерен в промежуточном макете." if args.draft else "."))
        para("Выходной P1dB на входе анализатора и границы интервала для каждой оценки сохранены в p1db.csv. "
             "Эти значения можно пересчитать к портам смесителя после измерения потерь RF и ПЧ трактов.")
        if includes_50mhz and part == len(chunks):
            heading("Сравнение на верхних частотах", 2)
            comparison = []
            for band in (17, 10, 6):
                first_row = selected(band, "center", 13)
                second_row = selected(band, "center", 20)
                comparison.append([f"{band} м", number(first_row["rf_hz"] / 1e6, 6) if first_row else "н/д",
                    number(first_row["observed_loss_db"]) if first_row else "н/д",
                    number(second_row["observed_loss_db"]) if second_row else "н/д",
                    p1text(estimate(band, "center", 13)), p1text(estimate(band, "center", 20))])
            table(["Диапазон", "RF МГц", "Потери дБ\nLO +13", "Потери дБ\nLO +20", "P1dB dBm\nLO +13", "P1dB dBm\nLO +20"],
                  comparison, [0.7, 1.4, 1.25, 1.25, 1.25, 1.25], font=9.5)
            high_row, hf_row = selected(6, "center", 13), selected(10, "center", 13)
            if high_row and hf_row:
                para(f"При RF −30 dBm и LO +13 dBm разность потерь на 51 МГц и в центре диапазона 10 м "
                     f"равна {number(high_row['observed_loss_db'] - hf_row['observed_loss_db'], signed=True)} дБ. "
                     "Сравнение относится ко всему подключённому смесителю и кабелям.")
            if report.get("rechecks"):
                para("Отдельный вклад феррита 61 этим измерением не выделяется.")
                image("p1db-recheck.png", "Перепроверка 51 МГц / LO +20 dBm. Сжатие относительно новой усреднённой опоры; линия 1 дБ — порог P1dB.")
            else:
                para("Материал феррита — 61 по сведениям владельца. Этот тест показывает поведение смесителя "
                     "с такими сердечниками. Для отдельной оценки вклада феррита нужны измерения самих "
                     "трансформаторов; геометрия сердечников и обмоток в данном протоколе не заданы.")

    new_page("Контроль данных и воспроизводимость")
    ended = report.get("ended_at", checkpoint["updated_at"])
    para(f"Начало серии: {local_time(manifest['started_at'])} МСК. "
         f"{'Завершение' if report.get('state') == 'complete' else 'Состояние на'}: {local_time(ended)} МСК. "
         f"Образец в архиве: {manifest['sample']}. Идентификатор полной серии: {manifest['run_id']}.")
    table(["Проверка", "Результат"], [
        ["Основная матрица", f"{len(base)} из {planned_matrix} уникальных точек"],
        ["Дополнительные спектры P1dB", str(len(rows) - len(base))],
        ["Всего спектров в этой серии", str(len(rows))],
        ["Проверки P1dB", f"{len(estimates)} из {planned_p1db} режимов"],
        ["Перегрузка ПЧ RSA", str(sum(bool(r["if_overload"]) for r in rows)) + " событий"],
        ["Минимальное превышение пика над фоном", number(min(r["peak_above_background_db"] for r in rows), 1) + " дБ"],
        ["Состояние серии", "Завершена без ошибок" if report.get("state") == "complete" and not report.get("errors") else "Измерения продолжаются"],
        ["RF выходы после завершения", "Оба выключены и подтверждены опросом" if report.get("state") == "complete" else "Не применяется к промежуточному макету"],
    ], [3.7, 3.4])
    if report.get("rechecks"):
        para(f"Отдельная перепроверка P1dB содержит ещё {sum(item['spectrum_count'] for item in report['rechecks'])} спектра "
             "в той же БД с собственным идентификатором серии и ссылкой parent_full_test_id. "
             "Эти спектры не включены в число 4487 основной серии; результат заменяет одну оценку P1dB.")
    heading("Что определено этим испытанием", 2)
    para("Измерены амплитудная передача на разностной ПЧ и компрессия 1 дБ для однотонального сигнала. "
         "Развязки LO–RF и LO–IF, IP3, интермодуляция, зеркальный канал, шумовая фигура и работа "
         "при ПЧ 8,192 МГц в данную серию не входят. Спектр снимался в узкой полосе вокруг 11,0592 МГц; "
         "по этим данным нельзя оценить внеполосные продукты преобразования.")
    heading("Файлы и архив", 2)
    para("Все спектры сохранены в отдельной SQLite базе смесителей data/mixers.db. "
         "В программе откройте архив стенда смесителей и обновите список. Для выборки именно этой серии "
         "используйте поле context.full_test_id с приведённым выше идентификатором. "
         "Предварительные короткие прогоны в статистику данного отчёта не включены.")
    para("В папке серии находятся summary.csv с уровнями всех спектров, p1db.csv с оценками компрессии, "
         "manifest.json с настройками, report.json с результатами, journal.jsonl с последовательностью точек "
         "и scpi.log с обменом приборов. CSV использует разделитель точку с запятой и кодировку UTF-8 с BOM.")
    for name in ("summary.csv", "p1db.csv"):
        path = folder / name
        if path.exists():
            p = para(f"SHA256 {name}\n{hashlib.sha256(path.read_bytes()).hexdigest()}")
            for run in p.runs:
                run.font.size = Pt(9)
    para("Сетка границ диапазонов согласована для лабораторной проверки по плану IARU Region 1. "
         "Источник границ КВ: " + manifest["reference"]
         + (" Частоты 50, 51 и 52 МГц заданы владельцем как дополнительные лабораторные точки." if includes_50mhz else ""), "Caption")
    document.save(output / output_name)
    print(str(output / output_name))


if __name__ == "__main__":
    main()
