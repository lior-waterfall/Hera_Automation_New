"""Copy a HERA External IP (TX) certificate into a TX mTLS folder."""

import base64
import os
import posixpath
import re
import shlex

import paramiko

TX_CERT_BEGIN = "=== TX CLIENT CERTIFICATE ==="
TX_CERT_END = "=== END TX CLIENT CERTIFICATE ==="
CLIENT_CERT_BEGIN = "Client Certificate:"
CLIENT_CERT_END = "=== END CLIENT CERTIFICATE ==="


def _normalize_pem(certificate):
    certificate = (certificate or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "BEGIN CERTIFICATE" not in certificate or "END CERTIFICATE" not in certificate:
        return None
    if not certificate.endswith("\n"):
        certificate += "\n"
    return certificate


def _format_pem(certificate):
    """Write the same 64-character PEM layout as a manual TX upload."""
    certificate = _normalize_pem(certificate)
    if not certificate:
        return None
    begin = certificate.find("-----BEGIN CERTIFICATE-----")
    end = certificate.find("-----END CERTIFICATE-----", begin)
    if begin < 0 or end < 0:
        return None
    body = "".join(certificate[begin + len("-----BEGIN CERTIFICATE-----"):end].split())
    if not body:
        return None
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def _extract_between(output, begin_marker, end_marker):
    begin = output.find(begin_marker)
    if begin < 0:
        return None
    line_end = output.find("\n", begin)
    if line_end < 0:
        return None
    end = output.find(end_marker, line_end)
    if end < 0:
        return None
    return output[line_end + 1:end]


def extract_tx_certificate(output):
    """Return the PEM printed by tauri-client-app --setup-account, or None."""
    if not output:
        return None
    for begin_marker, end_marker in (
        (TX_CERT_BEGIN, TX_CERT_END),
        (CLIENT_CERT_BEGIN, CLIENT_CERT_END),
    ):
        certificate = _normalize_pem(_extract_between(output, begin_marker, end_marker))
        if certificate:
            return certificate
    return None


def tx_cert_filename(email):
    """Build a stable PEM filename from the account email."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", email or "").strip("._")
    if not safe:
        raise ValueError("Cannot build a TX certificate filename without an email")
    return f"{safe}.pem"


def tx_upload_configured(settings):
    return bool(settings.get("tx_host") and settings.get("tx_cert_folder"))


def export_client_tx_certificate(settings, client_ip, email, execute=None):
    """Export the newest public .tx certificate for this account from the Windows client."""
    safe_email = (email or "").replace("'", "")
    if not safe_email:
        raise ValueError("Cannot export a TX certificate without an email")

    script = (
        "$certs = @(Get-ChildItem Cert:\\CurrentUser\\My, Cert:\\LocalMachine\\My "
        "-ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -like "
        f"'*{safe_email}*.tx' }})\n"
        "if ($certs.Count -eq 0) { exit 2 }\n"
        "$cert = $certs | Sort-Object NotBefore -Descending | Select-Object -First 1\n"
        "$b64 = [Convert]::ToBase64String($cert.RawData)\n"
        "Write-Output '-----BEGIN CERTIFICATE-----'\n"
        "Write-Output $b64\n"
        "Write-Output '-----END CERTIFICATE-----'\n"
        "Write-Output ('SUBJECT=' + $cert.Subject)\n"
    )
    if execute:
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded_script,
        ]
        output, error_output, exit_status, timed_out = execute(command, 30)
        if timed_out:
            raise RuntimeError(f"Timed out exporting the TX certificate for {email}")
    else:
        remote_script = r"C:\Users\Administrator\export-tx-cert.ps1"
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(
            hostname=client_ip,
            username=settings["client_username"],
            password=settings["client_pass"],
            timeout=20,
        )
        try:
            sftp = ssh_client.open_sftp()
            try:
                with sftp.file(remote_script, "w") as remote_file:
                    remote_file.write(script)
            finally:
                sftp.close()
            _stdin, stdout, stderr = ssh_client.exec_command(
                "powershell -NoProfile -ExecutionPolicy Bypass -File " + remote_script
            )
            output = stdout.read().decode("utf-8", errors="replace")
            error_output = stderr.read().decode("utf-8", errors="replace")
            exit_status = stdout.channel.recv_exit_status()
            ssh_client.exec_command(f'del /f /q "{remote_script}"')
        finally:
            ssh_client.close()

    if exit_status != 0 or "BEGIN CERTIFICATE" not in output:
        details = (error_output or output).strip() or f"exit {exit_status}"
        raise RuntimeError(f"Could not export the TX certificate for {email}: {details}")
    begin = output.find("-----BEGIN CERTIFICATE-----")
    end = output.find("-----END CERTIFICATE-----", begin)
    certificate = None
    if begin >= 0 and end > begin:
        certificate = _normalize_pem(output[begin:end + len("-----END CERTIFICATE-----")])
    if not certificate:
        raise RuntimeError(f"Exported TX certificate for {email} was not a PEM certificate")
    subject_line = next((line for line in output.splitlines() if line.startswith("SUBJECT=")), "")
    if subject_line:
        print(f"    - Exported TX certificate {subject_line[8:]}")
    return certificate


def upload_tx_certificate(settings, email, certificate, connect=None):
    """Upload the TX PEM to settings['tx_cert_folder'] on settings['tx_host']."""
    host = (settings.get("tx_host") or "").strip()
    folder = (settings.get("tx_cert_folder") or "").strip()
    if not host or not folder:
        raise ValueError("TX_HOST and TX_CERT_FOLDER are required to upload the TX certificate")
    certificate = _format_pem(certificate)
    if not certificate:
        raise ValueError("TX certificate is missing or is not a PEM certificate")

    filename = tx_cert_filename(email)
    remote_path = posixpath.join(folder.rstrip("/"), filename)
    ssh_client = connect(settings) if connect else _connect_tx(settings)
    try:
        sftp = ssh_client.open_sftp()
        try:
            sftp.stat(folder)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"TX certificate folder does not exist: {folder}. "
                "Apply the HERA SSL settings once, then copy the mTLS folder path into the script config."
            ) from exc
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(certificate)
        print(f"    - Uploaded client certificate to {host}:{remote_path}")
        _match_folder_owner(ssh_client, folder, remote_path)
        _rehash_tx_certificates(ssh_client, settings, folder, filename)
        return remote_path
    finally:
        ssh_client.close()


def rehash_command(settings, folder):
    """Command that rebuilds the OpenSSL hash links for the mTLS certificate folder."""
    custom = (settings.get("tx_rehash_command") or "").strip()
    quoted_folder = shlex.quote(folder)
    if custom:
        return custom.replace("{folder}", quoted_folder)
    return f"openssl rehash {quoted_folder} || c_rehash {quoted_folder}"


def _run_remote(ssh_client, command):
    _stdin, stdout, stderr = ssh_client.exec_command(command)
    output = stdout.read().decode("utf-8", errors="replace")
    error_output = stderr.read().decode("utf-8", errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, output, error_output


def _match_folder_owner(ssh_client, folder, *paths):
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = f"owner=$(stat -c '%U:%G' {shlex.quote(folder)}) && chown -h \"$owner\" {quoted_paths}"
    exit_status, output, error_output = _run_remote(ssh_client, command)
    if exit_status != 0:
        details = (error_output or output).strip()
        raise RuntimeError(f"Could not match the certificate owner to the TX folder: {details}")


def _rehash_tx_certificates(ssh_client, settings, folder, filename):
    command = rehash_command(settings, folder)
    print(f"    - Rehashing TX certificates: {command}")
    exit_status, output, error_output = _run_remote(ssh_client, command)
    if exit_status != 0:
        details = (error_output or output).strip()
        raise RuntimeError(f"TX certificate rehash failed with status {exit_status}: {details}")
    if output.strip():
        print(output.strip())

    quoted_folder = shlex.quote(folder)
    quoted_filename = shlex.quote(filename)
    list_links = (
        f"cd {quoted_folder} && for link in *.0; do "
        f"[ -L \"$link\" ] && [ \"$(readlink \"$link\")\" = {quoted_filename} ] && printf '%s\\n' \"$link\"; "
        f"done; true"
    )
    _exit_status, output, error_output = _run_remote(ssh_client, list_links)
    links = [line.strip() for line in output.splitlines() if line.strip()]
    if not links:
        details = (error_output or output).strip()
        raise RuntimeError(f"Rehash did not create a hash link for {filename}: {details}")
    _match_folder_owner(ssh_client, folder, *[posixpath.join(folder, link) for link in links])
    subject_status, subject, subject_error = _run_remote(
        ssh_client,
        f"openssl x509 -in {shlex.quote(posixpath.join(folder, filename))} -noout -subject -serial",
    )
    if subject_status == 0 and subject.strip():
        print(f"    - Certificate {subject.strip().replace(chr(10), ' | ')}")
    elif subject_error.strip():
        print(f"    - Could not read the uploaded certificate: {subject_error.strip()}")
    print(f"    - Rehash link ready: {', '.join(links)} -> {filename}")


def delete_tx_certificate(settings, email, connect=None):
    """Remove the uploaded TX PEM and rebuild the hash links."""
    if not tx_upload_configured(settings):
        print("    - TX certificate delete skipped: set TX_HOST and TX_CERT_FOLDER.")
        return False
    host = (settings.get("tx_host") or "").strip()
    folder = (settings.get("tx_cert_folder") or "").strip()
    filename = tx_cert_filename(email)
    remote_path = posixpath.join(folder.rstrip("/"), filename)
    ssh_client = connect(settings) if connect else _connect_tx(settings)
    try:
        sftp = ssh_client.open_sftp()
        try:
            sftp.stat(folder)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"TX certificate folder does not exist: {folder}") from exc
        try:
            sftp.remove(remote_path)
            print(f"    - Deleted TX certificate {host}:{remote_path}")
        except FileNotFoundError:
            print(f"    - TX certificate already absent: {remote_path}")
        finally:
            sftp.close()
        command = rehash_command(settings, folder)
        print(f"    - Rehashing TX certificates after delete: {command}")
        exit_status, output, error_output = _run_remote(ssh_client, command)
        if exit_status != 0:
            details = (error_output or output).strip()
            raise RuntimeError(f"TX certificate rehash failed with status {exit_status}: {details}")
        quoted_folder = shlex.quote(folder)
        quoted_filename = shlex.quote(filename)
        list_links = (
            f"cd {quoted_folder} && for link in *.0; do "
            f"[ -L \"$link\" ] && [ \"$(readlink \"$link\")\" = {quoted_filename} ] && printf '%s\\n' \"$link\"; "
            f"done; true"
        )
        _exit_status, output, error_output = _run_remote(ssh_client, list_links)
        stale_links = [line.strip() for line in output.splitlines() if line.strip()]
        if stale_links:
            details = (error_output or output).strip()
            raise RuntimeError(f"TX hash link still points at {filename}: {details}")
        return True
    finally:
        ssh_client.close()


def _connect_tx(settings):
    host = settings["tx_host"].strip()
    username = (settings.get("tx_ssh_user") or "").strip()
    if not username:
        raise ValueError("TX_SSH_USER is required to upload the TX certificate")

    port = int(settings.get("tx_ssh_port") or 22)
    password = settings.get("tx_ssh_pass") or None
    key_path = (settings.get("tx_ssh_key") or "").strip()
    if key_path:
        key_path = os.path.expanduser(key_path)
    if not password and not key_path:
        raise ValueError("Set TX_SSH_PASS or TX_SSH_KEY to upload the TX certificate")

    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        key_filename=key_path or None,
        timeout=10,
        allow_agent=False,
        look_for_keys=False,
    )
    return ssh_client
