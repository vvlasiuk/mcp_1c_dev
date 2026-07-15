#!/usr/bin/env python3
# server.py — MCP-сервер "mcp_1c_dev" (розробка запитів 1С).
# Читання структури/запитів + ЗБЕРЕЖЕННЯ запиту. БЕЗ виконання запитів (run_query),
# без доступу до бойових даних, без серверного AI (генерацію робить Claude у діалозі).
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

import os
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

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
    try:
        resp = httpx.post(url, json=payload, headers=_headers(), timeout=60)
        if resp.status_code == 401:
            _login()
            resp = httpx.post(url, json=payload, headers=_headers(), timeout=60)
    except httpx.RequestError as exc:
        raise RuntimeError(f"vps_api недоступний: {exc}")

    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"vps_api HTTP {resp.status_code}: {detail}")

    return resp.json()


def _get(path: str, params: dict = None) -> dict:
    """GET-версія _call для читальних cf_module ендпойнтів (query-параметри)."""
    url = f"{API_URL}{path}"
    try:
        resp = httpx.get(url, params=params or {}, headers=_headers(), timeout=60)
        if resp.status_code == 401:
            _login()
            resp = httpx.get(url, params=params or {}, headers=_headers(), timeout=60)
    except httpx.RequestError as exc:
        raise RuntimeError(f"vps_api недоступний: {exc}")

    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"vps_api HTTP {resp.status_code}: {detail}")

    return resp.json()

# ═══ ЧИТАННЯ (контекст для генерації) ═══

@mcp.tool(annotations=_RO)
def list_objects() -> dict:
    """Список об'єктів конфігурації 1С (довідники + документи).
    Повертає {total, objects:[{type, name, synonym}]}."""
    return _call("/1c/metadata_objects", {})


@mcp.tool(annotations=_RO)
def describe_object(object_type: str, object_name: str) -> dict:
    """Опис об'єкта 1С: реквізити (з типами) + табличні частини.
    object_type: "Справочник" | "Документ"; object_name: ім'я об'єкта.
    Повертає {type, name, synonym, attributes[], tabular_sections[]}."""
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