#!/usr/bin/env python3
# server.py — MCP-сервер "mcp_1c_dev" (розробка запитів 1С).
# Читання структури/запитів + ЗБЕРЕЖЕННЯ запиту + ЗАПИС довідників і документів
# (save_cat / save_doc).
# БЕЗ run_query: MCP-канал (Claude) не читає бойові дані довільними запитами —
# читання/запис бізнес-даних це роль веб-інтерфейсів vps_api, не MCP. Виняток —
# свідомо дозволений запис довідників і документів через контрольовані
# /1c/save_cat та /1c/save_doc. Серверного AI немає (генерацію робить Claude
# у діалозі).
#
# Робочий цикл у Claude Desktop:
#   describe_object → Claude сам складає .sel/.json → save_query.
#
# Залежності: pip install mcp httpx
# Конфіг (claude_desktop_config.json → "env"):
#   VPS_API_URL, VPS_USERNAME, VPS_PASSWORD
#
# Анотації інструментів (ToolAnnotations) керують групуванням у діалозі дозволів
# Claude Desktop: readOnlyHint=True → група «Read-only» (можна дозволити гуртом
# одним кліком); readOnlyHint=False + destructiveHint=True → група «Write/delete»
# (залишається під окремим підтвердженням). Це НЕ обмежує самі інструменти —
# лише впливає на UI дозволів клієнта.

import json as _json
import os
import time as _time
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from telemetry import record as _tel_record

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_URL = os.getenv("VPS_API_URL", "").rstrip("/")
USERNAME = os.getenv("VPS_USERNAME", "")
PASSWORD = os.getenv("VPS_PASSWORD", "")

mcp = FastMCP("mcp_1c_dev")

# Класи дозволів для UI Claude Desktop:
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False)   # читання (нічого не змінює)
_WR = ToolAnnotations(readOnlyHint=False, destructiveHint=True)   # запис/перезапис файлів
_WA = ToolAnnotations(readOnlyHint=False, destructiveHint=False)  # додавання (лог/бекап)

_token = {"value": None}


def _login():
    if not API_URL:
        raise RuntimeError("Не задано VPS_API_URL у конфігу MCP")
    if not USERNAME or not PASSWORD:
        raise RuntimeError("Не задано VPS_USERNAME / VPS_PASSWORD у конфігу MCP")
    resp = httpx.post(
        f"{API_URL}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Логін у vps_api не вдався: HTTP {resp.status_code} {resp.text[:200]}")
    tok = resp.json().get("token")
    if not tok:
        raise RuntimeError("vps_api не повернув token при логіні")
    _token["value"] = tok
    return tok


def _headers():
    if not _token["value"]:
        _login()
    return {
        "Authorization": "Bearer " + _token["value"],
        "Content-Type": "application/json",
    }


def _call(path: str, payload: dict) -> dict:
    url = f"{API_URL}{path}"
    _req_b = len(_json.dumps(payload, ensure_ascii=False).encode())
    _t0 = _time.perf_counter()
    _resp = None
    try:
        try:
            _resp = httpx.post(url, json=payload, headers=_headers(), timeout=60)
            if _resp.status_code == 401:
                _login()
                _resp = httpx.post(url, json=payload, headers=_headers(), timeout=60)
        except httpx.RequestError as exc:
            raise RuntimeError(f"vps_api недоступний: {exc}")

        if _resp.status_code != 200:
            detail = _resp.text[:300]
            try:
                detail = _resp.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"vps_api HTTP {_resp.status_code}: {detail}")

        result = _resp.json()
        _tel_record(path, "POST", _req_b, len(_resp.content),
                    round((_time.perf_counter() - _t0) * 1000, 1), True)
        return result
    except Exception as exc:
        _res_b = len(_resp.content) if _resp is not None else 0
        _tel_record(path, "POST", _req_b, _res_b,
                    round((_time.perf_counter() - _t0) * 1000, 1), False, str(exc)[:200])
        raise


