#!/usr/bin/env python3
"""
CLI-прототип кредитного конвейера — кейс 2.

Использование:
    python 04_прототип.py
    python 04_прототип.py Данные/application.csv
    python 04_прототип.py Данные/application.csv --out decisions.csv
    python 04_прототип.py Данные/application.csv --use-csv-external
    python 04_прототип.py Данные/application.csv --timeout-clients cli_0003,cli_0011

Результаты пишутся в decisions.csv, события — в audit.log,
вызовы внешних сервисов — в external.log.
"""
import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUT_FIELDS = ["app_id", "decision", "reason", "score", "dti", "message",
              "amount_approved", "term_approved", "rate"]

TIMEOUT_SECONDS = 3
RETRY_DELAY_SECONDS = 0.5
MIN_SCORE = 60
MAX_DTI = 0.5
MAX_AMOUNT_TO_INCOME = 10
MIN_INCOME = 30000
# Возраст вне этих границ считаем не числом клиента, а ошибкой входных данных.
PLAUSIBLE_AGE_LIMIT = 120

BANNER_WIDTH = 41
TEAM_NAME = "САГА"
PROJECT_NAME = "Кредитный конвейер"
VERSION = "v 0.0.1"

EMPLOYMENT_POINTS = {
    "employed": 25,
    "self-employed": 20,
    "retired": 10,
    "student": 5,
    "unemployed": 0,
}
FAMILY_POINTS = {"married": 15, "children": 10, "single": 10}
EMPLOYMENTS = set(EMPLOYMENT_POINTS)
FAMILIES = set(FAMILY_POINTS)

REASON_MESSAGES = {
    "LOW_INCOME": "Недостаточный доход для данного продукта",
    "AGE_OUT_OF_RANGE": "Возраст не соответствует условиям продукта",
    "INVALID_EMPLOYMENT": "Продукт не доступен при текущем статусе занятости",
    "MISSING_INCOME": "Укажите доход",
    "HIGH_DTI": "Слишком высокая долговая нагрузка",
    "BKI_DELINQUENCY": "Есть просрочки в кредитной истории",
    "SMEV_MISMATCH": "Данные не совпали при проверке, обратитесь в банк",
    "AMOUNT_LIMIT": "Сумма кредита превышает допустимую для вашего дохода",
    "EXTERNAL_TIMEOUT": "Не удалось проверить данные, попробуйте позже",
    "LOW_SCORE": "Недостаточный балл для одобрения",
    "VALIDATION_ERROR": "Недопустимое значение",
    "TERM_OUT_OF_RANGE": "Недопустимый срок кредита",
}


