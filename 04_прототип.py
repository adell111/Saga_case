#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI-прототип кредитного конвейера - кейс 2.

Использование:
    python 04_прототип.py                               диалог с вопросами
    python 04_прототип.py Данные/application.csv
    python 04_прототип.py Данные/application.csv --out decisions.csv
    python 04_прототип.py Данные/application.csv --timeout-clients cli_0003,cli_0010
    python 04_прототип.py Данные/application.csv --show-diff
    python 04_прототип.py Данные/application.csv --use-csv-external

--use-csv-external берёт ответы БКИ и СМЭВ из колонок bki_score и smev_status
самого csv (если там пусто или мусор, то из эмулятора). Нужно для сверки с
разметкой. Обычный режим - эмуляторы.

Шаги заявки: валидация, СМЭВ, БКИ, скоринг, DTI, правила отказа, решение,
аудит. Настоящих БКИ и СМЭВ нет, вместо них эмуляторы на md5 (см.
06_external_services.md). Только стандартная библиотека.

Рядом с decisions.csv появляются два журнала (в них только дописывается):
    external.log - вызовы БКИ и СМЭВ, без персональных данных
    audit.log    - события аудита, одна json-строка на событие

Файл делится на две части: сверху core (логика решения), потом стена из #,
ниже cli (консольная оболочка: диалог и запуск).
"""
import csv
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ----------------------------------------------------------------------------
# core
# логика: параметры, журналы, эмуляторы бки и смэв, валидация, скоринг, dti,
# правила отказа, process(). с человеком здесь не общаемся.
# ----------------------------------------------------------------------------

OUT_FIELDS = ["app_id", "decision", "reason", "score", "dti", "client_text",
              "amount_approved", "term_approved", "rate"]

# --- пороги (см. 02_правила.md) ---------------------------------------------
MIN_SCORE = 60              # ниже этого балла отказ LOW_SCORE
MAX_DTI = 0.5               # платёж не больше половины дохода
MAX_AMOUNT_MULT = 10        # сумма не больше 10 доходов
BKI_MIN_SCORE = 500         # ниже этого балла БКИ отказ BKI_DELINQUENCY
BKI_RANGE = (300, 850)      # допустимый bki_score, см. data_dictionary.md
BKI_INQUIRIES_90D = 1       # эмулятор БКИ всегда отдаёт 1, на решение не влияет
AMOUNT_BOUNDS = (10000, 5000000)   # общие границы заявки (шаг валидации)
TERM_BOUNDS = (3, 60)

EXTERNAL_TIMEOUT_SEC = 3    # сколько ждём ответ внешнего сервиса
RETRY_PAUSE_SEC = 0.5       # пауза перед единственным повтором
SMEV_STATUSES = ("MATCH", "MISMATCH", "TIMEOUT")

# --- веса скоринга (см. 02_правила.md) --------------------------------------
INCOME_POINTS = ((150000, 30), (80000, 20), (40000, 10))   # (от, баллы), иначе 0
EMPLOYMENT_POINTS = {"employed": 25, "self-employed": 20, "retired": 10,
                     "student": 5, "unemployed": 0}
FAMILY_POINTS = {"married": 15, "children": 10, "single": 10}
BKI_POINTS = ((700, 10), (600, 5))                         # иначе 0
BKI_PENALTY = -10           # просрочка или bki_score < BKI_MIN_SCORE

EMPLOYMENT = tuple(EMPLOYMENT_POINTS)
FAMILY = tuple(FAMILY_POINTS)

# пустое поле даёт код этого же поля из data_dictionary.md, порядок важен:
# у пустых полей доход идёт раньше возраста (app_0004, тест T2-3)
EMPTY_FIELD_CODES = (("income", "MISSING_INCOME"), ("age", "MISSING_AGE"),
                     ("employment", "INVALID_EMPLOYMENT"),
                     ("family", "INVALID_FAMILY"),
                     ("amount", "AMOUNT_OUT_OF_RANGE"),
                     ("term", "TERM_OUT_OF_RANGE"))
# колонки, без которых заявку не разобрать (остальные нужны только для сверки)
REQUIRED_COLUMNS = ("app_id", "age", "income", "employment", "family", "amount",
                    "term", "client_id", "passport_hash")

# тексты для клиента по коду reason. коды с пометкой * в data_dictionary.md
# (MISSING_AGE, INVALID_FAMILY, AMOUNT_OUT_OF_RANGE, TERM_OUT_OF_RANGE,
# INVALID_CLIENT_ID, INVALID_PASSPORT_HASH) бывают только от валидатора
CLIENT_TEXTS = {
    "AGE_OUT_OF_RANGE": "Возраст не соответствует условиям продукта",
    "INVALID_EMPLOYMENT": "Продукт не доступен при текущем статусе занятости",
    "LOW_INCOME": "Недостаточный доход для данного продукта",
    "HIGH_DTI": "Слишком высокая долговая нагрузка",
    "BKI_DELINQUENCY": "Есть просрочки в кредитной истории",
    "AMOUNT_LIMIT": "Сумма кредита превышает допустимую для вашего дохода",
    "SMEV_MISMATCH": "Данные не совпали при проверке, обратитесь в банк",
    "LOW_SCORE": "Недостаточный балл для одобрения",
    "MISSING_INCOME": "Укажите доход",
    "EXTERNAL_TIMEOUT": "Не удалось проверить данные, попробуйте позже",
    "MISSING_AGE": "Укажите корректный возраст",
    "INVALID_FAMILY": "Проверьте семейное положение",
    "AMOUNT_OUT_OF_RANGE": "Проверьте сумму кредита",
    "TERM_OUT_OF_RANGE": "Проверьте срок кредита",
    "INVALID_CLIENT_ID": "Не удалось проверить идентификатор клиента",
    "INVALID_PASSPORT_HASH": "Не удалось проверить данные документа",
}

PRODUCT_ID = "prod_standard"
# файл продуктов ищем рядом со скриптом, потом в текущей папке. организаторы
# присылали его и как products.csv, и как product.csv
PRODUCTS_PATH = Path(__file__).resolve().parent / "Данные" / "products.csv"
PRODUCTS_CANDIDATES = [base / "Данные" / name
                       for base in (Path(__file__).resolve().parent, Path.cwd())
                       for name in ("products.csv", "product.csv")]


class DataError(Exception):
    """неверный входной файл или параметры. текст показываем человеку"""


@dataclass(frozen=True)
class Product:
    """параметры продукта из products.csv"""
    min_age: int
    max_age: int
    min_income: float
    min_amount: float
    max_amount: float
    min_term: int
    max_term: int
    rate: float                 # годовая ставка, по ней везде считаем dti


@dataclass
class Application:
    """проверенная заявка: все поля уже нужного типа"""
    app_id: str
    age: int
    income: float
    employment: str
    family: str
    amount: float
    term: int
    client_id: str
    passport_hash: str


@dataclass
class External:
    """ответы внешних сервисов. calls идёт в audit.log"""
    smev: str = ""
    bki_score: int = 0
    delinquency: bool = False
    calls: list = field(default_factory=list)
    timed_out: bool = False


@dataclass
class Run:
    """всё, что нужно одному прогону"""
    product: Product
    journals: "Journals"
    timeout_clients: set = field(default_factory=set)
    use_csv: bool = False


# --- чтение файлов ----------------------------------------------------------
def _clean_name(text):
    """имя колонки или id без обратных слэшей и пробелов. файлы, выгруженные
    через markdown, приходят как product\\_id, min\\_amount"""
    return (text or "").replace("\\", "").strip()


def read_rows(path):
    """читает csv (utf-8, BOM можно). возвращает (имена колонок, строки).
    любая проблема с файлом это DataError с понятным текстом"""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            names = [_clean_name(n) for n in reader.fieldnames or []]
            if names:
                reader.fieldnames = names
            return names, list(reader)
    except UnicodeDecodeError:
        raise DataError("файл '%s' не в кодировке UTF-8, пересохраните как UTF-8" % path)
    except (OSError, csv.Error) as e:
        raise DataError("не удалось прочитать '%s' (%s)" % (path, e))


def load_product(path=PRODUCTS_PATH, product_id=PRODUCT_ID):
    """параметры продукта из products.csv"""
    if not Path(path).is_file() and Path(path) == PRODUCTS_PATH:
        path = next((c for c in PRODUCTS_CANDIDATES if c.is_file()), path)
    _, rows = read_rows(path)
    row = next((r for r in rows if _clean_name(r.get("product_id")) == product_id), None)
    if row is None:
        raise DataError("продукт %s не найден в %s" % (product_id, path))
    try:
        return Product(min_age=int(row["min_age"]), max_age=int(row["max_age"]),
                       min_income=float(row["min_income"]),
                       min_amount=float(row["min_amount"]),
                       max_amount=float(row["max_amount"]),
                       min_term=int(row["min_term"]), max_term=int(row["max_term"]),
                       rate=float(row["rate"]))
    except (KeyError, TypeError, ValueError) as e:
        raise DataError("неверные параметры продукта %s в %s (%s)"
                        % (product_id, path, e))


# --- журналы: external.log и audit.log --------------------------------------
class Journals:
    """два журнала, в файлы только дописываем. персональных данных нет,
    паспорт в логах только в виде хеша"""

    def __init__(self, directory):
        self.external_path = Path(directory) / "external.log"
        self.audit_path = Path(directory) / "audit.log"
        # продолжаем нумерацию с последнего id в логах, чтобы между
        # запусками не было повторов
        self.req_no = self._last_no(self.external_path, "request_id", "req")
        self.evt_no = self._last_no(self.audit_path, "event_id", "evt")

    @staticmethod
    def _last_no(path, key, prefix):
        """последний номер id в логе, 0 если файла нет или он пустой"""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return 0
        found = re.findall(r'"%s":\s*"%s_(\d+)"' % (key, prefix), text)
        return max(map(int, found)) if found else 0

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _append(path, line):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def external(self, app_id, service, status, latency_ms, result):
        self.req_no += 1
        self._append(self.external_path, {
            "timestamp": self._now(), "service": service,
            "request_id": "req_%04d" % self.req_no, "app_id": app_id,
            "status": status, "latency_ms": latency_ms, "result": result})

    def audit(self, app_id, step, decision="", reason="", score="", dti="",
              external=None, client_id=""):
        """поля как в ИБ.md, раздел 4. client_id это псевдоним клиента, он нужен,
        чтобы видеть повторные заявки. external - след вызовов БКИ и СМЭВ вида
        {"service": "SMEV", "status": "SUCCESS", "result": "MATCH"}, пустой
        список, если во внешние сервисы не ходили. decision пустой только у
        application_created, score и dti пустые, пока скоринг не считали"""
        self.evt_no += 1
        self._append(self.audit_path, {
            "event_id": "evt_%04d" % self.evt_no, "app_id": app_id,
            "client_id": client_id, "step": step, "decision": decision,
            "reason": reason, "score": score, "dti": dti,
            "external": external or [], "actor": "system", "at": self._now()})


# --- эмуляторы внешних сервисов (без random, результат повторяется) ---------
def _h10(text):
    """md5 от строки, остаток от деления на 10. встроенный hash() не подходит:
    он меняется при каждом запуске"""
    return int(hashlib.md5(text.encode()).hexdigest(), 16) % 10


def bki_emulator(passport_hash):
    """раскладка как в 06_external_services.md. возвращает (score, delinquency).
    h 0..5: 600+h*20, h 6..8: 450+(h-6)*50, h 9: 400 и просрочка"""
    h = _h10(passport_hash)
    if h <= 5:
        return 600 + h * 20, False
    if h <= 8:
        return 450 + (h - 6) * 50, False
    return 400, True


def smev_emulator(client_id):
    """MATCH для 9 из 10 client_id, MISMATCH для 10-го"""
    return "MISMATCH" if _h10(client_id) == 0 else "MATCH"


def call_external(run, app, service, key, emulator):
    """вызов сервиса с одним повтором. возвращает ответ эмулятора или None,
    если не ответил. таймаут включаем только для клиентов из --timeout-clients"""
    for attempt in (1, 2):
        started = time.perf_counter()
        if app.client_id in run.timeout_clients:
            # по-настоящему ждать 3 секунды не нужно, просто пишем в лог
            run.journals.external(app.app_id, service, "TIMEOUT",
                                  EXTERNAL_TIMEOUT_SEC * 1000, {"attempt": attempt})
            if attempt == 1:
                time.sleep(RETRY_PAUSE_SEC)
            continue
        result = emulator(key)
        if service == "BKI":
            shown = {"score": result[0], "delinquency": result[1],
                     "inquiries_90d": BKI_INQUIRIES_90D}
        else:
            shown = {"status": result}
        latency = int((time.perf_counter() - started) * 1000)
        run.journals.external(app.app_id, service, "SUCCESS", latency, shown)
        return result
    return None


def _trace(service, status, result=""):
    """запись о вызове для audit.log: service BKI или SMEV, status SUCCESS,
    TIMEOUT или ERROR (а не MATCH и MISMATCH), result это краткий ответ без ПДн"""
    return {"service": service, "status": status, "result": result}


def fetch_external(run, app, row):
    """шаги 2 и 3: СМЭВ и БКИ. в режиме CSV ответы берём из колонок файла и
    никуда не обращаемся, пустое или мусорное значение заменяет эмулятор"""
    if run.use_csv:
        smev = row.get("smev_status")
        if smev not in SMEV_STATUSES:
            smev = smev_emulator(app.client_id)
        if smev == "TIMEOUT":
            return External(smev=smev, timed_out=True)
        bki = _to_number(row.get("bki_score"), int)
        if bki is None or not BKI_RANGE[0] <= bki <= BKI_RANGE[1]:
            bki_score, delinquency = bki_emulator(app.passport_hash)
        else:
            bki_score, delinquency = bki, bki < BKI_MIN_SCORE
        return External(smev, bki_score, delinquency)

    ext = External()
    smev = call_external(run, app, "SMEV", app.client_id, smev_emulator)
    if smev is None:
        ext.calls.append(_trace("SMEV", "TIMEOUT"))
        ext.timed_out = True
        return ext
    ext.smev = smev
    ext.calls.append(_trace("SMEV", "SUCCESS", smev))
    bki = call_external(run, app, "BKI", app.passport_hash, bki_emulator)
    if bki is None:
        ext.calls.append(_trace("BKI", "TIMEOUT"))
        ext.timed_out = True
        return ext
    ext.bki_score, ext.delinquency = bki
    ext.calls.append(_trace("BKI", "SUCCESS", "score=%d, delinquency=%s" % (
        ext.bki_score, "true" if ext.delinquency else "false")))
    return ext


# --- валидация --------------------------------------------------------------
def _to_number(text, cast):
    """число из csv или None. nan и inf не пропускаем: с ними все сравнения
    ложны и заявка прошла бы все пороги. для int принимаем 32.0 как 32"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if cast is int:
        return int(value) if value == int(value) else None
    return value