def _get(path: str, params: dict = None) -> dict:
    """GET-версія _call для читальних cf_module ендпойнтів (query-параметри)."""
    url = f"{API_URL}{path}"
    _params = params or {}
    _req_b = len(_json.dumps(_params, ensure_ascii=False).encode())
    _t0 = _time.perf_counter()
    _resp = None
    try:
        try:
            _resp = httpx.get(url, params=_params, headers=_headers(), timeout=60)
            if _resp.status_code == 401:
                _login()
                _resp = httpx.get(url, params=_params, headers=_headers(), timeout=60)
        except httpx.RequestError as exc:
            raise RuntimeError(f"vps_api недоступний: {exc}")

        if _resp.status_code != 200:
            detail = _resp.text[:300]
            try:
                detail = _resp.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"vps_api HTTP {_resp.status_code}: {detail}")

        result = _resp.json()
        _tel_record(path, "GET", _req_b, len(_resp.content),
                    round((_time.perf_counter() - _t0) * 1000, 1), True)
        return result
    except Exception as exc:
        _res_b = len(_resp.content) if _resp is not None else 0
        _tel_record(path, "GET", _req_b, _res_b,
                    round((_time.perf_counter() - _t0) * 1000, 1), False, str(exc)[:200])
        raise


# ═══ ФІЛЬТРАЦІЯ МЕТАДАНИХ (клієнтська, для list_objects) ═══
# 1С віддає всі ~1250 об'єктів одним масивом (~170К символів). Фільтруємо тут,
# у Python: BSL не чіпаємо, а в контекст моделі потрапляє лише потрібний зріз.
# HTTP-трафік між server.py і 1С у токени не рахується — економія саме на
# тому, що повертається інструментом.

_CANON_TYPES = ("Справочник", "Документ", "РегістрВідомостей",
                "РегістрНакопичення", "РегістрБухгалтерії", "Перелічення")

# Орфографічна нормалізація рос↔укр: конфігурація історично змішана
# (746 імен з ы/э/ё/ъ, 95 з і/ї/є/ґ). Зводимо до спільного вигляду, щоб
# запит «регіон» знаходив довідник «Регионы», а «інструмент» — «Инструмента».
_NORM_TBL = str.maketrans({
    "і": "и", "ї": "и", "ы": "и", "є": "е", "э": "е", "ё": "е",
    "ґ": "г", "ъ": "", "ь": "", "'": "", "\u2019": "", "`": "",
})


def _norm(s: str) -> str:
    return (s or "").lower().translate(_NORM_TBL)


_TYPE_ALIASES = {}


def _reg_type(canon: str, *aliases):
    for a in (canon,) + aliases:
        _TYPE_ALIASES[_norm(a).replace(" ", "").replace("_", "")] = canon


_reg_type("Справочник", "справочники", "довідник", "довідники",
          "catalog", "catalogs", "спр")
_reg_type("Документ", "документи", "документы", "document", "documents", "док")
_reg_type("РегістрВідомостей", "регистрсведений", "регистрысведений",
          "регістривідомостей", "регістр відомостей",
          "inforegister", "informationregister", "рс")
_reg_type("РегістрНакопичення", "регистрнакопления", "регистрынакопления",
          "регістринакопичення", "регістр накопичення",
          "accumulationregister", "рн")
_reg_type("РегістрБухгалтерії", "регистрбухгалтерии", "регистрыбухгалтерии",
          "регістрибухгалтерії", "регістр бухгалтерії",
          "accountingregister", "рб")
_reg_type("Перелічення", "переліки", "перечисление", "перечисления",
          "enum", "enums", "пер")


