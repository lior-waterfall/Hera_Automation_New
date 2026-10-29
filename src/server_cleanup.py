"""Erase load-test users, assets, and server certificates through the Hera API."""

import json
import ssl
import time
import urllib.error
import urllib.request

from tx_cert_upload import delete_tx_certificate


def split_admin_credentials(value):
    credentials = (value or "").strip()
    if ":" not in credentials:
        raise ValueError("ADMIN_CREDENTIALS must be username:password")
    username, password = credentials.split(":", 1)
    if not username or not password:
        raise ValueError("ADMIN_CREDENTIALS must be username:password")
    return username, password


def asset_display_name(settings, counter):
    return f'{settings["asset_name"]}-{counter}'.replace("_", " ")


def find_user(users, email):
    wanted = (email or "").strip().lower()
    for user in users or []:
        stored_email = (user.get("email") or "").strip().lower()
        stored_username = (user.get("username") or "").strip().lower()
        if wanted and wanted in (stored_email, stored_username):
            return user
    return None


def matching_connections(connections, display_name, host):
    wanted_name = (display_name or "").strip()
    wanted_host = (host or "").strip()
    matches = []
    for connection in connections or []:
        name = (connection.get("display_name") or "").strip()
        connection_host = (connection.get("host") or "").strip()
        if name == wanted_name and connection_host == wanted_host and connection.get("id"):
            matches.append(connection)
    return matches


def _candidate_bases(settings):
    if settings.get("api_base"):
        return [settings["api_base"]]
    scheme = (settings.get("api_scheme") or "https").strip().lower()
    prefix = settings.get("api_prefix")
    if prefix is None:
        prefix = "/web-server" if scheme == "https" else ""
    primary = f"{scheme}://{settings['server_ip']}:{settings['api_port']}{prefix}"
    candidates = [primary]
    if scheme == "https" and prefix == "/web-server":
        candidates.append(f"https://{settings['server_ip']}:{settings['api_port']}")
        candidates.append(f"http://{settings['server_ip']}:{settings['api_port']}")
    return candidates


def _request(settings, method, path, token=None, body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    last_error = None
    for base in _candidate_bases(settings):
        request_token = token
        for attempt in range(2):
            request = urllib.request.Request(f"{base}{path}", data=payload, method=method)
            request.add_header("Accept", "application/json")
            if body is not None:
                request.add_header("Content-Type", "application/json")
            if request_token:
                request.add_header("Authorization", f"Bearer {request_token}")
            context = ssl._create_unverified_context() if base.startswith("https://") else None
            try:
                with urllib.request.urlopen(request, context=context, timeout=30) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    settings["api_base"] = base
                    if not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                settings["api_base"] = base
                details = exc.read().decode("utf-8", errors="replace").strip()
                if exc.code == 401 and request_token and attempt == 0:
                    username, password = split_admin_credentials(settings.get("admin_credentials"))
                    request_token = _login(settings, username, password)
                    settings["admin_access_token"] = request_token
                    continue
                raise RuntimeError(f"{method} {path} failed with status {exc.code}: {details}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                break
    raise RuntimeError(f"Could not reach the Hera API: {last_error}")


def _login(settings, username, password):
    response = _request(
        settings,
        "POST",
        "/api/hera-sessions/login",
        body={"username": username, "password": password},
    )
    token = (response or {}).get("access_token")
    if not token:
        raise RuntimeError(f"Login did not return an access token for {username}")
    return token


def erase_web_user_and_asset(settings, email, display_name, asset_host):
    """Delete the matching asset and web user. User deletion also removes the server certificate."""
    admin_username, admin_password = split_admin_credentials(settings.get("admin_credentials"))
    if email.strip().lower() == admin_username.strip().lower():
        raise ValueError(f"Refusing to delete the admin user {admin_username}")

    token = settings.get("admin_access_token")
    if not token:
        token = _login(settings, admin_username, admin_password)
        settings["admin_access_token"] = token

    connections = _request(settings, "GET", "/api/connections", token=token) or []
    matches = matching_connections(connections, display_name, asset_host)
    if not matches:
        print(f"    - No web asset found for {display_name} at {asset_host}")
    asset_errors = []
    for connection in matches:
        connection_id = connection["id"]
        # The server can briefly return 500 while the asset's session is still closing.
        for attempt in range(3):
            try:
                _request(settings, "DELETE", f"/api/connections/{connection_id}", token=token)
                print(f"    - Deleted web asset {display_name} ({asset_host})")
                break
            except RuntimeError as exc:
                if attempt == 2:
                    # The caller reports this failure once, in plain language.
                    asset_errors.append(str(exc))
                else:
                    time.sleep(5)

    users = _request(settings, "GET", "/api/admin/users", token=token) or []
    user = find_user(users, email)
    if not user or not user.get("user_id"):
        print(f"    - No web user found for {email}")
    else:
        _request(settings, "DELETE", f"/api/admin/users/{user['user_id']}", token=token)
        print(f"    - Deleted web user {email} and its server certificate")
    if asset_errors:
        raise RuntimeError("; ".join(asset_errors))