def normalize(raw):
    """копия строки без пробелов по краям, enum и хеш в нижнем регистре,
    статус СМЭВ в верхнем. пустое значение остаётся пустым. так лишний
    пробел или Employed не превращаются в отказ по формату"""
    row = {k: v.strip() for k, v in raw.items()
           if k is not None and isinstance(v, str)}     # лишние колонки не нужны
    for name in ("employment", "family", "passport_hash"):
        row[name] = row.get(name, "").lower()
    row["smev_status"] = row.get("smev_status", "").upper()
    return row


def validate(row):
    """проверяет строку. возвращает (Application, None) или (None, reason).
    любая проблема это NEED_MORE_DATA, клиент должен поправить заявку.
    диапазон возраста 18-70 и лимиты продукта здесь не проверяем: это правила
    отказа в decide(). здесь только общие границы заявки"""
    age = _to_number(row.get("age"), int)
    if row.get("age") and age is None:         # заполнен, но не целое число
        return None, "MISSING_AGE"
    for name, code in EMPTY_FIELD_CODES:
        if not row.get(name):
            return None, code

    income = _to_number(row["income"], float)
    if income is None or income <= 0:
        return None, "MISSING_INCOME"
    if row["employment"] not in EMPLOYMENT:
        return None, "INVALID_EMPLOYMENT"
    if row["family"] not in FAMILY:
        return None, "INVALID_FAMILY"
    amount = _to_number(row["amount"], float)
    if amount is None or not AMOUNT_BOUNDS[0] <= amount <= AMOUNT_BOUNDS[1]:
        return None, "AMOUNT_OUT_OF_RANGE"
    term = _to_number(row["term"], int)
    if term is None or not TERM_BOUNDS[0] <= term <= TERM_BOUNDS[1]:
        return None, "TERM_OUT_OF_RANGE"
    if not re.fullmatch(r"cli_[0-9]{4}", row.get("client_id", "")):
        return None, "INVALID_CLIENT_ID"
    if not re.fullmatch(r"[a-f0-9]{16}", row["passport_hash"]):
        return None, "INVALID_PASSPORT_HASH"
    return Application(row.get("app_id", ""), age, income, row["employment"],
                       row["family"], amount, term, row["client_id"],
                       row["passport_hash"]), None


