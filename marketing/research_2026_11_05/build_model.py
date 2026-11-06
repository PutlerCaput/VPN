"""Build a transparent planning workbook; does not contact services or change prices."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import xlsxwriter

ROOT = Path(__file__).resolve().parent
OUT = Path("/workspace/artifacts/cyberdefency-market-20261105")


def main() -> None:
    a = json.loads((ROOT / "assumptions.json").read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    prices = {int(k): v for k, v in a["prices_rub"].items()}
    mix = {int(k): v for k, v in a["active_customer_mix"].items()}
    if not math.isclose(sum(mix.values()), 1):
        raise ValueError("Active customer mix must sum to 1")
    arpu = sum(prices[n] / n * mix[n] for n in prices)
    rates = a["payment_fee_all_in"] + a["tax_reserve_rate"] + a["refund_reserve_rate"]
    contribution = (
        arpu * (1 - rates)
        - a["support_and_risk_rub_per_active"]
        - a["vpn_node_rub_per_month"] / a["active_customers_per_node"]
    )
    ltv6 = contribution * sum(
        (1 - a["monthly_equivalent_churn"]) ** k for k in range(6)
    )

    def scale(n: int) -> dict[str, float | int]:
        nodes = math.ceil(n / a["active_customers_per_node"]) + a["spare_nodes"]
        traffic = (
            n * a["average_customer_traffic_gb"] * (1 + a["traffic_overhead_ratio"])
        )
        overage = (
            max(0, traffic - (nodes - a["spare_nodes"]) * a["node_included_traffic_gb"])
            * a["overage_rub_per_gb"]
        )
        result = (
            n * arpu * (1 - rates)
            - n * a["support_and_risk_rub_per_active"]
            - nodes * a["vpn_node_rub_per_month"]
            - a["control_fixed_rub_per_month"]
            - n * a["monthly_equivalent_churn"] * a["cac_all_in_rub"]
            - overage
        )
        return {
            "active": n,
            "nodes": nodes,
            "revenue": n * arpu,
            "traffic": traffic,
            "overage": overage,
            "cash_result": result,
            "after_owner_time": result - a["owner_time_rub_per_month"],
        }

    sizes = [25, 50, 75, 100, 150, 200, 250, 300, 500, 750, 1000]
    rows = [scale(n) for n in sizes]
    summary = {
        "arpu": arpu,
        "contribution_at_full_working_node": contribution,
        "ltv6_approx": ltv6,
        "ltv6_to_cac": ltv6 / a["cac_all_in_rub"],
        "cac_payback_client_months": a["cac_all_in_rub"] / contribution,
        "cash_break_even_active": next(
            n for n in range(1, 10000) if scale(n)["cash_result"] >= 0
        ),
        "owner_included_break_even_active": next(
            n for n in range(1, 10000) if scale(n)["after_owner_time"] >= 0
        ),
        "scale": rows,
        "assumptions": a,
    }
    (OUT / "model_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    with (OUT / "scale.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (OUT / "tariffs.csv").open("w", encoding="utf-8-sig", newline="") as f:
        tariff_writer = csv.writer(f)
        tariff_writer.writerow(
            ["days", "price_rub", "rub_per_30_days", "devices", "quota_gb_per_30_days"]
        )
        for n, price in prices.items():
            tariff_writer.writerow([30 * n, price, round(price / n, 2), 2, 200])

    book = xlsxwriter.Workbook(str(OUT / "cyberdefency-unit-economics-20261105.xlsx"))
    book.set_properties(
        {
            "title": "Cyberdefency — сценарная экономика",
            "author": "Cyberdefency",
            "comments": (
                "План, не фактическая отчётность. Формулы с сохранёнными "
                "расчётными значениями."
            ),
        }
    )
    title = book.add_format({"bold": True, "font_size": 18, "font_color": "#17365D"})
    header = book.add_format(
        {
            "bold": True,
            "bg_color": "#17365D",
            "font_color": "#FFFFFF",
            "text_wrap": True,
        }
    )
    note = book.add_format({"text_wrap": True, "valign": "top"})
    rub = book.add_format({"num_format": "#,##0.00", "font_color": "#222222"})
    pct = book.add_format({"num_format": "0.0%"})
    input_num = book.add_format(
        {"num_format": "#,##0.00", "font_color": "#0066CC", "bg_color": "#EEF6FF"}
    )
    input_pct = book.add_format(
        {"num_format": "0.0%", "font_color": "#0066CC", "bg_color": "#EEF6FF"}
    )
    integer = book.add_format({"num_format": "#,##0"})
    negative = book.add_format({"font_color": "#A00000", "bg_color": "#FFE5E5"})

    readme = book.add_worksheet("Readme")
    readme.set_column("A:A", 31)
    readme.set_column("B:B", 110)
    readme.merge_range("A1:B1", "Cyberdefency | модель запуска", title)
    explanations = [
        ("Дата", "05.11.2026. Тарифы и рекламные расходы не применены."),
        (
            "Как пользоваться",
            (
                "Синие ячейки Inputs, Plans и Acquisition можно менять. Формулы "
                "пересчитываются в Excel/LibreOffice. Снимки CSV/JSON относятся "
                "только к исходным допущениям."
            ),
        ),
        (
            "Комиссия",
            (
                "1% — гипотеза полной комиссии СБП. ЮKassa рассчитывает её "
                "индивидуально. Учесть НДС/чеки по вашему договору."
            ),
        ),
        (
            "Налог",
            (
                "6% — резерв модели, не установленный налог. НПД для физлиц может "
                "быть 4%; лимит 2,4 млн ₽ и авансы проверяются отдельно."
            ),
        ),
        (
            "Plans",
            (
                "Один продукт; 200 ГБ/30 суток, два профиля. Доля — доля активных "
                "клиентских месяцев, не заказов. Сумма долей должна быть 100%."
            ),
        ),
        (
            "Scale",
            (
                "Результат стабильной базы: замещение оттока включено; первичный "
                "набор, рост и разработка оплачиваются дополнительно. Один "
                "VPN-узел резервный."
            ),
        ),
        (
            "Серверы",
            (
                "75 подписок/узел и цена 1500 ₽ — допущения. Рабочие узлы плюс "
                "один резервный. Не считать имеющийся RU платёжный шлюз "
                "VPN-резервом."
            ),
        ),
        (
            "Трафик",
            (
                "Расход равномерно распределён по рабочим узлам; трафик резервного "
                "узла не используется для компенсации превышения. Тариф VPS, "
                "направления учёта и балансировку проверить."
            ),
        ),
        (
            "Маржа",
            (
                "Маржа клиента дана при заполненном рабочем узле, без "
                "фиксированных затрат, резерва узла и привлечения. Это не чистая "
                "прибыль."
            ),
        ),
        (
            "LTV6",
            (
                "Сценарное приближение по месячному эквиваленту оттока. Продления "
                "считать отдельно для сроков 30/90/180 дней, предоплата не уходит "
                "ежемесячно."
            ),
        ),
        (
            "Реклама",
            (
                "Acquisition — только гипотеза допустимого размещения. Полный CAC "
                "включает материалы. Правовые/платформенные ограничения описаны в "
                "отчёте."
            ),
        ),
        (
            "Платёжный канал",
            (
                "Модель относится к внешнему СБП. Внутри Telegram цифровые услуги "
                "требуют Stars. USDT требует отдельной допустимой схемы и расчёта."
            ),
        ),
        (
            "Денежный поток",
            (
                "Аванс за 6 месяцев не является прибылью одного месяца. Оставлять "
                "средства на обслуживание и возвраты."
            ),
        ),
    ]
    for row, (label, body) in enumerate(explanations, 3):
        readme.write(row, 0, label, header)
        readme.write(row, 1, body, note)
        readme.set_row(row, 44)

    inputs = book.add_worksheet("Inputs")
    inputs.set_column("A:A", 49)
    inputs.set_column("B:B", 18)
    inputs.set_column("C:C", 73)
    inputs.merge_range("A1:C1", "Изменяемые допущения", title)
    inputs.write_row(3, 0, ["Параметр", "Значение", "Пояснение"], header)
    specs = [
        (
            "payment_fee_all_in",
            "Комиссия платежа",
            "Полная ставка с применимыми надбавками",
        ),
        ("tax_reserve_rate", "Резерв на налоги", "Не определяет налоговый режим"),
        (
            "refund_reserve_rate",
            "Резерв возвратов",
            "Процент выручки, не измеренная доля возвратов",
        ),
        (
            "support_and_risk_rub_per_active",
            "Поддержка и резерв, ₽/клиента",
            "База: поддержка 25 ₽ + резерв 10 ₽",
        ),
        (
            "control_fixed_rub_per_month",
            "Постоянные расходы, ₽/месяц",
            "Без VPN-узлов: шлюз, мониторинг, бэкапы, домен, администрирование",
        ),
        (
            "vpn_node_rub_per_month",
            "Стоимость VPN-узла, ₽/месяц",
            "Заменить фактическим счётом",
        ),
        (
            "active_customers_per_node",
            "Подписок на рабочий узел",
            "Гипотеза, требуется нагрузочный тест",
        ),
        ("spare_nodes", "Резервных узлов", "Один дополнительный VPN-узел"),
        (
            "node_included_traffic_gb",
            "Включённый трафик на узел, ГБ",
            "Проверить квоту и направления учёта хостера",
        ),
        (
            "average_customer_traffic_gb",
            "Средний трафик клиента, ГБ",
            "Не равен максимальной квоте",
        ),
        ("traffic_overhead_ratio", "Накладной трафик", "Бюджетная оценка"),
        (
            "overage_rub_per_gb",
            "Дополнительный ГБ, ₽",
            "Стоимость превышения квоты хостера",
        ),
        (
            "monthly_equivalent_churn",
            "Месячный эквивалент оттока",
            "Модель, не наблюдение когорты",
        ),
        ("cac_all_in_rub", "Полный CAC, ₽", "Включает привлечение и материалы"),
        (
            "owner_time_rub_per_month",
            "Оценка труда владельца, ₽/месяц",
            "Не зарплата наёмного работника",
        ),
    ]
    refs = {}
    percentage_keys = {
        "payment_fee_all_in",
        "tax_reserve_rate",
        "refund_reserve_rate",
        "traffic_overhead_ratio",
        "monthly_equivalent_churn",
    }
    for row, (key, label, detail) in enumerate(specs, 4):
        refs[key] = f"Inputs!$B${row + 1}"
        inputs.write(row, 0, label)
        inputs.write(row, 1, a[key], input_pct if key in percentage_keys else input_num)
        inputs.write(row, 2, detail, note)
        if key in percentage_keys:
            inputs.data_validation(
                row,
                1,
                row,
                1,
                {
                    "validate": "decimal",
                    "criteria": "between",
                    "minimum": 0,
                    "maximum": 1,
                },
            )
        else:
            inputs.data_validation(
                row,
                1,
                row,
                1,
                {
                    "validate": "decimal",
                    "criteria": ">=",
                    "value": 1 if key == "active_customers_per_node" else 0,
                },
            )
    inputs.freeze_panes(4, 1)
    deduction_formula = "+".join(
        refs[k]
        for k in ["payment_fee_all_in", "tax_reserve_rate", "refund_reserve_rate"]
    )

    plans = book.add_worksheet("Plans")
    plans.set_column("A:B", 13)
    plans.set_column("C:H", 19)
    plans.merge_range("A1:H1", "Тарифы: два профиля, 200 ГБ на каждые 30 суток", title)
    plans.write_row(
        3,
        0,
        [
            "Месяцев",
            "Дней",
            "Оплата, ₽",
            "₽/30 суток",
            "Скидка",
            "Доля активных",
            "После %, ₽/срок",
            "Вклад за срок, ₽",
        ],
        header,
    )
    for row, n in enumerate(prices, 4):
        r = row + 1
        price = prices[n]
        plans.write(row, 0, n, integer)
        plans.write_formula(row, 1, f"=A{r}*30", integer, n * 30)
        plans.write(row, 2, price, input_num)
        plans.write_formula(row, 3, f"=C{r}/A{r}", rub, price / n)
        plans.write_formula(
            row, 4, f"=1-C{r}/($C$5*A{r})", pct, 1 - price / (prices[1] * n)
        )
        plans.write(row, 5, mix[n], input_pct)
        plans.write_formula(
            row, 6, f"=C{r}*(1-({deduction_formula}))", rub, price * (1 - rates)
        )
        plans.write_formula(
            row,
            7,
            f'=G{r}-A{r}*({refs["support_and_risk_rub_per_active"]}+{refs["vpn_node_rub_per_month"]}/{refs["active_customers_per_node"]})',
            rub,
            price * (1 - rates)
            - n
            * (
                a["support_and_risk_rub_per_active"]
                + a["vpn_node_rub_per_month"] / a["active_customers_per_node"]
            ),
        )
        plans.data_validation(
            row,
            5,
            row,
            5,
            {"validate": "decimal", "criteria": "between", "minimum": 0, "maximum": 1},
        )
    plans.write("C12", "Сумма долей")
    plans.write_formula("D12", "=SUM(F5:F10)", pct, 1)
    plans.conditional_format(
        "D12", {"type": "cell", "criteria": "!=", "value": 1, "format": negative}
    )
    plans.write("C14", "ARPU, ₽")
    plans.write_formula(
        "D14",
        "=IF(ABS(SUM(F5:F10)-1)>0.000001,NA(),SUMPRODUCT(D5:D10,F5:F10))",
        rub,
        arpu,
    )
    plans.write("C15", "Вклад/клиента")
    plans.write_formula(
        "D15",
        f'=D14*(1-({deduction_formula}))-{refs["support_and_risk_rub_per_active"]}-{refs["vpn_node_rub_per_month"]}/{refs["active_customers_per_node"]}',
        rub,
        contribution,
    )
    plans.write("C16", "LTV6, приближение")
    retention_terms = "+".join(
        f'(1-{refs["monthly_equivalent_churn"]})^{n}' for n in range(6)
    )
    plans.write_formula("D16", f"=D15*({retention_terms})", rub, ltv6)
    plans.write("C17", "LTV6 / CAC")
    plans.write_formula(
        "D17",
        f'=IFERROR(D16/{refs["cac_all_in_rub"]},0)',
        rub,
        ltv6 / a["cac_all_in_rub"],
    )
    plans.write("C18", "CAC / вклад")
    plans.write_formula(
        "D18",
        f'=IF(D15>0,{refs["cac_all_in_rub"]}/D15,NA())',
        rub,
        a["cac_all_in_rub"] / contribution,
    )
    plans.merge_range(
        "A21:H22",
        (
            "Вклад не включает фиксированные расходы, резервный узел, CAC и "
            "труд владельца. Для конечного результата см. Scale. Квота и срок "
            "— проект условий, не внедрённые настройки."
        ),
        note,
    )
    plans.freeze_panes(4, 2)

    sheet = book.add_worksheet("Scale")
    sheet.set_column("A:B", 12)
    sheet.set_column("C:L", 19)
    sheet.merge_range(
        "A1:L1",
        "Стабильная база: замещение оттока включено, стоимость роста отдельно",
        title,
    )
    sheet.write_row(
        3,
        0,
        [
            "Активные",
            "Узлы N+1",
            "Выручка",
            "Комиссия/налог/резерв",
            "Поддержка/резерв",
            "VPN-узлы",
            "Постоянные",
            "Замещение оттока",
            "Перерасход ГБ",
            "Денежный результат",
            "После труда владельца",
            "Трафик с накладными, ГБ",
        ],
        header,
    )
    sheet.set_row(3, 42)
    for row, data in enumerate(rows, 4):
        r = row + 1
        n = int(data["active"])
        sheet.write(row, 0, n, integer)
        formulas = {
            1: (
                f'=ROUNDUP(A{r}/{refs["active_customers_per_node"]},0)+{refs["spare_nodes"]}',
                data["nodes"],
            ),
            2: (f"=A{r}*Plans!$D$14", data["revenue"]),
            3: (f"=C{r}*({deduction_formula})", n * arpu * rates),
            4: (
                f'=A{r}*{refs["support_and_risk_rub_per_active"]}',
                n * a["support_and_risk_rub_per_active"],
            ),
            5: (
                f'=B{r}*{refs["vpn_node_rub_per_month"]}',
                data["nodes"] * a["vpn_node_rub_per_month"],
            ),
            6: (
                f'={refs["control_fixed_rub_per_month"]}',
                a["control_fixed_rub_per_month"],
            ),
            7: (
                f'=A{r}*{refs["monthly_equivalent_churn"]}*{refs["cac_all_in_rub"]}',
                n * a["monthly_equivalent_churn"] * a["cac_all_in_rub"],
            ),
            8: (
                f'=MAX(0,L{r}-(B{r}-{refs["spare_nodes"]})*{refs["node_included_traffic_gb"]})*{refs["overage_rub_per_gb"]}',
                data["overage"],
            ),
            9: (f"=C{r}-SUM(D{r}:I{r})", data["cash_result"]),
            10: (f'=J{r}-{refs["owner_time_rub_per_month"]}', data["after_owner_time"]),
            11: (
                f'=A{r}*{refs["average_customer_traffic_gb"]}*(1+{refs["traffic_overhead_ratio"]})',
                data["traffic"],
            ),
        }
        for col, (formula, value) in formulas.items():
            sheet.write_formula(row, col, formula, integer if col == 1 else rub, value)
    sheet.conditional_format(
        "J5:K15", {"type": "cell", "criteria": "<", "value": 0, "format": negative}
    )
    sheet.freeze_panes(4, 2)
    chart = book.add_chart({"type": "scatter", "subtype": "straight_with_markers"})
    for col, name in [(9, "До труда владельца"), (10, "После труда владельца")]:
        chart.add_series(
            {
                "name": name,
                "categories": ["Scale", 4, 0, 14, 0],
                "values": ["Scale", 4, col, 14, col],
            }
        )
    chart.set_title({"name": "Результат при базовых допущениях"})
    chart.set_x_axis({"name": "Активные платящие"})
    chart.set_y_axis({"name": "₽ / 30 дней"})
    chart.set_size({"width": 900, "height": 420})
    sheet.insert_chart("A19", chart)

    acq = book.add_worksheet("Acquisition")
    acq.set_column("A:A", 50)
    acq.set_column("B:B", 20)
    acq.set_column("C:C", 80)
    acq.merge_range("A1:C1", "Черновой тест рекламы — расходы не запускаются", title)
    funnel = [
        (4, "Полный бюджет, ₽", 30000, False),
        (5, "Доля медиарасходов", 0.8, True),
        (6, "Предлагаемый CPM, ₽", 600, False),
        (7, "Просмотр → переход (CTR)", 0.015, True),
        (8, "Переход → старт", 0.6, True),
        (9, "Старт → подключение", 0.7, True),
        (10, "Подключение → оплата", 0.3, True),
    ]
    for row, label, value, percent in funnel:
        acq.write(row, 0, label)
        acq.write(row, 1, value, input_pct if percent else input_num)
    acq.write("A14", "Медиабюджет, ₽")
    acq.write_formula("B14", "=B5*B6", rub, 24000)
    acq.write("A15", "Просмотры, модель")
    acq.write_formula("B15", "=IFERROR(B14/B7*1000,0)", rub, 40000)
    acq.write("A16", "Ожидаемые первые оплаты, модель")
    acq.write_formula("B16", "=B15*B8*B9*B10*B11", rub, 75.6)
    acq.write("A17", "Полный CAC, ₽")
    acq.write_formula("B17", "=IF(B16>0,B5/B16,NA())", rub, 30000 / 75.6)
    acq.write("A18", "Максимальный CPM при целевом CAC")
    acq.write_formula(
        "B18",
        f'={refs["cac_all_in_rub"]}*1000*B8*B9*B10*B11*B6',
        rub,
        a["cac_all_in_rub"] * 1000 * 0.015 * 0.6 * 0.7 * 0.3 * 0.8,
    )
    acq.merge_range(
        "A21:C23",
        (
            "Конверсии — предположения, не статистика рынка. Проверить "
            "разрешённость географии, рекламодателя и услуги. Google для "
            "российского рекламодателя не становится доступным от выбора "
            "другой страны. Платить за аудиторию конкурента без "
            "подтверждённого размещения нельзя."
        ),
        note,
    )

    sources = book.add_worksheet("Sources")
    sources.set_column("A:A", 8)
    sources.set_column("B:B", 50)
    sources.set_column("C:C", 85)
    sources.set_column("D:D", 90)
    sources.write_row(0, 0, ["ID", "Источник", "Адрес", "Ограничения"], header)
    if (ROOT / "sources.csv").exists():
        with (ROOT / "sources.csv").open(encoding="utf-8-sig", newline="") as f:
            for row, record in enumerate(csv.DictReader(f), 1):
                sources.write(row, 0, record["id"])
                sources.write(row, 1, record["title"], note)
                sources.write_url(row, 2, record["url"])
                sources.write(row, 3, record["limitations"], note)
                sources.set_row(row, 42)
    sources.freeze_panes(1, 0)
    book.close()
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"scale", "assumptions"}},
            ensure_ascii=False,
        )
    )
    print("WORKBOOK=" + str(OUT / "cyberdefency-unit-economics-20261105.xlsx"))


if __name__ == "__main__":
    main()
