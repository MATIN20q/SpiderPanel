"""Manage a standalone Cloudflare Worker and its custom domain."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx

CF_API = "https://api.cloudflare.com/client/v4"
WORKER_TEMPLATE = Path(__file__).resolve().parent / "worker" / "worker.js"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class CloudflareWorkerError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.strip()}", "User-Agent": "SpiderPanel"}


def _error(data: dict) -> str:
    errors = data.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "Cloudflare API request failed")
    return str(data.get("message") or "Cloudflare API request failed")


async def _request(method: str, path: str, token: str, *, params: dict | None = None,
                   payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.request(
                method, f"{CF_API}{path}", headers=_headers(token),
                params=params, json=payload,
            )
        except httpx.HTTPError as exc:
            raise CloudflareWorkerError(f"Cloudflare API connection failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("success") is False:
        raise CloudflareWorkerError(f"Cloudflare API ({response.status_code}): {_error(data)}")
    return data


async def list_accounts(token: str) -> list[dict]:
    data = await _request("GET", "/accounts", token, params={"per_page": 50})
    return [
        {"id": str(item.get("id") or ""), "name": str(item.get("name") or item.get("id") or "")}
        for item in data.get("result", []) if item.get("id")
    ]


async def list_zones(token: str, account_id: str) -> list[dict]:
    zones: list[dict] = []
    page = 1
    while page <= 20:
        data = await _request(
            "GET", "/zones", token,
            params={"account.id": account_id, "per_page": 50, "page": page},
        )
        for item in data.get("result", []):
            if item.get("id") and item.get("name"):
                zones.append({"id": str(item["id"]), "name": str(item["name"])})
        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
        page += 1
    return zones


async def deploy(token: str, account_id: str, panel_domain: str) -> dict:
    token = token.strip()
    if not token or len(token) > 4096:
        raise CloudflareWorkerError("Enter a valid Cloudflare API token")
    if not re.fullmatch(r"[a-f0-9]{32}", account_id, re.I):
        raise CloudflareWorkerError("Choose a valid Cloudflare account")
    accounts = await list_accounts(token)
    account = next((item for item in accounts if item["id"] == account_id), None)
    if not account:
        raise CloudflareWorkerError("The selected account is not available to this token")

    worker_name = "spider-domain-" + secrets.token_hex(4)
    kv_title = worker_name + "-db"
    kv_data = await _request(
        "POST", f"/accounts/{account_id}/storage/kv/namespaces", token,
        payload={"title": kv_title},
    )
    kv_id = str((kv_data.get("result") or {}).get("id") or "")
    if not kv_id:
        raise CloudflareWorkerError("Cloudflare did not return a KV namespace ID")

    script_created = False
    try:
        if not WORKER_TEMPLATE.is_file():
            raise CloudflareWorkerError("The worker/worker.js deployment template is missing")
        source = WORKER_TEMPLATE.read_text(encoding="utf-8")
        try:
            account_subdomain_data = await _request("GET", f"/accounts/{account_id}/workers/subdomain", token)
            account_subdomain = str((account_subdomain_data.get("result") or {}).get("subdomain") or "").strip()
        except CloudflareWorkerError:
            # The account may not have a workers.dev subdomain configured yet;
            # custom domains can still route to the deployed Worker.
            account_subdomain = ""
        worker_domain = f"{worker_name}.{account_subdomain}.workers.dev" if account_subdomain else ""
        replacements = {
            "__PANEL_TOKEN__": json.dumps("spider-domain-" + secrets.token_urlsafe(32)),
            "__PANEL_DOMAIN__": json.dumps(panel_domain),
            "__WORKER_DOMAIN__": json.dumps(worker_domain),
        }
        for marker, value in replacements.items():
            source = source.replace(marker, value)
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2026-09-18",
            "bindings": [{"type": "kv_namespace", "name": "SPIDER_KV", "namespace_id": kv_id}],
        }
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            response = await client.put(
                f"{CF_API}/accounts/{account_id}/workers/scripts/{quote(worker_name, safe='')}",
                headers=_headers(token),
                files={
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    "worker.js": ("worker.js", source.encode("utf-8"), "application/javascript+module"),
                },
            )
        try:
            upload_data = response.json()
        except ValueError:
            upload_data = {}
        if response.status_code >= 400 or upload_data.get("success") is False:
            raise CloudflareWorkerError(f"Cloudflare Worker deploy ({response.status_code}): {_error(upload_data)}")
        script_created = True
        if account_subdomain:
            await _request(
                "POST", f"/accounts/{account_id}/workers/scripts/{worker_name}/subdomain", token,
                payload={"enabled": True, "previews_enabled": False},
            )
        return {
            "account_id": account_id,
            "account_name": account["name"],
            "worker_name": worker_name,
            "worker_domain": worker_domain,
            "worker_url": f"https://{worker_domain}" if worker_domain else "",
            "kv_namespace_id": kv_id,
            "kv_namespace_title": kv_title,
            "token": token,
        }
    except Exception:
        # Roll back resources created by this operation if a later deployment
        # step fails; never leave an orphaned script or KV namespace behind.
        if script_created:
            try:
                await _request("DELETE", f"/accounts/{account_id}/workers/scripts/{worker_name}", token)
            except Exception:
                pass
        try:
            await _request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{kv_id}", token)
        except Exception:
            pass
        raise


async def attach_custom_domain(token: str, account_id: str, worker_name: str,
                               zone_id: str, zone_name: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", zone_id, re.I):
        raise CloudflareWorkerError("Choose a valid Cloudflare zone")
    if not re.fullmatch(r"[a-f0-9]{32}", account_id, re.I):
        raise CloudflareWorkerError("Invalid Cloudflare account")
    hostname = f"{secrets.token_hex(4)}.{zone_name}".lower()
    result = await _request(
        "PUT", f"/accounts/{account_id}/workers/domains", token,
        payload={"hostname": hostname, "service": worker_name, "zone_id": zone_id,
                 "enabled": True, "previews_enabled": False},
    )
    domain = result.get("result") or {}
    if not domain.get("id"):
        raise CloudflareWorkerError("Cloudflare did not return the custom domain ID")
    return {"custom_domain": hostname, "custom_domain_id": str(domain["id"]), "zone_id": zone_id,
            "zone_name": zone_name}


async def delete_deployment(state: dict) -> None:
    token = str(state.get("token") or "")
    account_id = str(state.get("account_id") or "")
    worker_name = str(state.get("worker_name") or "")
    if not token or not account_id or not worker_name:
        raise CloudflareWorkerError("No Cloudflare Worker deployment is connected")
    custom_domain_id = str(state.get("custom_domain_id") or "")
    kv_id = str(state.get("kv_namespace_id") or "")
    errors = []
    if custom_domain_id:
        try:
            await _request("DELETE", f"/accounts/{account_id}/workers/domains/{custom_domain_id}", token)
        except CloudflareWorkerError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    # This removes only this script's workers.dev hostname, never the
    # account-wide workers.dev subdomain shared by other scripts.
    try:
        await _request("DELETE", f"/accounts/{account_id}/workers/scripts/{worker_name}/subdomain", token)
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    try:
        await _request("DELETE", f"/accounts/{account_id}/workers/scripts/{worker_name}", token)
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    if kv_id:
        try:
            await _request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{kv_id}", token)
        except CloudflareWorkerError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    if errors:
        raise CloudflareWorkerError("; ".join(errors))