def now_iso():
    """Время события в UTC в формате ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reason_message(reason, field=None):
    """Понятный клиенту текст по машинному коду причины."""
    if not reason:
        return ""
    if reason == "VALIDATION_ERROR" and field:
        return f"Недопустимое значение - {field}"
    return REASON_MESSAGES.get(reason, "Обратитесь в банк")


def find_product_file():
    """Ищем product.csv рядом со скриптом."""
    here = Path(__file__).resolve().parent
    names = ("products.csv", "product.csv")
    candidates = []
    for name in names:
        candidates.append(here / "Данные" / name)
        candidates.append(Path.cwd() / "Данные" / name)
        candidates.append(here / name)
        candidates.append(Path.cwd() / name)
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("не найден Данные/product.csv")


def clean_product_cell(value):
    """В product.csv символы подчёркивания экранированы как \\_."""
    if value is None:
        return ""
    return str(value).replace("\\_", "_").strip()


def load_product(product_id="prod_standard"):
    """Читаем параметры продукта из CSV. Поддерживаем оба имени файла."""
    path = find_product_file()
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [clean_product_cell(name) for name in reader.fieldnames or []]
        for row in reader:
            row = {clean_product_cell(k): clean_product_cell(v) for k, v in row.items() if k}
            if row.get("product_id") != product_id:
                continue
            try:
                return {
                    "product_id": product_id,
                    "min_amount": float(row["min_amount"]),
                    "max_amount": float(row["max_amount"]),
                    "min_term": int(float(row["min_term"])),
                    "max_term": int(float(row["max_term"])),
                    "min_age": int(float(row["min_age"])),
                    "max_age": int(float(row["max_age"])),
                    "min_income": float(row["min_income"]),
                    "rate": float(row["rate"]),
                }
            except (KeyError, ValueError) as exc:
                raise ValueError(f"некорректные параметры продукта {product_id}: {exc}") from exc
    raise ValueError(f"в {path} нет продукта {product_id}")


def bki_emulator(passport_hash):
    """Детерминированный эмулятор БКИ из 06_external_services.md."""
    h = int(hashlib.md5(passport_hash.encode()).hexdigest(), 16) % 10
    if h <= 5:
        score, delinquency = 600 + h * 20, False
    elif h <= 8:
        score, delinquency = 450 + (h - 6) * 50, False
    else:
        score, delinquency = 400, True
    return {"score": score, "delinquency": delinquency, "inquiries_90d": 1}


def smev_emulator(client_id):
    """Детерминированный эмулятор СМЭВ: MISMATCH у каждого десятого клиента."""
    h = int(hashlib.md5(client_id.encode()).hexdigest(), 16) % 10
    return "MISMATCH" if h == 0 else "MATCH"


class Journal:
    """Append-only журналы audit.log и external.log с продолжением нумерации."""

    def __init__(self, audit_path="audit.log", external_path="external.log"):
        self.audit_path = Path(audit_path)
        self.external_path = Path(external_path)
        self.event_no = self._last_number(self.audit_path, "event_id", "evt")
        self.request_no = self._last_number(self.external_path, "request_id", "req")

    @staticmethod
    def _last_number(path, key, prefix):
        """Продолжаем evt_NNNN/req_NNNN с последнего номера в файле."""
        last = 0
        if not path.is_file():
            return last
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    value = str(json.loads(line).get(key, ""))
                except (ValueError, TypeError):
                    continue
                match = re.fullmatch(rf"{prefix}_(\d+)", value)
                if match:
                    last = max(last, int(match.group(1)))
        return last

    @staticmethod
    def _append(path, record):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"WARN: не удалось записать {path}: {exc}", file=sys.stderr)

    def audit(self, app_id, client_id, step, decision="", reason="",
              score="", dti="", external=None):
        self.event_no += 1
        self._append(self.audit_path, {
            "event_id": f"evt_{self.event_no:04d}",
            "app_id": app_id,
            "client_id": client_id,
            "step": step,
            "decision": decision,
            "reason": reason,
            "score": score,
            "dti": dti,
            "external": external or [],
            "actor": "system",
            "at": now_iso(),
        })

    def external(self, service, app_id, status, latency_ms, result):
        self.request_no += 1
        self._append(self.external_path, {
            "timestamp": now_iso(),
            "service": service,
            "app_id": app_id,
            "request_id": f"req_{self.request_no:04d}",
            "status": status,
            "latency_ms": int(latency_ms),
            "result": result,
        })


def normalize_app(app):
    """Приводим строковые поля к единому виду до проверки."""
    app = dict(app)
    for key in ("app_id", "age", "income", "employment", "family", "amount",
                "term", "client_id", "passport_hash", "bki_score", "smev_status"):
        value = app.get(key)
        app[key] = "" if value is None else str(value).strip()
    app["employment"] = app["employment"].lower()
    app["family"] = app["family"].lower()
    app["passport_hash"] = app["passport_hash"].lower()
    for key in ("age", "term"):  # 32.0 считаем целым числом 32
        try:
            number = float(app[key])
        except ValueError:
            continue
        if math.isfinite(number) and number.is_integer():
            app[key] = str(int(number))
    return app


def validate(app):
    """Проверка до внешних вызовов. Возвращает (reason, field) или None."""
    if not re.fullmatch(r"app_[0-9]{4}", app.get("app_id", "")):
        return "VALIDATION_ERROR", "app_id"
    # Возраст проверяется первым, но пустой возраст откладываем: по T2-3
    # при пустом доходе причина должна быть MISSING_INCOME.
    age_missing = False
    age_text = app.get("age", "")
    if age_text == "":
        age_missing = True
    else:
        try:
            age = int(age_text)
        except ValueError:
            return "VALIDATION_ERROR", "age"
        if age < 0 or age > PLAUSIBLE_AGE_LIMIT:
            return "VALIDATION_ERROR", "age"

    income_text = app.get("income", "")
    if income_text == "":
        return "MISSING_INCOME", "income"
    try:
        income = float(income_text)
    except ValueError:
        return "VALIDATION_ERROR", "income"
    if not math.isfinite(income):  # nan/inf — не число (FR-2)
        return "MISSING_INCOME", "income"
    if income < 0:
        return "VALIDATION_ERROR", "income"
    if age_missing:
        return "VALIDATION_ERROR", "age"

    amount_text = app.get("amount", "")
    if amount_text == "":
        return "VALIDATION_ERROR", "amount"
    try:
        amount = float(amount_text)
    except ValueError:
        return "VALIDATION_ERROR", "amount"
    if not math.isfinite(amount):
        return "VALIDATION_ERROR", "amount"
    if amount < 10_000 or amount > 5_000_000:
        return "VALIDATION_ERROR", "amount"

    try:
        term = int(app.get("term", ""))
    except ValueError:
        return "VALIDATION_ERROR", "term"
    if term < 3 or term > 60:
        return "VALIDATION_ERROR", "term"

    if not re.fullmatch(r"[a-f0-9]{16}", app.get("passport_hash", "")):
        return "VALIDATION_ERROR", "passport_hash"

    if app.get("employment") not in EMPLOYMENTS:
        return "VALIDATION_ERROR", "employment"
    if app.get("family") not in FAMILIES:
        return "VALIDATION_ERROR", "family"
    if not re.fullmatch(r"cli_[0-9]{4}", app.get("client_id", "")):
        return "VALIDATION_ERROR", "client_id"
    return None


def score(app, bki_score, delinquency):
    """Баллы по 5 признакам; итог не может быть меньше нуля."""
    income = float(app["income"])
    if income >= 150000:
        s_income = 30
    elif income >= 80000:
        s_income = 20
    elif income >= 40000:
        s_income = 10
    else:
        s_income = 0

    age = int(app["age"])
    if 25 <= age <= 50:
        s_age = 20
    elif 18 <= age <= 24 or 51 <= age <= 60:
        s_age = 10
    elif 61 <= age <= 70:
        s_age = 5
    else:
        s_age = 0

    if delinquency or bki_score < 500:
        s_bki = -10
    elif bki_score >= 700:
        s_bki = 10
    elif bki_score >= 600:
        s_bki = 5
    else:
        s_bki = 0

    total = (s_income + EMPLOYMENT_POINTS[app["employment"]] + s_age
             + FAMILY_POINTS[app["family"]] + s_bki)
    return max(0, total)


def compute_dti(amount, term, income, annual_rate):
    """DTI = аннуитетный платёж / доход (формула из 02_правила.md)."""
    if income <= 0:
        return None
    monthly_rate = annual_rate / 12
    payment = amount * (monthly_rate / (1 - (1 + monthly_rate) ** (-term)))
    return payment / income


def bki_trace(data):
    return {"service": "BKI", "status": "SUCCESS",
            "result": (f"score={data['score']}, delinquency={str(data['delinquency']).lower()}, "
                       f"inquiries_90d={data['inquiries_90d']}")}


def call_bki(app_id, passport_hash, journal):
    """Вызов БКИ с одной повторной попыткой и записью каждой попытки."""
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            data = bki_emulator(passport_hash)
            latency = int((time.monotonic() - started) * 1000)
            journal.external("BKI", app_id, "SUCCESS", latency, data)
            return {"status": "SUCCESS", "data": data, "trace": bki_trace(data)}
        except Exception as exc:  # на случай сбоя эмулятора
            latency = int((time.monotonic() - started) * 1000)
            journal.external("BKI", app_id, "ERROR", latency,
                             {"attempt": attempt, "error": type(exc).__name__})
        if attempt == 1:
            time.sleep(RETRY_DELAY_SECONDS)
    return {"status": "TIMEOUT", "data": None,
            "trace": {"service": "BKI", "status": "TIMEOUT", "result": "TIMEOUT"}}


def call_smev(app_id, client_id, timeout_clients, journal):
    """Вызов СМЭВ; для --timeout-clients имитируем две неудачные попытки."""
    for attempt in (1, 2):
        if client_id in timeout_clients:
            # Эмулятор «не отвечает» дольше 3 секунд; фиксируем это в журнале.
            time.sleep(TIMEOUT_SECONDS)
            journal.external("SMEV", app_id, "TIMEOUT", TIMEOUT_SECONDS * 1000,
                             {"attempt": attempt})
            if attempt == 1:
                time.sleep(RETRY_DELAY_SECONDS)
            continue

        started = time.monotonic()
        try:
            status = smev_emulator(client_id)
            latency = int((time.monotonic() - started) * 1000)
            journal.external("SMEV", app_id, "SUCCESS", latency, {"status": status})
            return {"status": status,
                    "trace": {"service": "SMEV", "status": "SUCCESS", "result": status}}
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            journal.external("SMEV", app_id, "ERROR", latency,
                             {"attempt": attempt, "error": type(exc).__name__})
        if attempt == 1:
            time.sleep(RETRY_DELAY_SECONDS)
    return {"status": "TIMEOUT",
            "trace": {"service": "SMEV", "status": "TIMEOUT", "result": "TIMEOUT"}}


def external_bki(app, use_csv, journal):
    """БКИ из CSV в режиме сверки либо из эмулятора при битом значении."""
    if use_csv:
        raw = app.get("bki_score", "")
        try:
            value = float(raw)
            if not value.is_integer():
                raise ValueError
            value = int(value)
            if 300 <= value <= 850:
                data = {"score": value, "delinquency": value < 500, "inquiries_90d": 1}
                return {"status": "SUCCESS", "data": data, "trace": bki_trace(data)}
        except ValueError:
            pass  # пусто/не число/вне 300-850 — идём в эмулятор
    return call_bki(app["app_id"], app["passport_hash"], journal)


def external_smev(app, use_csv, timeout_clients, journal):
    """СМЭВ из CSV в режиме сверки либо из эмулятора при битом значении."""
    if use_csv:
        status = app.get("smev_status", "").strip().upper()
        if status in {"MATCH", "MISMATCH", "TIMEOUT"}:
            trace = {"service": "SMEV",
                     "status": "SUCCESS" if status != "TIMEOUT" else "TIMEOUT",
                     "result": status}
            return {"status": status, "trace": trace}
    return call_smev(app["app_id"], app["client_id"], timeout_clients, journal)


def decide(app, score_total, dti, bki_score, delinquency, smev, product):
    """Правила отказа строго по приоритету из 02_правила.md."""
    age = int(app["age"])
    income = float(app["income"])
    amount = float(app["amount"])
    term = int(app["term"])

    if age < product["min_age"] or age > product["max_age"]:
        return "REFUSED", "AGE_OUT_OF_RANGE"
    if app["employment"] == "unemployed":
        return "REFUSED", "INVALID_EMPLOYMENT"
    if app.get("income", "") == "":
        return "NEED_MORE_DATA", "MISSING_INCOME"
    if income < max(MIN_INCOME, product["min_income"]):
        return "REFUSED", "LOW_INCOME"
    if dti is not None and dti > MAX_DTI:
        return "REFUSED", "HIGH_DTI"
    if bki_score < 500 or delinquency:
        return "REFUSED", "BKI_DELINQUENCY"

    # Лимиты продукта и правило "сумма не больше 10 доходов".
    if term < product["min_term"] or term > product["max_term"]:
        return "REFUSED", "TERM_OUT_OF_RANGE"
    if (amount < product["min_amount"] or amount > product["max_amount"]
            or amount > MAX_AMOUNT_TO_INCOME * income):
        return "REFUSED", "AMOUNT_LIMIT"

    if smev == "MISMATCH":
        return "REFUSED", "SMEV_MISMATCH"
    if score_total < MIN_SCORE:
        return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""


def make_result(app, decision, reason, score_value="", dti="",
                message="", step="decision_made", external=None, approved=None):
    approved = approved or {}
    return {
        "app_id": app["app_id"],
        "decision": decision,
        "reason": reason,
        "score": score_value,
        "dti": dti,
        "message": message,
        "amount_approved": approved.get("amount", ""),
        "term_approved": approved.get("term", ""),
        "rate": approved.get("rate", ""),
        "_step": step,
        "_external": external or [],
    }


def process(app, product, journal, use_csv, timeout_clients):
    """Полный конвейер одной заявки."""
    error = validate(app)
    if error:
        reason, field = error
        return make_result(
            app, "NEED_MORE_DATA", reason, message=reason_message(reason, field),
            step="validation_failed",
        )

    dti_exact = compute_dti(float(app["amount"]), int(app["term"]),
                            float(app["income"]), product["rate"])
    dti = round(dti_exact, 2) if dti_exact is not None else None
    traces = []

    bki = external_bki(app, use_csv, journal)
    traces.append(bki["trace"])
    if bki["status"] != "SUCCESS":
        return make_result(app, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT", dti=dti,
                           message=reason_message("EXTERNAL_TIMEOUT"),
                           external=traces)

    bki_data = bki["data"]
    score_total = score(app, bki_data["score"], bki_data["delinquency"])

    smev = external_smev(app, use_csv, timeout_clients, journal)
    traces.append(smev["trace"])
    if smev["status"] == "TIMEOUT":
        return make_result(app, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT",
                           score_value=score_total, dti=dti,
                           message=reason_message("EXTERNAL_TIMEOUT"),
                           external=traces)

    decision, reason = decide(app, score_total, dti_exact, bki_data["score"],
                              bki_data["delinquency"], smev["status"], product)
    approved = None
    if decision == "APPROVED":
        amount = float(app["amount"])
        approved = {"amount": int(amount) if amount.is_integer() else amount,
                    "term": int(app["term"]), "rate": product["rate"]}
    return make_result(app, decision, reason, score_value=score_total, dti=dti,
                       message=reason_message(reason), external=traces,
                       approved=approved)


def read_applications(path):
    """Читаем CSV заявок и проверяем наличие обязательных колонок."""
    required = {"app_id", "age", "income", "employment", "family", "amount",
                "term", "client_id", "passport_hash"}
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("пустой CSV или отсутствует строка заголовков")
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError("в CSV нет колонок: " + ", ".join(sorted(missing)))
        apps = []
        for row in reader:
            if not any(str(value).strip() for value in row.values() if value is not None):
                continue  # пропускаем полностью пустые строки
            apps.append(normalize_app(row))
    return apps


def write_decisions(path, results):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerow({field: result[field] for field in OUT_FIELDS})


def print_banner():
    inner = BANNER_WIDTH - 2
    border = "=" * BANNER_WIDTH
    lines = [
        PROJECT_NAME.center(inner),
        f"version: {VERSION}".center(inner),
        f"produced by {TEAM_NAME}".center(inner),
    ]
    print(border, file=sys.stderr)
    for line in lines:
        print("|" + line + "|", file=sys.stderr)
    print(border, file=sys.stderr)


def ask(question, default=""):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip().strip("\"'")
    return answer or default


def ask_yes_no(question, default=False):
    hint = "Д/н" if default else "д/Н"
    while True:
        answer = input(f"{question} ({hint}): ").strip().lower()
        if answer == "":
            return default
        if answer in ("д", "да", "y", "yes"):
            return True
        if answer in ("н", "нет", "n", "no"):
            return False
        print("Введите д или н")


def default_applications_path():
    here = Path(__file__).resolve().parent
    for folder in (here, Path.cwd()):
        path = folder / "Данные" / "application.csv"
        if path.is_file():
            return str(path)
    return ""


def fill_args_interactively(args):
    print("Файл не указан, включён интерактивный режим.")
    print("Enter оставляет значение в квадратных скобках. Выход: Ctrl+C.")
    default_path = default_applications_path()
    while True:
        path = ask("Путь к CSV с заявками", default_path)
        if path and Path(path).is_file():
            break
        print(f"Файл не найден: {path or '(пусто)'}. Попробуйте ещё раз.")
    args.applications = path
    args.out = ask("Куда записать решения (--out)", args.out)
    args.use_csv_external = ask_yes_no("Брать ответы БКИ/СМЭВ из CSV (--use-csv-external)?")
    args.timeout_clients = ask("Клиенты с таймаутом СМЭВ через запятую (--timeout-clients, Enter - никого)")


def main():
    parser = argparse.ArgumentParser(description="Кредитный конвейер — кейс 2")
    parser.add_argument("applications", nargs="?",
                        help="CSV с заявками (если не указан, включится интерактивный режим)")
    parser.add_argument("--out", default="decisions.csv", help="CSV с решениями")
    parser.add_argument("--use-csv-external", action="store_true",
                        help="брать ответы БКИ/СМЭВ из колонок CSV")
    parser.add_argument("--timeout-clients", default="",
                        help="client_id через запятую, для которых СМЭВ отвечает таймаутом")
    args = parser.parse_args()
    print_banner()
    if args.applications is None:
        if not sys.stdin.isatty():
            parser.error("не указан файл с заявками")
        try:
            fill_args_interactively(args)
        except (EOFError, KeyboardInterrupt):
            print("\nВыход", file=sys.stderr)
            return 1

    timeout_clients = {item.strip() for item in args.timeout_clients.split(",") if item.strip()}

    try:
        product = load_product()
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        apps = read_applications(args.applications)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    journal = Journal()
    results = []
    seen = set()
    for app in apps:
        if app["app_id"] in seen:
            print(f"WARN: повтор {app['app_id']}, строка пропущена", file=sys.stderr)
            continue
        seen.add(app["app_id"])
        # В аудит пишем только корректный псевдонимный client_id, не произвольный текст.
        client_id = app.get("client_id", "")
        if not re.fullmatch(r"cli_[0-9]{4}", client_id):
            client_id = ""
        journal.audit(app["app_id"], client_id, "application_created")
        try:
            result = process(app, product, journal, args.use_csv_external, timeout_clients)
        except Exception as exc:  # одна ошибка не останавливает остальные заявки
            print(f"ERROR: {app['app_id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            result = make_result(app, "NEED_MORE_DATA", "VALIDATION_ERROR",
                                 message=reason_message("VALIDATION_ERROR", "application"),
                                 step="validation_failed")
        results.append(result)
        journal.audit(app["app_id"], client_id, result["_step"],
                      decision=result["decision"], reason=result["reason"],
                      score=result["score"], dti=result["dti"],
                      external=result["_external"])

    try:
        write_decisions(args.out, results)
    except OSError as exc:
        print(f"ERROR: не удалось записать {args.out}: {exc}", file=sys.stderr)
        return 1

    stats = {}
    for result in results:
        stats[result["decision"]] = stats.get(result["decision"], 0) + 1
    print(f"OK: {args.out}")
    print(f"    обработано: {len(results)}")
    print(f"    решения:    {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