# --- скоринг ----------------------------------------------------------------
def _points(value, steps):
    """баллы по лестнице (от, баллы), сверху вниз. ниже всех ступеней 0"""
    for threshold, points in steps:
        if value >= threshold:
            return points
    return 0


def score(app, bki_score, delinquency):
    """сумма баллов по признакам, ниже нуля не бывает"""
    if 25 <= app.age <= 50:
        s_age = 20
    elif 18 <= app.age <= 24 or 51 <= app.age <= 60:
        s_age = 10
    elif 61 <= app.age <= 70:
        s_age = 5
    else:
        s_age = 0
    if delinquency or bki_score < BKI_MIN_SCORE:
        s_bki = BKI_PENALTY
    else:
        s_bki = _points(bki_score, BKI_POINTS)
    total = (_points(app.income, INCOME_POINTS) + EMPLOYMENT_POINTS[app.employment]
             + s_age + FAMILY_POINTS[app.family] + s_bki)
    return max(0, total)


def compute_dti(app, rate):
    """платёж по аннуитету делим на доход. не округляем: округление только
    при выводе, иначе 0.5004 станет 0.50 и пройдёт порог"""
    mr = rate / 12
    payment = app.amount * mr / (1 - (1 + mr) ** (-app.term))
    return payment / app.income


