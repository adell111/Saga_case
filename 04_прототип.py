#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI-прототип кредитного конвейера — кейс 2.

Использование:
    python 04_прототип.py applications.csv
    python 04_прототип.py applications.csv --out decisions.csv
    python 04_прототип.py applications.csv --timeout-clients cli_0003,cli_0010
    python 04_прототип.py applications.csv --show-diff
    python 04_прототип.py applications.csv --use-csv-external

--use-csv-external берёт ответы БКИ и СМЭВ из колонок bki_score и smev_status
самого csv (если там пусто, то из эмулятора). Нужно для сверки и для тестов,
где ответ внешнего сервиса уже задан в условии. Обычный режим - эмуляторы.

TODO (от организаторов), всё сделано:
    1. Дописать validate() - правила из 02_правила.md. (сделано)
    2. Дописать bki_emulator() и smev_emulator(). (сделано)
    3. Дописать score() - веса из 02_правила.md. (сделано)
    4. Дописать decide() - правила отказа. (сделано, их 9 + APPROVED)
    5. Считать DTI по формуле. (сделано)
    6. Записать decisions.csv. (сделано)

Порядок шагов: валидация, СМЭВ, БКИ, скоринг, DTI, правила отказа, решение,
аудит. Настоящих БКИ и СМЭВ нет, вместо них эмуляторы на md5 (описаны в
06_external_services.md). Только стандартная библиотека.

Рядом с decisions.csv появляются два журнала (в них только дописывается):
    external.log - вызовы БКИ и СМЭВ, без персональных данных
    audit.log    - события аудита, одна json-строка на событие