def _as_list(v) -> list:
    """Толерантний ввід: приймаємо і рядок, і масив, і None."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x) for x in v if str(x).strip()]


def _resolve_types(object_type):
    """Аліас → канонічний тип 1С. Повертає (типи, нерозпізнані)."""
    out, bad = [], []
    for raw in _as_list(object_type):
        key = _norm(raw).replace(" ", "").replace("_", "")
        canon = _TYPE_ALIASES.get(key)
        if canon:
            if canon not in out:
                out.append(canon)
        else:
            bad.append(raw)
    return out, bad


def _match_score(obj: dict, nterms: list) -> int:
    """Релевантність збігу: 3 — точний, 2 — префікс імені, 1 — в імені,
    0 — лише в синонімі, -1 — не збіг. Потрібна, щоб при обрізанні по limit
    відкидались найменш релевантні, а не випадкові."""
    n = _norm(obj.get("name", ""))
    s = _norm(obj.get("synonym", ""))
    best = -1
    for t in nterms:
        if n == t or s == t:
            best = max(best, 3)
        elif n.startswith(t):
            best = max(best, 2)
        elif t in n:
            best = max(best, 1)
        elif t in s:
            best = max(best, 0)
    return best


def _filter_objects(all_objs, object_type=None, name_contains=None,
                    limit: int = 0, offset: int = 0) -> dict:
    """Три режими відповіді за рівнем конкретики запиту (див. docstring інструмента)."""
    types, bad = _resolve_types(object_type)
    if bad:
        return {"error": "Невідомий object_type: " + ", ".join(bad),
                "allowed": list(_CANON_TYPES)}

    nterms = [t for t in (_norm(x) for x in _as_list(name_contains)) if t]
    pool = [o for o in all_objs if not types or o.get("type") in types]

    # ── Режим "counts": без жодного фільтра → лише масштаб по типах.
    if not types and not nterms:
        counts = {}
        for o in all_objs:
            counts[o.get("type")] = counts.get(o.get("type"), 0) + 1
        return {"mode": "counts", "total": len(all_objs), "counts": counts,
                "hint": "Задай object_type (масив) та/або name_contains "
                        "(масив термінів, OR) для деталізації."}

    # ── Режим "search": є терміни → повна структура, за спаданням релевантності.
    if nterms:
        scored = []
        for o in pool:
            sc = _match_score(o, nterms)
            if sc >= 0:
                scored.append((sc, o))
        scored.sort(key=lambda p: (-p[0], p[1].get("name", "")))
        total = len(scored)
        lim = limit if limit > 0 else 50
        window = [o for _, o in scored[offset:offset + lim]]
        res = {"mode": "search", "total": total, "returned": len(window),
               "offset": offset, "truncated": offset + len(window) < total,
               "objects": window}
        if total == 0:
            res["hint"] = ("Збігів немає. Спробуй інший корінь слова або "
                           "рос./укр. відповідник (напр. 'упаковк' замість 'пакунк').")
        return res

    # ── Режим "names": лише типи → компактні імена, згруповані за типом.
    lim = limit if limit > 0 else 500
    grouped, taken, total = {}, 0, len(pool)
    for t in types:
        if taken >= lim:
            break
        names = sorted(o.get("name", "") for o in pool if o.get("type") == t)
        grouped[t] = names[:lim - taken]
        taken += len(grouped[t])
    res = {"mode": "names", "total": total, "returned": taken,
           "truncated": taken < total, "objects": grouped}
    if res["truncated"]:
        res["hint"] = ("Обрізано захисним лімітом. Звузь object_type або "
                       "додай name_contains.")
    return res


# ═══ ЧИТАННЯ (контекст для генерації) ═══

@mcp.tool(annotations=_RO)
def list_objects(object_type: list = None, name_contains: list = None,
                 limit: int = 0, offset: int = 0) -> dict:
    """Об'єкти конфігурації 1С: довідники, документи, регістри (відомостей,
    накопичення, бухгалтерії) та переліки — усього ~1250.

    Форма відповіді залежить від конкретики запиту (щоб не тягнути зайве):

    1) БЕЗ параметрів → {mode:"counts", total, counts:{тип: кількість}} —
       лише масштаб по типах, ~270 символів. Використовуй для орієнтації,
       далі звужуй.
    2) Лише object_type → {mode:"names", total, returned, truncated,
       objects:{тип:[імена]}} — компактний список імен без синонімів.
    3) Є name_contains → {mode:"search", total, returned, offset, truncated,
       objects:[{type, name, synonym}]} — повна структура лише для збігів,
       відсортована за релевантністю (точний збіг → префікс імені → входження
       в ім'я → лише в синонімі), тож обрізання по limit прибирає найменш
       доречне, а не випадкове.

    object_type: масив типів (OR). Приймає рос./укр./англ. написання та
        скорочення — "Справочник", "довідники", "catalogs", "спр" → Справочник;
        "РегистрСведений", "регістр відомостей", "рс" → РегістрВідомостей;
        "enum", "перечисления" → Перелічення. Нерозпізнаний тип → {error, allowed}.
    name_contains: масив підрядків (OR), шукає в name І synonym одночасно,
        без урахування регістру та рос↔укр орфографії (і/ї/ы→и, є/э/ё→е,
        ґ→г, ъ/ь→∅). Тому "регіон" знаходить довідник "Регионы".
        ВАЖЛИВО: нормалізація лікує різне НАПИСАННЯ, але не різні СЛОВА —
        "пакунк" не знайде "Упаковка". Передавай кілька варіантів одразу:
        ["пакунк","упаковк","відправлен","отправл"].
    limit: 0 → авто (50 для пошуку, 500 для списку імен). offset: для хвоста
        вибірки при truncated=true.

    Обидва фільтри комбінуються (спершу тип, потім терміни)."""
    data = _call("/1c/metadata_objects", {})
    return _filter_objects(data.get("objects", []), object_type,
                           name_contains, limit, offset)


@mcp.tool(annotations=_RO)
def describe_object(object_type: str, object_name: str) -> dict:
    """Опис об'єкта 1С: реквізити (з типами) + табличні частини.
    object_type: "Справочник" | "Документ"; object_name: ім'я об'єкта.
    Повертає {type, name, synonym, attributes[], tabular_sections[]}.
    tabular_sections[]: [{name, synonym, attributes[{name, synonym, types[]}]}] —
    використовуй ці імена ТЧ і реквізитів при формуванні tabular_sections для
    save_cat / save_doc."""
    return _call("/1c/metadata_describe", {"type": object_type, "name": object_name})


@mcp.tool(annotations=_RO)
def list_queries(object_type: str, object_name: str) -> dict:
    """Наявні іменовані запити (.sel/.json), прив'язані до об'єкта 1С.
    Повертає {total, queries:[{query_name, info, file, fields_count, mcp_allowed}]}.
    mcp_allowed — чи дозволено виконувати запит через MCP-канал (керується полем
    mcp_allowed у .json; при save_query можна виставити через meta)."""
    return _call("/metadata/queries", {"object_type": object_type, "object_name": object_name})


@mcp.tool(annotations=_RO)
def get_query(query_name: str) -> dict:
    """Сирий вміст запиту: текст .sel і метадані .json (поля, типи).
    Повертає {query_name, file, sel, meta}."""
    return _call("/metadata/query_get", {"query_name": query_name})


# ═══ ЗАПИС ДАНИХ 1С — ДОВІДНИКИ + ДОКУМЕНТИ (БОЙОВІ ДАНІ) ═══
# УВАГА: ці інструменти пишуть у РЕАЛЬНІ довідники/документи 1С через
# /1c/save_cat та /1c/save_doc. На відміну від решти write-інструментів (які
# чіпають лише артефакти розробки), вони змінюють бойові дані. Тому — анотація
# _WR (окреме підтвердження щоразу).

@mcp.tool(annotations=_WR)
def save_cat(catalog: str, fields: dict, action: str = "write",
             ref: str = "", version: str = "",
             is_folder: bool = False, fields_search: dict = None,
             tabular_sections: dict = None) -> dict:
    """Створити / змінити / позначити на видалення елемент довідника 1С.
    ПИШЕ В БОЙОВІ ДАНІ — використовуй свідомо, звіряй значення перед викликом.

    catalog: ім'я довідника (object_name), напр. "Валюты", "Контрагенты".
    fields: реквізити у форматі {реквізит: {"type": ..., "value": ...}} —
            той самий формат, що й params запиту та fields у save_doc.
            Напр. {"Наименование": {"type": "string", "value": "Долар США"},
                   "Код":         {"type": "string", "value": "840"}}.
    action: "write" (типово) | "mark_delete" | "unmark_delete".
    ref: посилання наявного елемента для ОНОВЛЕННЯ; "" → створити НОВИЙ.
    version: ВерсіяДаних (оптимістичне блокування) — передавай отриману з
             попереднього читання, щоб не затерти чужі зміни; "" → без перевірки.
    is_folder: True → група (ЭтоГруппа); False → елемент.
    fields_search: опційний іменований набір для find-or-create; структуру
                   знає 1С; None → пропустити.
    tabular_sections: табличні частини у форматі
            {ІмяТЧ: [{реквізит: {"type": ..., "value": ...}, ...}, ...]}.
            Імена ТЧ і реквізитів бери з describe_object (tabular_sections[]).
            УВАГА — кожна ТЧ, вказана тут, ПОВНІСТЮ ПЕРЕЗАПИСУЄТЬСЯ (старі
            рядки очищуються, потім додаються нові з масиву). ТЧ, відсутні в
            цьому словнику, не чіпаються. None/пропущено → жодна ТЧ не
            змінюється. Неіснуюча назва ТЧ → помилка 400 від 1С.
    Повертає {ref, code, description, version, is_folder, marked}."""
    payload = {
        "catalog": catalog,
        "ref": ref,
        "version": version,
        "action": action,
        "is_folder": is_folder,
        "fields": fields,
    }
    if fields_search is not None:
        payload["fields_search"] = fields_search
    if tabular_sections is not None:
        payload["tabular_sections"] = tabular_sections
    return _call("/1c/save_cat", payload)


@mcp.tool(annotations=_WR)
def save_doc(document: str, date: str, fields: dict, action: str = "write",
             ref: str = "", version: str = "", fields_search: dict = None,
             tabular_sections: dict = None) -> dict:
    """Створити / провести / скасувати проведення / позначити на видалення документ 1С.
    ПИШЕ В БОЙОВІ ДАНІ — використовуй свідомо, звіряй значення перед викликом.

    document: ім'я документа (object_name), напр. "ПриемНаСервис".
    date: дата документа в ISO (передається ЗАВЖДИ), напр. "2026-07-22T10:30:00".
    fields: реквізити у форматі {реквізит: {"type": ..., "value": ...}} —
            той самий формат, що й params запиту та fields у save_cat.
    action: "write" (типово) | "post" | "unpost" | "mark_delete".
    ref: посилання наявного документа для ОНОВЛЕННЯ; "" → створити НОВИЙ.
    version: ВерсіяДаних (оптимістичне блокування) — з попереднього читання;
             "" → без перевірки.
    fields_search: опційний іменований набір для find-or-create; структуру
                   знає 1С; None → пропустити.
    tabular_sections: табличні частини у форматі
            {ІмяТЧ: [{реквізит: {"type": ..., "value": ...}, ...}, ...]}.
            Імена ТЧ і реквізитів бери з describe_object (tabular_sections[]).
            УВАГА — кожна ТЧ, вказана тут, ПОВНІСТЮ ПЕРЕЗАПИСУЄТЬСЯ (старі
            рядки очищуються, потім додаються нові з масиву). ТЧ, відсутні в
            цьому словнику, не чіпаються. None/пропущено → жодна ТЧ не
            змінюється. Неіснуюча назва ТЧ → помилка 400 від 1С.
    УВАГА: реквізит "Ответственный" vps_api проставляє за обліковкою MCP —
    тобто документ буде за акаунтом MCP, а не за реальним оператором.
    Повертає {ref, number, date, version, posted, marked}."""
    payload = {
        "document": document,
        "ref": ref,
        "version": version,
        "date": date,
        "action": action,
        "fields": fields,
    }
    if fields_search is not None:
        payload["fields_search"] = fields_search
    if tabular_sections is not None:
        payload["tabular_sections"] = tabular_sections
    return _call("/1c/save_doc", payload)


# ═══ ЗАПИС (розробка) ═══

@mcp.tool(annotations=_RO)
def generate_query(object_type: str, object_name: str) -> dict:
    """Механічна чернетка запиту з опису об'єкта (БЕЗ запису на диск, без AI).
    Дає надійну болванку: системні поля _* згори (для довідника 6, для документа 5),
    решта реквізитів з коректним мапінгом типів, псевдонім дов/док, source_name.
    Використовуй ЯК ОСНОВУ: візьми цю болванку, прибери зайві поля, задай осмислені
    аліаси й query_name під завдання — і збережи через save_query.
    object_type: "Справочник" | "Документ"; object_name: ім'я об'єкта.
    Повертає {sel, meta}."""
    return _call("/metadata/generate_query", {"object_type": object_type, "object_name": object_name})


@mcp.tool(annotations=_WR)
def save_query(sel: str, meta: dict, file_name: str = "") -> dict:
    """Зберегти запит (.sel + .json) на диск + гарячий перечит loader (без рестарту).
    sel: текст запиту 1С.
    meta: вміст .json — ДЖЕРЕЛО ПРАВДИ. Обов'язково містить:
          query_name (ASCII-ідентифікатор), object_type ("Справочник"|"Документ"),
          object_name (ім'я об'єкта), info (опис), fields[{key,type,info}].
    file_name: ім'я файлу без розширення; "" → береться з meta.query_name.
               При редагуванні наявного передавай реальне ім'я файлу (з get_query.file),
               щоб перезаписати ТОЙ САМИЙ файл, а не створити дубль.
    Повертає {ok, query_name, path_sel, path_json, total_queries}."""
    return _call("/metadata/save_query", {"file_name": file_name, "sel": sel, "meta": meta})


@mcp.tool(annotations=_WA)
def create_backup(set_name: str = "full_html") -> dict:
    """Створити повний zip-бекап (знімок) набору тек — роби ПЕРЕД блоком змін.
    set_name: псевдонім набору (дефолт "full_html" = queries1c + html + html_command_log).
    Автор знімка визначається за обліковкою MCP (з токена). Заразом чистить
    прострочені тимчасові копії.
    Повертає {ok, set_name, file, archived[], skipped[], temp_removed, warnings[]}."""
    return _call("/backups/create", {"set_name": set_name})


# ═══ ФОРМИ / ФРОНТЕНД (html/) ═══

@mcp.tool(annotations=_RO)
def list_forms() -> dict:
    """Перелік файлів фронтенду (html/) — .html/.css/.js, з усіх підпапок.
    Кожен: {path (відносно html/), ext, writable, size}.
    writable=true → у цей файл дозволено писати (тільки pages/ та menu/).
    Повертає {total, files[]}."""
    return _call("/forms/list", {})


@mcp.tool(annotations=_RO)
def read_form(path: str) -> dict:
    """Прочитати вміст файлу з html/ (читання доступне по всій html/ — для контексту:
    компоненти, стилі, наявні форми). path — відносний, напр. "components/ref_select.js"
    або "pages/admin/users.html".
    Повертає {path, content, writable}."""
    return _call("/forms/read", {"path": path})


@mcp.tool(annotations=_WR)
def write_form(path: str, content: str) -> dict:
    """Записати/перезаписати файл. ЗАПИС ДОЗВОЛЕНО ЛИШЕ в pages/ та menu/
    (lib/, components/, system/ — тільки читання). Перед перезаписом наявного
    робиться тимчасова копія. Підтеки створюються за потреби.
    path: відносний шлях у html/, напр. "pages/nomenclature/list.html".
    content: повний вміст файлу.
    Повертає {ok, path}."""
    return _call("/forms/write", {"path": path, "content": content})


# ═══ ЖУРНАЛ КОМАНД (html_command_log/) ═══

@mcp.tool(annotations=_WA)
def log_command(cmd: str, desc: str, clar: str = "", why: str = "",
                files: list = None) -> dict:
    """Записати команду користувача в журнал (html_command_log/) — ОСТАННІЙ крок
    блоку змін: бекап → зміни → log_command (закриваюча дужка ритуалу).
    Створює один .md-файл: html_command_log/<user>/<дата>/<час>_<desc>.md.
    cmd: суть команди користувача українською (обов'язкове, непорожнє).
    desc: короткий ASCII-ідентифікатор для імені файлу — літери/цифри/дефіс/
          підкреслення, без пробілів і кирилиці (напр. "currency-form-green").
    clar: уточнення з діалогу (колір, код фільтра тощо).
    why: мотив рішення, якщо був озвучений.
    files: перелік зачеплених файлів (шляхи від кореня проекту), напр.
           ["queries1c/catalogs/Валюты/cat_currencies.sel", "html/pages/..."].
    user і час проставляє сервер. Порожній cmd/desc → помилка (нічого не пишемо).
    Повертає {ok, file}."""
    return _call("/command_log", {
        "cmd": cmd, "desc": desc, "clar": clar, "why": why, "files": files or [],
    })


# ═══ КОД КОНФІГУРАЦІЇ (артефакт cf_module) — ЧИТАННЯ ═══
# Читальні зрізи коду конфігурації з SQLite-маніфесту (кістяки, тіла, індекс).
# Не чіпають бойові дані; допомагають орієнтуватися в коді при розробці запитів.

@mcp.tool(annotations=_RO)
def cf_where(name: str, export_only: bool = True) -> dict:
    """Де оголошено процедуру/функцію 1С за ТОЧНИМ іменем.
    export_only=True → лише експортні (публічний API конфігурації).
    Повертає {name, results:[{name, kind, is_export, module_path, sig}]}."""
    return _get(f"/cf_module/where/{quote(name)}",
                {"export_only": "true" if export_only else "false"})


@mcp.tool(annotations=_RO)
def cf_search(prefix: str, export_only: bool = True, limit: int = 50) -> dict:
    """Пошук символів за ПРЕФІКСОМ імені (навігація/автодоповнення).
    Повертає {prefix, results:[{name, kind, is_export, module_path}]}."""
    return _get("/cf_module/search",
                {"prefix": prefix, "export_only": "true" if export_only else "false",
                 "limit": limit})


@mcp.tool(annotations=_RO)
def cf_object_modules(object_type: str, object_name: str) -> dict:
    """Усі модулі об'єкта 1С (модуль об'єкта, менеджера, форм) з ролями.
    object_type: "Справочник" | "Документ" | "РегістрВідомостей" тощо.
    Повертає {type, name, modules:[{module_path, role, proc_count, export_count}]}."""
    return _get("/cf_module/object", {"type": object_type, "name": object_name})


@mcp.tool(annotations=_RO)
def cf_module_toc(module_path: str) -> dict:
    """Зміст модуля: роль + перелік процедур (найдешевший зріз, без коду).
    module_path — шлях модуля з cf_object_modules/cf_where (напр.
    "Catalogs/Контрагенты/Ext/ObjectModule.bsl").
    Повертає {module_path, role, proc_count, procedures:[{name, kind, is_export, significant_lines}]}."""
    return _get("/cf_module/module/toc", {"path": module_path})


@mcp.tool(annotations=_RO)
def cf_skeleton(module_path: str, level: str = "compact") -> dict:
    """Кістяк модуля БЕЗ тіл процедур (економія контексту для god-модулів).
    level="compact" — лише сигнатури; level="full" — з доккоментарями-заголовками.
    Повертає {module, level, text}."""
    return _get("/cf_module/module/skeleton", {"path": module_path, "level": level})


@mcp.tool(annotations=_RO)
def cf_body(module_path: str, name: str) -> dict:
    """Текст ЦІЛОЇ процедури/функції за модулем та іменем (сигнатура..Кінець).
    Повертає {module, name, text}."""
    return _get("/cf_module/body", {"module": module_path, "name": name})


@mcp.tool(annotations=_RO)
def cf_top_modules(limit: int = 20) -> dict:
    """Найбільші модулі за кількістю процедур (орієнтація по god-модулях).
    Повертає {results:[{module_path, role, proc_count, export_count}]}."""
    return _get("/cf_module/modules/top", {"limit": limit})


@mcp.tool(annotations=_RO)
def cf_meta() -> dict:
    """Свіжість артефакту cf_module: коли/з чого згенеровано, лічильники.
    Повертає {generated_at, source_tree, modules, procedures, ...}."""
    return _get("/cf_module/meta", {})


@mcp.tool(annotations=_RO)
def cf_find(query: str, match: str = "word", type: str = "", name: str = "",
            path_prefix: str = "", role: str = "", max_modules: int = 200,
            max_per_module: int = 20, context_lines: int = 0) -> dict:
    """Знайти ВСІ використання імені/тексту в коді конфігурації (тіла процедур
    + рівень модуля). Пошук завжди регістронезалежний (мова 1С така).

    Це пошук ВИКОРИСТАНЬ (де згадується), на відміну від cf_where (де ВИЗНАЧЕНО)
    і cf_search (префікс ІМЕНІ символу).

    match: "word" — по межах ідентифікатора (типово; "Валюты" знайде
           РегистрыСведений.Валюты, але не ВалютыДокумента);
           "contains" — будь-де (уся родина імен); "prefix" — з початку слова.
    Звуження (опційно): type(+name) — тип+ім'я об'єкта 1С (напр. "Справочник"/
           "Валюты"); або path_prefix — префікс шляху; role — роль модуля.
    ТЕКСТОВИЙ пошук: знаходить усі згадки імені; регістр від довідника з тим
    самим іменем модель розрізняє за кваліфікатором у рядку
    (РегистрыСведений.X vs Справочники.X), читаючи text.

    Повертає {query, match, total_modules, total_hits, truncated,
    results:[{module_path, role, hit_count, hits:[{line_no, container,
    is_export, text}]}]}. container=null — рівень модуля; інакше ім'я
    процедури-контейнера. Результати відсортовані за щільністю збігів."""
    params = {"query": query, "match": match, "max_modules": max_modules,
              "max_per_module": max_per_module, "context_lines": context_lines}
    if type:
        params["type"] = type
    if name:
        params["name"] = name
    if path_prefix:
        params["path_prefix"] = path_prefix
    if role:
        params["role"] = role
    return _get("/cf_module/find", params)


if __name__ == "__main__":
    mcp.run()