# --- решение ----------------------------------------------------------------
def decide(app, product, score_total, dti, ext):
    """правила отказа сверху вниз, первое сработавшее и есть итог.
    порядок и номера как в 02_правила.md. возвращает (decision, reason)"""
    if not product.min_age <= app.age <= product.max_age:               # 1
        return "REFUSED", "AGE_OUT_OF_RANGE"
    if app.employment == "unemployed":                                  # 2
        return "REFUSED", "INVALID_EMPLOYMENT"
    # 3: пустой income ловит validate() (NEED_MORE_DATA, MISSING_INCOME)
    if app.income < product.min_income:                                 # 4
        return "REFUSED", "LOW_INCOME"
    if dti > MAX_DTI:                                                   # 5
        return "REFUSED", "HIGH_DTI"
    if ext.delinquency or ext.bki_score < BKI_MIN_SCORE:                # 6
        return "REFUSED", "BKI_DELINQUENCY"
    if app.amount > MAX_AMOUNT_MULT * app.income:                       # 7
        return "REFUSED", "AMOUNT_LIMIT"
    # потолок суммы продукта (products.csv) тоже даёт AMOUNT_LIMIT, но не раньше
    # правил 5 и 6, чтобы порядок из 02_правила.md не нарушался
    if not product.min_amount <= app.amount <= product.max_amount:
        return "REFUSED", "AMOUNT_LIMIT"
    if ext.smev == "MISMATCH":                                          # 8
        return "REFUSED", "SMEV_MISMATCH"
    if score_total < MIN_SCORE:                                         # 9
        return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""                                               # 10