"""
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUT_FIELDS = ["app_id", "decision", "reason", "score", "dti"]

# пороги вынесены наверх, чтобы не искать их по коду (см. 02_правила.md)
MIN_AGE, MAX_AGE = 18, 70
MIN_INCOME = 30000          # меньше этого дохода сразу отказ
MIN_SCORE = 60              # ниже этого балла отказ
MAX_DTI = 0.5               # платёж не больше половины дохода
MAX_AMOUNT_MULT = 10        # сумма не больше 10 доходов
BKI_MIN_SCORE = 500         # ниже этого балла БКИ отказ BKI_DELINQUENCY
AMOUNT_RANGE = (10000, 5000000)
TERM_RANGE = (3, 60)
ANNUAL_RATE = 0.18          # ставка prod_standard, по ней везде считаем dti

EMPLOYMENT = ("employed", "self-employed", "student", "retired", "unemployed")
FAMILY = ("single", "married", "children")

EXTERNAL_TIMEOUT_SEC = 3    # сколько ждём ответ внешнего сервиса
RETRY_PAUSE_SEC = 0.5       # пауза перед единственным повтором


# журналы: external.log и audit.log
class Journals:
    """два журнала, в файлы только дописываем. персональных данных нет,
    паспорт в логах только в виде хеша"""

    def __init__(self, directory):
        self.external_path = Path(directory) / "external.log"
        self.audit_path = Path(directory) / "audit.log"
        self.req_no = 0
        self.evt_no = 0

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def external(self, service, status, latency_ms, result):
        self.req_no += 1
        line = {"timestamp": self._now(), "service": service,
                "request_id": "req_%04d" % self.req_no, "status": status,
                "latency_ms": latency_ms, "result": result}
        with open(self.external_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def audit(self, app_id, action, decision="", reason="", score="", dti=""):
        self.evt_no += 1
        line = {"event_id": "evt_%04d" % self.evt_no, "app_id": app_id,
                "action": action, "decision": decision, "reason": reason,
                "score": score, "dti": dti, "actor": "system", "at": self._now()}
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


# эмуляторы внешних сервисов, без random, чтобы результат повторялся
def _h10(text):
    """md5 от строки, берём остаток от деления на 10. встроенный hash() не
    подходит: он меняется при каждом запуске"""
    return int(hashlib.md5(text.encode()).hexdigest(), 16) % 10


def bki_emulator(passport_hash):
    """TODO: 0..5 - score 600+, 6..8 - score 450+, 9 - delinquency. (сделано)
    раскладка как в 06_external_services.md: 0..5 это 600+h*20 без просрочки,
    6..8 это 450+(h-6)*50 без просрочки, 9 это 400 и просрочка"""
    h = _h10(passport_hash)
    if h <= 5:
        return 600 + h * 20, False
    if h <= 8:
        return 450 + (h - 6) * 50, False
    return 400, True


def smev_emulator(client_id):
    """TODO: MATCH для 9 из 10, MISMATCH для 10-го. (сделано)"""
    return "MISMATCH" if _h10(client_id) == 0 else "MATCH"


def call_external(service, key, emulator, journals, timeout_clients, client_id):
    """вызов сервиса с одним повтором. возвращает ответ эмулятора или None,
    если не ответил. таймаут включаем только для клиентов из --timeout-clients"""
    for attempt in (1, 2):
        started = time.perf_counter()
        if client_id in timeout_clients:
            # по-настоящему ждать 3 секунды не нужно, просто пишем в лог
            journals.external(service, "TIMEOUT", EXTERNAL_TIMEOUT_SEC * 1000,
                              {"attempt": attempt})
            if attempt == 1:
                time.sleep(RETRY_PAUSE_SEC)
            continue
        result = emulator(key)
        latency = int((time.perf_counter() - started) * 1000)
        if service == "BKI":
            shown = {"score": result[0], "delinquency": result[1]}
        else:
            shown = {"status": result}
        journals.external(service, "SUCCESS", latency, shown)
        return result
    return None


# валидация
def _to_number(text, cast):
    try:
        return cast(text)
    except (TypeError, ValueError):
        return None


def validate(app):
    """TODO: проверить age, income, employment, family, amount, term. (сделано)
    возвращает (вид, reason, поле) или None, если всё в порядке.
    вид MISSING: поле пустое, reason будет MISSING_<ПОЛЕ>.
    вид INVALID: значение неверное, reason берём из data_dictionary.md.
    в обоих случаях решение NEED_MORE_DATA, клиент должен поправить заявку.
    возраст вне 18-70 здесь не проверяем, это правило отказа №1 в decide().
    порядок важен: у app_0004 пустые и age, и income, а нужен MISSING_INCOME,
    поэтому доход проверяем первым"""
    for field in ("income", "age", "employment", "family", "amount", "term"):
        if not (app.get(field) or "").strip():
            return "MISSING", "MISSING_" + field.upper(), field

    if _to_number(app["income"], float) is None or float(app["income"]) <= 0:
        return "INVALID", "MISSING_INCOME", "income"
    if _to_number(app["age"], int) is None:
        return "INVALID", "MISSING_AGE", "age"
    if app["employment"] not in EMPLOYMENT:
        return "INVALID", "INVALID_EMPLOYMENT", "employment"
    if app["family"] not in FAMILY:
        return "INVALID", "INVALID_FAMILY", "family"
    amount = _to_number(app["amount"], float)
    if amount is None or not AMOUNT_RANGE[0] <= amount <= AMOUNT_RANGE[1]:
        return "INVALID", "AMOUNT_OUT_OF_RANGE", "amount"
    term = _to_number(app["term"], int)
    if term is None or not TERM_RANGE[0] <= term <= TERM_RANGE[1]:
        return "INVALID", "TERM_OUT_OF_RANGE", "term"
    if not re.fullmatch(r"cli_[0-9]{4}", app.get("client_id") or ""):
        return "INVALID", "INVALID_CLIENT_ID", "client_id"
    if not re.fullmatch(r"[a-f0-9]{16}", app.get("passport_hash") or ""):
        return "INVALID", "INVALID_PASSPORT_HASH", "passport_hash"
    return None


# скоринг, веса из 02_правила.md
def score(app, bki_score, delinquency):
    """TODO: считать score по весам. (сделано)"""
    income = float(app["income"])
    age = int(app["age"])

    s_income = 30 if income >= 150000 else 20 if income >= 80000 else 10 if income >= 40000 else 0
    s_emp = {"employed": 25, "self-employed": 20, "retired": 10,
             "student": 5, "unemployed": 0}[app["employment"]]
    # TODO: возраст, семья, БКИ (сделано)
    if 25 <= age <= 50:
        s_age = 20
    elif 18 <= age <= 24 or 51 <= age <= 60:
        s_age = 10
    elif 61 <= age <= 70:
        s_age = 5
    else:
        s_age = 0
    s_family = {"married": 15, "children": 10, "single": 10}[app["family"]]
    if delinquency or bki_score < 500:
        s_bki = -10
    elif bki_score >= 700:
        s_bki = 10
    elif bki_score >= 600:
        s_bki = 5
    else:
        s_bki = 0

    # ниже нуля балл не опускается
    return max(0, s_income + s_emp + s_age + s_family + s_bki)


def compute_dti(amount, term, income):
    """платёж по аннуитету делим на доход. не округляем, округление только
    при выводе: иначе 0.5004 станет 0.50 и пройдёт порог"""
    mr = ANNUAL_RATE / 12
    payment = amount * mr / (1 - (1 + mr) ** (-term))
    return payment / income if income else None


# решение: идём по правилам сверху вниз, первое сработавшее и есть итог
def decide(app, score_total, dti, bki_score, delinquency, smev):
    """TODO: правила отказа. (сделано, порядок как в 02_правила.md)"""
    age = int(app["age"])
    income = float(app["income"])
    amount = float(app["amount"])

    if age < MIN_AGE or age > MAX_AGE:                      # 1
        return "REFUSED", "AGE_OUT_OF_RANGE"
    if app["employment"] == "unemployed":                   # 2
        return "REFUSED", "INVALID_EMPLOYMENT"
    # 3: пустой income ловит validate() (NEED_MORE_DATA, MISSING_INCOME)
    if income < MIN_INCOME:                                 # 4
        return "REFUSED", "LOW_INCOME"
    if dti is not None and dti > MAX_DTI:                   # 5
        return "REFUSED", "HIGH_DTI"
    if delinquency or bki_score < BKI_MIN_SCORE:            # 6
        return "REFUSED", "BKI_DELINQUENCY"
    if amount > MAX_AMOUNT_MULT * income:                   # 7
        return "REFUSED", "AMOUNT_LIMIT"
    if smev == "MISMATCH":                                  # 8, дополнительное
        return "REFUSED", "SMEV_MISMATCH"
    if score_total < MIN_SCORE:                             # 9, дополнительное
        return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""                                   # 10, всё прошло


def _result(app_id, decision, reason, score_total="", dti=""):
    return {"app_id": app_id, "decision": decision, "reason": reason,
            "score": score_total, "dti": dti}


def process(app, journals, timeout_clients, use_csv=False):
    app_id = app["app_id"]
    journals.audit(app_id, "application_created")

    # сначала валидация, чтобы не ходить во внешние сервисы с мусором
    problem = validate(app)
    if problem:
        _, reason, _ = problem
        journals.audit(app_id, "validation_failed", "NEED_MORE_DATA", reason)
        return _result(app_id, "NEED_MORE_DATA", reason)

    if use_csv:
        # режим для сверки: берём ответы из csv, никуда не обращаемся
        smev = app.get("smev_status") or smev_emulator(app["client_id"])
        csv_bki = _to_number(app.get("bki_score"), int)
        if csv_bki is None:
            bki_score, delinquency = bki_emulator(app["passport_hash"])
        else:
            bki_score, delinquency = csv_bki, csv_bki < BKI_MIN_SCORE
    else:
        # шаг 2: СМЭВ
        smev = call_external("SMEV", app["client_id"], smev_emulator, journals,
                             timeout_clients, app["client_id"])
        if smev is None:
            journals.audit(app_id, "decision_made", "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
            return _result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")

        # шаг 3: БКИ
        bki = call_external("BKI", app["passport_hash"], bki_emulator, journals,
                            timeout_clients, app["client_id"])
        if bki is None:
            journals.audit(app_id, "decision_made", "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
            return _result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
        bki_score, delinquency = bki

    # шаги 4-6: скоринг, dti, правила отказа
    score_total = score(app, bki_score, delinquency)
    dti = compute_dti(float(app["amount"]), int(app["term"]), float(app["income"]))
    decision, reason = decide(app, score_total, dti, bki_score, delinquency, smev)

    dti_out = round(dti, 2) if dti is not None else ""
    journals.audit(app_id, "decision_made", decision, reason, score_total, dti_out)
    return _result(app_id, decision, reason, score_total, dti_out)


def main():
    if len(sys.argv) < 2:
        print("Использование: python 04_прототип.py applications.csv "
              "[--out decisions.csv] [--timeout-clients cli_0001,cli_0002] "
              "[--show-diff] [--use-csv-external]")
        sys.exit(1)

    src = sys.argv[1]
    out = "decisions.csv"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    timeout_clients = set()
    if "--timeout-clients" in sys.argv:
        timeout_clients = set(sys.argv[sys.argv.index("--timeout-clients") + 1].split(","))
    show_diff = "--show-diff" in sys.argv
    use_csv = "--use-csv-external" in sys.argv

    # utf-8-sig, чтобы не спотыкаться о BOM, если файл сохраняли из excel
    with open(src, encoding="utf-8-sig", newline="") as f:
        apps = list(csv.DictReader(f))

    journals = Journals(Path(out).resolve().parent)
    results = [process(a, journals, timeout_clients, use_csv) for a in apps]

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        for r in results:
            w.writerow(r)

    stats = {}
    for r in results:
        stats[r["decision"]] = stats.get(r["decision"], 0) + 1
    print(f"OK: {out}")
    print(f"    обработано: {len(results)}")
    print(f"    решения:    {stats}")

    # сверка с разметкой в csv (колонки decision и reason)
    if apps and "decision" in apps[0]:
        diff = [(a, r) for a, r in zip(apps, results)
                if (a["decision"], a["reason"]) != (r["decision"], r["reason"])]
        same = len(apps) - len(diff)
        print(f"    совпало с разметкой CSV: {same}/{len(apps)}")
        if show_diff:
            for a, r in diff:
                print(f"      {a['app_id']}: CSV={a['decision']}/{a['reason'] or '-'}"
                      f"  прототип={r['decision']}/{r['reason'] or '-'}")


if __name__ == "__main__":
    main()