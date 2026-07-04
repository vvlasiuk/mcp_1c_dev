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

import os

import httpx
from mcp.server.fastmcp import FastMCP

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_URL = os.getenv("VPS_API_URL", "").rstrip("/")
USERNAME = os.getenv("VPS_USERNAME", "")
PASSWORD = os.getenv("VPS_PASSWORD", "")

mcp = FastMCP("mcp_1c_dev")

_token = {"value": None}


def _login():
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


# ═══ ЧИТАННЯ (контекст для генерації) ═══

@mcp.tool()
def list_objects() -> dict:
    """Список об'єктів конфігурації 1С (довідники + документи).
    Повертає {total, objects:[{type, name, synonym}]}."""
    return _call("/1c/metadata_objects", {})


@mcp.tool()
def describe_object(object_type: str, object_name: str) -> dict:
    """Опис об'єкта 1С: реквізити (з типами) + табличні частини.
    object_type: "Справочник" | "Документ"; object_name: ім'я об'єкта.
    Повертає {type, name, synonym, attributes[], tabular_sections[]}."""
    return _call("/1c/metadata_describe", {"type": object_type, "name": object_name})


@mcp.tool()
def list_queries(object_type: str, object_name: str) -> dict:
    """Наявні іменовані запити (.sel/.json), прив'язані до об'єкта 1С.
    Повертає {total, queries:[{query_name, info, file, fields_count}]}."""
    return _call("/metadata/queries", {"object_type": object_type, "object_name": object_name})


@mcp.tool()
def get_query(query_name: str) -> dict:
    """Сирий вміст запиту: текст .sel і метадані .json (поля, типи).
    Повертає {query_name, file, sel, meta}."""
    return _call("/metadata/query_get", {"query_name": query_name})


# ═══ ЗАПИС (розробка) ═══

@mcp.tool()
def generate_query(object_type: str, object_name: str) -> dict:
    """Механічна чернетка запиту з опису об'єкта (БЕЗ запису на диск, без AI).
    Дає надійну болванку: системні поля _* згори (для довідника 6, для документа 5),
    решта реквізитів з коректним мапінгом типів, псевдонім дов/док, source_name.
    Використовуй ЯК ОСНОВУ: візьми цю болванку, прибери зайві поля, задай осмислені
    аліаси й query_name під завдання — і збережи через save_query.
    object_type: "Справочник" | "Документ"; object_name: ім'я об'єкта.
    Повертає {sel, meta}."""
    return _call("/metadata/generate_query", {"object_type": object_type, "object_name": object_name})


@mcp.tool()
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


if __name__ == "__main__":
    mcp.run()