def _num(value):
    """число без лишнего .0: 500000, а не 500000.0"""
    return int(value) if value == int(value) else value


def _result(app_id, decision, reason, score_total="", dti="", approved=None):
    """строка decisions.csv. amount_approved, term_approved и rate заполняем
    только для APPROVED, у него же reason и текст для клиента пустые"""
    row = {"app_id": app_id, "decision": decision, "reason": reason,
           "score": score_total, "dti": dti,
           "client_text": CLIENT_TEXTS.get(reason, ""),
           "amount_approved": "", "term_approved": "", "rate": ""}
    row.update(approved or {})
    return row


def process(raw, run):
    """одна заявка от строки csv до решения"""
    row = normalize(raw)
    app_id = row.get("app_id") or "app_unknown"
    journals = run.journals
    client_id = row.get("client_id", "")
    if not CLIENT_RE.fullmatch(client_id):
        client_id = ""      # мусор вместо псевдонима в журнал не пишем
    journals.audit(app_id, "application_created", client_id=client_id)

    # сначала валидация, чтобы не ходить во внешние сервисы с мусором
    app, reason = validate(row)
    if reason:
        journals.audit(app_id, "validation_failed", "NEED_MORE_DATA", reason,
                       client_id=client_id)
        return _result(app_id, "NEED_MORE_DATA", reason)

    ext = fetch_external(run, app, row)
    if ext.timed_out:
        journals.audit(app_id, "decision_made", "NEED_MORE_DATA", "EXTERNAL_TIMEOUT",
                       external=ext.calls, client_id=client_id)
        return _result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")

    score_total = score(app, ext.bki_score, ext.delinquency)
    dti = compute_dti(app, run.product.rate)
    decision, reason = decide(app, run.product, score_total, dti, ext)

    dti_out = round(dti, 2)
    journals.audit(app_id, "decision_made", decision, reason, score_total, dti_out,
                   external=ext.calls, client_id=client_id)
    approved = None
    if decision == "APPROVED":
        approved = {"amount_approved": _num(app.amount), "term_approved": app.term,
                    "rate": run.product.rate}
    return _result(app_id, decision, reason, score_total, dti_out, approved)


##############################################################################
##############################################################################
##############################################################################
# cli
# консольная оболочка: диалог, разбор аргументов, запуск прогона, вывод.
# правила решения здесь не меняются, тут только вызывается core.
##############################################################################

BANNER = r"""
$$$$$$\   $$$$$$\   $$$$$$\   $$$$$$\
$$  __$$\ $$  __$$\ $$  __$$\ $$  __$$\
$$ /  \__|$$ /  $$ |$$ /  \__|$$ /  $$ |
\$$$$$$\  $$$$$$$$ |$$ |$$$$\ $$$$$$$$ |
 \____$$\ $$  __$$ |$$ |\_$$ |$$  __$$ |
$$\   $$ |$$ |  $$ |$$ |  $$ |$$ |  $$ |
\$$$$$$  |$$ |  $$ |\$$$$$$  |$$ |  $$ |
 \______/ \__|  \__| \______/ \__|  \__|
"""

QUIT_WORDS = ("q", "quit", "exit", "выход", "й")
YES_WORDS = ("y", "yes", "д", "да", "1")
NO_WORDS = ("n", "no", "н", "нет", "0")
NONE_WORDS = ("", "-", "нет", "no", "n", "0")
BAD_FILE_CHARS = '<>"|?*'
CLIENT_RE = re.compile(r"cli_[0-9]{4}")

USAGE = """Использование:
    python 04_прототип.py                       диалог с вопросами
    python 04_прототип.py ЗАЯВКИ.csv [параметры]

Параметры:
    --out ФАЙЛ                куда писать решения (по умолчанию decisions.csv)
    --use-csv-external        брать ответы БКИ и СМЭВ из csv (режим сверки)
    --timeout-clients cli_0003,cli_0010
                              имитировать таймаут внешних сервисов для клиентов
    --show-diff               показать расхождения с разметкой CSV
    -h, --help                эта справка
"""


class InputCancelled(Exception):
    """ввод закончился (EOF, Ctrl+C) или человек ввёл q"""


# --- вопросы ----------------------------------------------------------------
def _ask(text, default):
    """спрашивает значение, пустой Enter оставляет значение в скобках.
    q в любом вопросе, Ctrl+C и конец ввода - выход без ошибок"""
    try:
        value = input("%s [%s]: " % (text, default)).strip()
    except (EOFError, KeyboardInterrupt):
        raise InputCancelled()
    if value.lower() in QUIT_WORDS:
        raise InputCancelled()
    return value or default


def _ask_valid(text, default, check):
    """спрашивает, пока check(ответ) не вернёт (результат, None).
    ошибку check печатаем и спрашиваем снова"""
    while True:
        result, error = check(_ask(text, default))
        if not error:
            return result
        print(error)


def _ask_yes_no(text, default):
    def check(value):
        if value.lower() in YES_WORDS:
            return True, None
        if value.lower() in NO_WORDS:
            return False, None
        return None, "Не понял. Введите y (да) или n (нет), либо q для выхода."
    return _ask_valid(text, default, check)


# --- проверки ввода ---------------------------------------------------------
def _clean_path(text):
    """убирает кавычки и пробелы (путь часто перетаскивают в окно с кавычками),
    обратные слэши Windows меняет на прямые, если иначе файл не найти"""
    text = (text or "").strip().strip("\"'").strip()
    path = Path(text).expanduser()
    if not path.exists() and "\\" in text:
        alt = Path(text.replace("\\", "/")).expanduser()
        if alt.exists():
            return alt
    return path


def _csv_hint():
    """подсказка: какие csv лежат рядом, чтобы не гадать с путём"""
    found = []
    for folder in (Path(__file__).resolve().parent / "Данные", Path.cwd() / "Данные",
                   Path.cwd()):
        try:
            for f in sorted(folder.glob("*.csv")):
                if f.name.lower() not in ("decisions.csv", "products.csv", "product.csv"):
                    found.append(str(f))
        except OSError:
            pass
    return "Подсказка, csv рядом: " + ", ".join(dict.fromkeys(found)) if found else ""


def _default_src():
    folder = Path("Данные")
    for name in ("applications.csv", "application.csv"):
        if (folder / name).is_file():
            return (folder / name).as_posix()
    return "Данные/application.csv"


def _src_error(src):
    """проверяет файл заявок до запуска: есть ли, читается ли, те ли колонки"""
    path = Path(src)
    if not str(src).strip():
        return "Ошибка: путь пустой."
    if not path.exists():
        return "Ошибка: файл '%s' не найден. Проверьте путь и повторите ввод." % src
    if path.is_dir():
        return "Ошибка: '%s' это папка, нужен файл .csv." % src
    try:
        names, _ = read_rows(path)
    except DataError as e:
        return "Ошибка: %s." % e
    if not names:
        return "Ошибка: файл '%s' пустой." % src
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        return ("Ошибка: в '%s' нет колонок: %s. Это точно файл заявок?"
                % (src, ", ".join(missing)))
    return None


def _out_error(out, src=None):
    """проверяет до обработки, что результат и журналы можно записать"""
    if not str(out).strip():
        return "Ошибка: имя файла пустое."
    if any(ch in str(out) for ch in BAD_FILE_CHARS):
        return "Ошибка: в имени файла нельзя символы %s" % BAD_FILE_CHARS
    try:
        path = Path(out).resolve()
        if path.is_dir():
            return "Ошибка: '%s' это каталог, укажите имя файла." % out
        if src is not None and path == Path(src).resolve():
            return "Ошибка: результат не должен совпадать с файлом заявок, он бы его затёр."
        with tempfile.TemporaryFile(dir=path.parent):
            pass
    except (OSError, ValueError):
        return ("Ошибка: каталог для '%s' не существует или недоступен для записи. "
                "Повторите ввод." % out)
    return None


def _parse_timeout(text):
    """список client_id из строки. возвращает (множество, список неверных)"""
    items = [t for t in re.split(r"[,; ]+", (text or "").strip()) if t]
    good = {t for t in items if CLIENT_RE.fullmatch(t)}
    bad = [t for t in items if not CLIENT_RE.fullmatch(t)]
    return good, bad


# --- диалог и аргументы -----------------------------------------------------
def interactive():
    """диалог запуска. на любой неверный ввод объясняет и спрашивает снова"""
    print("Кредитный конвейер. Выход: q в любой момент или Ctrl+C.")

    def check_src(value):
        path = _clean_path(value)
        error = _src_error(path)
        if error:
            hint = _csv_hint()
            return None, error + ("\n" + hint if hint else "")
        return path, None

    def check_mode(value):
        if value in ("1", "2"):
            return value, None
        return None, "Не понял '%s'. Нужно ввести 1 или 2." % value

    def check_timeouts(value):
        if value.lower() in NONE_WORDS:
            return set(), None
        good, bad = _parse_timeout(value)
        if bad:
            return None, "Не понял: %s. Нужен вид cli_0003, через запятую." % ", ".join(bad)
        return good, None

    src = _ask_valid("Путь к заявкам", _default_src(), check_src)
    print("Режим: 1 - обычный (эмуляторы), 2 - сверка с разметкой CSV")
    mode = _ask_valid("Режим", "1", check_mode)
    timeout_clients = set()
    if mode == "1":
        timeout_clients = _ask_valid(
            "Клиенты для имитации таймаута, например cli_0003,cli_0010 "
            "(Enter - без таймаутов)", "нет", check_timeouts)

    def check_out(value):
        path = _clean_path(value)
        error = _out_error(path, src)
        return (None, error) if error else (path, None)

    out = _ask_valid("Куда писать результат", "decisions.csv", check_out)
    show_diff = _ask_yes_no("Показать расхождения с CSV (y/n)", "n")
    return str(src), str(out), timeout_clients, show_diff, mode == "2"


def parse_args(argv):
    """разбор аргументов. возвращает (параметры, None) или (None, текст ошибки)"""
    src, out, show_diff, use_csv = None, "decisions.csv", False, False
    timeout_text = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--out", "--timeout-clients"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                return None, "после %s нужно значение" % arg
            if arg == "--out":
                out = argv[i + 1]
            else:
                timeout_text = argv[i + 1]
            i += 2
            continue
        if arg == "--show-diff":
            show_diff = True
        elif arg == "--use-csv-external":
            use_csv = True
        elif arg.startswith("-") and len(arg) > 1:
            return None, "неизвестный параметр '%s'" % arg
        elif src is None:
            src = arg
        else:
            return None, "лишний аргумент '%s'" % arg
        i += 1
    if src is None:
        return None, "не указан файл заявок"
    timeout_clients, bad = _parse_timeout(timeout_text)
    if bad:
        return None, "неверные client_id в --timeout-clients: %s" % ", ".join(bad)
    return (src, out, timeout_clients, show_diff, use_csv), None


# --- прогон -----------------------------------------------------------------
def run_pipeline(src, out, timeout_clients, show_diff, use_csv):
    """весь прогон. возвращает True, если результат записан. любую ошибку
    печатает по-человечески и возвращает False, не падая"""
    try:
        product = load_product()
        _, apps = read_rows(src)
        if not apps:
            print("Ошибка: в файле нет заявок, только заголовок.")
            return False
        run = Run(product, Journals(Path(out).resolve().parent),
                  timeout_clients, use_csv)
        pairs, skipped = [], []
        for number, raw in enumerate(apps, 2):          # 1-я строка файла это заголовок
            try:
                pairs.append((raw, process(raw, run)))
            except Exception as e:      # строка с неожиданным мусором не валит прогон
                skipped.append("строка %d (%s): %s" % (number, raw.get("app_id"), e))
        if not pairs:
            print("Ошибка: ни одну заявку обработать не удалось.")
            return False
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            w.writeheader()
            for _, result in pairs:
                w.writerow(result)
    except DataError as e:
        print("Ошибка: %s." % e)
        return False
    except PermissionError:
        print("Ошибка: нет доступа к файлу. Если результат открыт в Excel, закройте его.")
        return False
    except (OSError, csv.Error, ValueError) as e:
        print("Ошибка: не удалось выполнить прогон (%s)." % e)
        return False

    stats = {}
    for _, result in pairs:
        stats[result["decision"]] = stats.get(result["decision"], 0) + 1
    print(f"OK: {out}")
    print(f"    обработано: {len(pairs)}")
    print(f"    решения:    {stats}")
    for line in skipped:
        print("    пропущено:  " + line)

    # сверка с разметкой в csv (колонки decision и reason)
    if "decision" in pairs[0][0]:
        diff = [(raw, result) for raw, result in pairs
                if ((raw.get("decision") or "").strip(), (raw.get("reason") or "").strip())
                != (result["decision"], result["reason"])]
        print(f"    совпало с разметкой CSV: {len(pairs) - len(diff)}/{len(pairs)}")
        if show_diff:
            for raw, result in diff:
                print(f"      {raw.get('app_id')}: CSV={raw.get('decision')}/{raw.get('reason') or '-'}"
                      f"  прототип={result['decision']}/{result['reason'] or '-'}")
    return True


def _setup_console():
    """русский текст не должен ронять консоль со старой кодировкой"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main():
    _setup_console()
    print(f"{'#'*50}\n" + "\n".join(ln.center(50) for ln in BANNER.strip().split("\n")) + f"\n{'#'*50}\n")
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0

    if not args:
        while True:
            try:
                params = interactive()
            except InputCancelled:
                print("\nВыход.")
                return 0
            ok = run_pipeline(*params)
            try:
                again = _ask_yes_no("Запустить ещё раз (y/n)", "n" if ok else "y")
            except InputCancelled:
                again = False
            if not again:
                print("Выход.")
                return 0 if ok else 1

    params, error = parse_args(args)
    if error:
        print("Ошибка: " + error + "\n\n" + USAGE)
        return 2
    src, out = params[0], params[1]
    error = _src_error(src) or _out_error(out, src)
    if error:
        print(error)
        return 2
    return 0 if run_pipeline(*params) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
    except Exception as e:      # последняя страховка: без трейсбека для пользователя
        print("Непредвиденная ошибка: %s: %s" % (type(e).__name__, e))
        sys.exit(1)
