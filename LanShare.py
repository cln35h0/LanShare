from __future__ import annotations

import atexit
import http.client
import os
import re
import socket
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Final

import ifaddr
import qrcode
from flask import (
    Flask,
    abort,
    jsonify,
    render_template_string,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename
from zeroconf import InterfaceChoice, ServiceBrowser, ServiceInfo, Zeroconf

APP_HOST: Final[str] = "0.0.0.0"
APP_PORT: Final[int] = 8080
UPLOAD_DIR: Final[Path] = Path("uploads")
QR_PATH: Final[Path] = Path("server_qr.png")
SERVICE_TYPE: Final[str] = "_lanshare._tcp.local."

# Unique id for this running instance, so we can ignore ourselves while browsing.
INSTANCE_ID: Final[str] = uuid.uuid4().hex[:12]

UPLOAD_DIR.mkdir(exist_ok=True)


def get_device_name() -> str:
    """Friendly, human-readable name for this device (overridable via env)."""
    name = os.environ.get("LANSHARE_NAME", "").strip()
    if name:
        return name
    return socket.gethostname() or "LanShare device"


DEVICE_NAME: Final[str] = get_device_name()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024 * 1024  # 50 GB


# --------------------------------------------------------------------------- #
# Networking: interface enumeration that works on wired / air-gapped links     #
# --------------------------------------------------------------------------- #


def get_all_ipv4() -> list[str]:
    """Every usable IPv4 on this machine, across all interfaces.

    Unlike the old ``connect(("8.8.8.8", 80))`` trick, this needs no internet,
    so it works on a direct Ethernet cable between two devices, including
    auto-assigned link-local (APIPA, 169.254.x.x) addresses.

    Ordinary LAN addresses are returned first, link-local addresses last,
    because a routed LAN IP is preferred when both exist.
    """
    normal: list[str] = []
    link_local: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            addr = ip.ip
            if not isinstance(addr, str):
                continue
            if addr.startswith("127.") or addr == "0.0.0.0":
                continue
            if addr.startswith("169.254."):
                link_local.append(addr)
            else:
                normal.append(addr)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for addr in normal + link_local:
        if addr not in seen:
            seen.add(addr)
            ordered.append(addr)
    return ordered


def get_primary_ip() -> str:
    """Best single IP to show in the QR code / primary link."""
    ips = get_all_ipv4()
    return ips[0] if ips else "127.0.0.1"


def access_urls() -> list[str]:
    return [f"http://{ip}:{APP_PORT}" for ip in get_all_ipv4()]


def mdns_hostname() -> str:
    """Stable, typeable .local name for this device."""
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", DEVICE_NAME.lower()).strip("-") or "device"
    return f"lanshare-{safe}.local"


# --------------------------------------------------------------------------- #
# File helpers                                                                 #
# --------------------------------------------------------------------------- #


def human_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num} B"


def list_files() -> list[dict]:
    items: list[dict] = []
    for p in sorted(UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            stat = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size": stat.st_size,
                    "size_h": human_size(stat.st_size),
                    "mtime": int(stat.st_mtime),
                }
            )
    return items


def safe_join_uploads(filename: str) -> Path:
    candidate = (UPLOAD_DIR / filename).resolve()
    base = UPLOAD_DIR.resolve()
    if base not in candidate.parents and candidate != base / filename:
        abort(403)
    return candidate


def unique_target(filename: str) -> Path:
    """Pick a non-colliding path inside UPLOAD_DIR for an incoming file."""
    target = UPLOAD_DIR / filename
    if target.exists():
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        target = UPLOAD_DIR / f"{target.stem}-{timestamp}{target.suffix}"
    return target


# --------------------------------------------------------------------------- #
# Peer discovery via mDNS / zeroconf                                           #
# --------------------------------------------------------------------------- #


class PeerListener:
    """Tracks other LanShare devices announced on the local network."""

    def __init__(self) -> None:
        self.peers: dict[str, dict] = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._update(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._update(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.peers.pop(name, None)

    def _update(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=2000)
        if not info:
            return
        props = info.properties or {}
        peer_id = props.get(b"id", b"").decode(errors="ignore")
        # Ignore our own advertisement.
        if peer_id == INSTANCE_ID:
            self.peers.pop(name, None)
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        display = props.get(b"name", b"").decode(errors="ignore") or name.split(".")[0]
        self.peers[name] = {
            "id": peer_id,
            "name": display,
            "addresses": addresses,
            "port": info.port,
            "url": f"http://{addresses[0]}:{info.port}",
        }

    def snapshot(self) -> list[dict]:
        return sorted(self.peers.values(), key=lambda p: p["name"].lower())


_zeroconf: Zeroconf | None = None
_peer_listener = PeerListener()


def start_zeroconf() -> None:
    """Advertise this device and start browsing for peers (incl. link-local)."""
    global _zeroconf
    ips = get_all_ipv4()
    if not ips:
        print("[mDNS] No usable network interfaces found; discovery disabled.")
        return
    try:
        _zeroconf = Zeroconf(interfaces=InterfaceChoice.All)
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"[mDNS] Could not start zeroconf: {exc}")
        return

    info = ServiceInfo(
        SERVICE_TYPE,
        f"{DEVICE_NAME}-{INSTANCE_ID}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip) for ip in ips],
        port=APP_PORT,
        properties={"id": INSTANCE_ID, "name": DEVICE_NAME},
        server=f"{mdns_hostname()}.",
    )
    try:
        _zeroconf.register_service(info, allow_name_change=True)
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"[mDNS] Could not register service: {exc}")

    ServiceBrowser(_zeroconf, SERVICE_TYPE, _peer_listener)
    atexit.register(stop_zeroconf)
    print(f"[mDNS] Advertising as '{DEVICE_NAME}' at {mdns_hostname()}:{APP_PORT}")


def stop_zeroconf() -> None:
    global _zeroconf
    if _zeroconf is not None:
        try:
            _zeroconf.close()
        finally:
            _zeroconf = None


# --------------------------------------------------------------------------- #
# Pushing a file to a peer (streamed, memory-safe for large files)            #
# --------------------------------------------------------------------------- #


def push_file_to_peer(peer_url: str, filepath: Path) -> tuple[int, str]:
    """POST a local file to another LanShare device's /upload endpoint."""
    boundary = uuid.uuid4().hex
    filename = filepath.name
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    size = filepath.stat().st_size
    total = len(preamble) + size + len(epilogue)

    parsed = urllib.parse.urlparse(peer_url)
    conn = http.client.HTTPConnection(
        parsed.hostname, parsed.port or APP_PORT, timeout=60
    )
    try:
        conn.putrequest("POST", "/upload")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(total))
        conn.endheaders()
        conn.send(preamble)
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(epilogue)
        resp = conn.getresponse()
        body = resp.read().decode(errors="ignore")
        return resp.status, body
    finally:
        conn.close()


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#111827">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="LanShare">
  <link rel="manifest" href="/manifest.json">
  <title>LanShare — {{ device_name }}</title>
  <style>
    :root {
      --text: #111827;
      --muted: #4b5563;
      --muted-2: #6b7280;
      --panel-border: rgba(0, 0, 0, 0.08);
      --panel-bg: rgba(255, 255, 255, 0.50);
      --panel-bg-2: rgba(255, 255, 255, 0.45);
      --shadow-lg: 0 25px 50px rgba(0, 0, 0, 0.18);
      --shadow-md: 0 18px 50px rgba(0, 0, 0, 0.08);
      --shadow-sm: 0 12px 35px rgba(0, 0, 0, 0.06);
      --bg-main: linear-gradient(
        to right,
        rgba(229, 231, 235, 0.70),
        rgba(209, 213, 219, 0.70),
        rgba(156, 163, 175, 0.70)
      );
      --btn-dark: #111827;
      --btn-dark-hover: #1f2937;
      --danger: #dc2626;
      --danger-bg: rgba(239, 68, 68, 0.10);
      --danger-border: rgba(239, 68, 68, 0.18);
      --progress-bg: rgba(17, 24, 39, 0.08);
      --progress-fill: linear-gradient(90deg, #6b7280, #374151);
      --ready: #16a34a;
      --ready-bg: rgba(22, 163, 74, 0.10);
      --ready-border: rgba(22, 163, 74, 0.22);
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --text: #f3f4f6;
        --muted: #d1d5db;
        --muted-2: #9ca3af;
        --panel-border: rgba(255, 255, 255, 0.10);
        --panel-bg: rgba(255, 255, 255, 0.05);
        --panel-bg-2: rgba(255, 255, 255, 0.04);
        --shadow-lg: 0 25px 50px rgba(0, 0, 0, 0.28);
        --shadow-md: 0 18px 50px rgba(0, 0, 0, 0.16);
        --shadow-sm: 0 12px 35px rgba(0, 0, 0, 0.14);
        --bg-main: linear-gradient(
          to right,
          rgba(31, 41, 55, 0.70),
          rgba(17, 24, 39, 0.70),
          rgba(3, 7, 18, 0.70)
        );
        --btn-dark: #f9fafb;
        --btn-dark-hover: #e5e7eb;
        --danger: #f87171;
        --danger-bg: rgba(239, 68, 68, 0.10);
        --danger-border: rgba(248, 113, 113, 0.22);
        --progress-bg: rgba(255, 255, 255, 0.10);
        --progress-fill: linear-gradient(90deg, #d1d5db, #9ca3af);
        --ready: #4ade80;
        --ready-bg: rgba(74, 222, 128, 0.10);
        --ready-border: rgba(74, 222, 128, 0.22);
      }
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      min-height: 100%;
    }

    body {
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(255,255,255,0.35), transparent 25%),
        radial-gradient(circle at bottom right, rgba(255,255,255,0.18), transparent 20%),
        #e5e7eb;
      transition: background 0.5s ease, color 0.5s ease;
    }

    @media (prefers-color-scheme: dark) {
      body {
        background:
          radial-gradient(circle at top left, rgba(255,255,255,0.04), transparent 25%),
          radial-gradient(circle at bottom right, rgba(255,255,255,0.03), transparent 20%),
          #030712;
      }
    }

    .wrap {
      width: min(1200px, calc(100% - 24px));
      margin: 24px auto;
    }

    .shell {
      margin: 24px 0;
      max-width: 100%;
      border-radius: 24px;
      padding: 24px 16px;
      color: var(--text);
      box-shadow: var(--shadow-lg);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      background: var(--bg-main);
      transition: background 0.5s ease, color 0.5s ease;
    }

    @media (min-width: 640px) {
      .shell {
        margin: 32px 0;
        padding: 32px 24px;
      }
    }

    @media (min-width: 768px) {
      .shell {
        padding: 40px;
      }
    }

    @media (min-width: 1024px) {
      .shell {
        margin: 48px 0;
        padding: 48px;
      }
    }

    .ready-banner {
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 24px;
      border: 1px solid var(--ready-border);
      background: var(--ready-bg);
      border-radius: 20px;
      padding: 16px 20px;
      box-shadow: var(--shadow-sm);
    }

    .dot {
      flex: none;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--ready);
      box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.5);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.45); }
      70% { box-shadow: 0 0 0 12px rgba(22, 163, 74, 0); }
      100% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0); }
    }

    .ready-title {
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }

    .ready-sub {
      margin-top: 2px;
      font-size: 0.9rem;
      color: var(--muted);
    }

    .top {
      display: grid;
      grid-template-columns: 1.5fr 0.9fr;
      gap: 24px;
      align-items: stretch;
    }

    .panel {
      border: 1px solid var(--panel-border);
      background: var(--panel-bg);
      border-radius: 28px;
      padding: 24px;
      box-shadow: var(--shadow-md);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .panel-soft {
      border: 1px solid var(--panel-border);
      background: var(--panel-bg-2);
      border-radius: 24px;
      padding: 20px;
      box-shadow: var(--shadow-sm);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    h1 {
      margin: 0;
      font-size: clamp(2rem, 4vw, 2.75rem);
      line-height: 1.1;
      letter-spacing: -0.03em;
      font-weight: 700;
    }

    p {
      margin: 0;
    }

    .lead {
      margin-top: 12px;
      max-width: 760px;
      font-size: 0.98rem;
      line-height: 1.8;
      color: var(--muted);
    }

    .eyebrow {
      margin-bottom: 12px;
      font-size: 0.74rem;
      font-weight: 700;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      color: var(--muted-2);
    }

    .dropzone {
      margin-top: 24px;
      border: 2px dashed rgba(0, 0, 0, 0.12);
      border-radius: 24px;
      background: rgba(255, 255, 255, 0.45);
      padding: 48px 24px;
      min-height: 240px;
      display: grid;
      place-items: center;
      text-align: center;
      cursor: pointer;
      transition: all 0.25s ease;
    }

    .dropzone:hover {
      border-color: rgba(0, 0, 0, 0.22);
      background: rgba(255, 255, 255, 0.62);
    }

    .dropzone.dragover {
      border-color: rgba(0, 0, 0, 0.28);
      background: rgba(255, 255, 255, 0.72);
      transform: scale(1.01);
    }

    @media (prefers-color-scheme: dark) {
      .dropzone {
        border-color: rgba(255, 255, 255, 0.14);
        background: rgba(255, 255, 255, 0.04);
      }

      .dropzone:hover {
        border-color: rgba(255, 255, 255, 0.24);
        background: rgba(255, 255, 255, 0.06);
      }

      .dropzone.dragover {
        border-color: rgba(255, 255, 255, 0.28);
        background: rgba(255, 255, 255, 0.08);
      }
    }

    .drop-title {
      font-size: 1.2rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .drop-subtitle {
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.95rem;
    }

    .btn-row {
      margin-top: 20px;
      display: flex;
      justify-content: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .btn {
      appearance: none;
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 11px 18px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      white-space: nowrap;
    }

    .btn-primary {
      background: var(--btn-dark);
      color: #fff;
    }

    .btn-primary:hover {
      background: var(--btn-dark-hover);
      transform: translateY(-1px);
    }

    .btn-secondary {
      border-color: var(--panel-border);
      background: rgba(255, 255, 255, 0.35);
      color: var(--text);
    }

    .btn-secondary:hover {
      background: rgba(255, 255, 255, 0.52);
    }

    .btn-danger {
      border-color: var(--danger-border);
      background: var(--danger-bg);
      color: var(--danger);
    }

    .btn-danger:hover {
      transform: translateY(-1px);
      filter: brightness(1.02);
    }

    @media (prefers-color-scheme: dark) {
      .btn-primary {
        color: #111827;
      }

      .btn-secondary {
        background: rgba(255, 255, 255, 0.04);
      }

      .btn-secondary:hover {
        background: rgba(255, 255, 255, 0.08);
      }
    }

    .hidden {
      display: none;
    }

    .qr-box {
      text-align: center;
    }

    .qr-wrap {
      margin: 18px auto 0;
      width: min(100%, 220px);
      aspect-ratio: 1 / 1;
      display: grid;
      place-items: center;
      border-radius: 24px;
      background: #ffffff;
      padding: 14px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.08);
    }

    .qr-wrap img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
      border-radius: 16px;
    }

    .linkbox {
      margin-top: 12px;
      border: 1px solid var(--panel-border);
      border-radius: 18px;
      background: rgba(17, 24, 39, 0.03);
      padding: 12px 14px;
      font-size: 0.9rem;
      color: var(--muted);
      word-break: break-all;
    }

    .linkbox a {
      color: inherit;
      text-decoration: none;
    }

    .linkbox a:hover {
      text-decoration: underline;
    }

    .link-label {
      display: block;
      margin-bottom: 4px;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted-2);
    }

    .phone-note {
      margin-top: 12px;
      font-size: 0.9rem;
      line-height: 1.7;
      color: var(--muted);
    }

    .sections {
      margin-top: 24px;
      display: grid;
      gap: 18px;
    }

    .section-title {
      margin: 0 0 12px;
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }

    .upload-list,
    .files,
    .peers {
      display: grid;
      gap: 12px;
    }

    .upload-item,
    .file-item,
    .peer-item {
      border: 1px solid var(--panel-border);
      background: var(--panel-bg-2);
      border-radius: 24px;
      padding: 18px;
      box-shadow: var(--shadow-sm);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .file-name {
      font-size: 1rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      word-break: break-word;
    }

    .file-meta {
      margin-top: 6px;
      font-size: 0.92rem;
      color: var(--muted);
    }

    .progress {
      width: 100%;
      height: 10px;
      margin-top: 14px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--progress-bg);
    }

    .bar {
      width: 0%;
      height: 100%;
      background: var(--progress-fill);
      transition: width 0.1s linear;
    }

    .meta {
      margin-top: 10px;
      font-size: 0.92rem;
      color: var(--muted);
      line-height: 1.7;
    }

    .result {
      margin-top: 8px;
    }

    .muted {
      color: var(--muted);
    }

    .danger-text {
      color: var(--danger);
      font-weight: 600;
    }

    .ok-text {
      color: var(--ready);
      font-weight: 600;
    }

    .file-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }

    .peer-select {
      appearance: none;
      border: 1px solid var(--panel-border);
      background: rgba(255, 255, 255, 0.35);
      color: var(--text);
      border-radius: 999px;
      padding: 10px 14px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
    }

    @media (prefers-color-scheme: dark) {
      .peer-select {
        background: rgba(255, 255, 255, 0.06);
      }
    }

    .peer-dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--ready);
      margin-right: 8px;
    }

    .toast {
      position: fixed;
      left: 50%;
      bottom: 24px;
      transform: translateX(-50%) translateY(20px);
      background: var(--btn-dark);
      color: #fff;
      padding: 12px 18px;
      border-radius: 999px;
      font-size: 0.92rem;
      font-weight: 600;
      box-shadow: var(--shadow-md);
      opacity: 0;
      pointer-events: none;
      transition: all 0.3s ease;
      z-index: 50;
    }

    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }

    @media (prefers-color-scheme: dark) {
      .toast { color: #111827; }
    }

    @media (max-width: 860px) {
      .top {
        grid-template-columns: 1fr;
      }

      .panel,
      .panel-soft {
        padding: 20px;
      }

      .dropzone {
        min-height: 210px;
        padding: 36px 18px;
      }

      .file-actions {
        width: 100%;
      }

      .file-actions .btn {
        flex: 1 1 auto;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="shell">
      <div class="ready-banner">
        <span class="dot"></span>
        <div>
          <div class="ready-title">{{ device_name }} is ready</div>
          <div class="ready-sub">Accepting and sharing files at the same time</div>
        </div>
      </div>

      <div class="top">
        <div class="panel">
          <h1>LanShare</h1>
          <p class="lead">
            Drop files here from this PC, open this page on your phone with the QR code,
            or send a file straight to another device on the network below.
            Works over Wi-Fi and over a direct Ethernet cable (no internet needed).
          </p>

          <div id="dropzone" class="dropzone">
            <div>
              <div class="drop-title">Drop files here</div>
              <div class="drop-subtitle">or click to select multiple files</div>

              <div class="btn-row">
                <button class="btn btn-primary" id="pickBtn" type="button">Choose files</button>
              </div>

              <input id="fileInput" class="hidden" type="file" multiple>
            </div>
          </div>
        </div>

        <div class="panel qr-box">
          <div class="eyebrow">Open on another device</div>

          <div class="qr-wrap">
            <img src="/qr" alt="Server QR code">
          </div>

          {% if mdns_url %}
          <div class="linkbox">
            <span class="link-label">By name (any platform)</span>
            <a href="{{ mdns_url }}" target="_blank">{{ mdns_url }}</a>
          </div>
          {% endif %}

          {% for url in access_urls %}
          <div class="linkbox">
            <span class="link-label">{{ "Wired / link-local" if "169.254." in url else "By IP address" }}</span>
            <a href="{{ url }}" target="_blank">{{ url }}</a>
          </div>
          {% endfor %}

          <p class="phone-note">
            Same Wi-Fi, or an Ethernet cable between the two devices.
          </p>
        </div>
      </div>

      <div class="sections">
        <div class="panel-soft">
          <div class="section-title">Devices on this network</div>
          <div id="peers" class="peers">
            <div class="muted">Looking for other LanShare devices…</div>
          </div>
        </div>

        <div class="panel-soft">
          <div class="section-title">Uploads in progress</div>
          <div id="uploadList" class="upload-list">
            <div class="muted">No active uploads.</div>
          </div>
        </div>

        <div class="panel-soft">
          <div class="section-title">Files</div>
          <div id="files" class="files"></div>
        </div>
      </div>
    </section>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    const dropzone = document.getElementById("dropzone");
    const pickBtn = document.getElementById("pickBtn");
    const fileInput = document.getElementById("fileInput");
    const uploadList = document.getElementById("uploadList");
    const filesBox = document.getElementById("files");
    const peersBox = document.getElementById("peers");
    const toast = document.getElementById("toast");

    let PEERS = [];

    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add("show");
      clearTimeout(toast._t);
      toast._t = setTimeout(() => toast.classList.remove("show"), 2600);
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c]));
    }

    function formatBytes(bytes) {
      if (bytes === 0) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      const i = Math.floor(Math.log(bytes) / Math.log(1024));
      return (bytes / Math.pow(1024, i)).toFixed(2) + " " + units[i];
    }

    function formatTime(seconds) {
      if (!isFinite(seconds) || seconds < 0) return "--";
      if (seconds < 60) return `${Math.ceil(seconds)} sec`;
      const min = Math.floor(seconds / 60);
      const sec = Math.ceil(seconds % 60);
      return `${min} min ${sec} sec`;
    }

    pickBtn.addEventListener("click", () => fileInput.click());

    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) uploadFiles(fileInput.files);
    });

    ["dragenter", "dragover"].forEach(evt => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach(evt => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", (e) => {
      const files = e.dataTransfer.files;
      if (files.length) uploadFiles(files);
    });

    function makeUploadItem(file) {
      if (uploadList.textContent.includes("No active uploads")) {
        uploadList.innerHTML = "";
      }

      const el = document.createElement("div");
      el.className = "upload-item";
      el.innerHTML = `
        <div class="row">
          <div>
            <div class="file-name">${escapeHtml(file.name)}</div>
            <div class="file-meta">${formatBytes(file.size)}</div>
          </div>
        </div>
        <div class="progress"><div class="bar"></div></div>
        <div class="meta">
          <span class="percent">0%</span> •
          <span class="speed">0 B/s</span> •
          <span class="eta">ETA --</span> •
          <span class="loaded">0 B / ${formatBytes(file.size)}</span>
        </div>
        <div class="meta result"></div>
      `;
      uploadList.prepend(el);
      return el;
    }

    function cleanupUploadsIfEmpty() {
      if (!uploadList.children.length) {
        uploadList.innerHTML = `<div class="muted">No active uploads.</div>`;
      }
    }

    async function uploadFiles(fileList) {
      for (const file of fileList) {
        await uploadSingle(file);
      }
      await loadFiles();
    }

    function uploadSingle(file) {
      return new Promise((resolve) => {
        const card = makeUploadItem(file);
        const bar = card.querySelector(".bar");
        const percent = card.querySelector(".percent");
        const speed = card.querySelector(".speed");
        const eta = card.querySelector(".eta");
        const loaded = card.querySelector(".loaded");
        const result = card.querySelector(".result");

        const formData = new FormData();
        formData.append("file", file);

        const xhr = new XMLHttpRequest();
        const start = performance.now();

        xhr.open("POST", "/upload");

        xhr.upload.onprogress = (e) => {
          if (!e.lengthComputable) return;

          const pct = (e.loaded / e.total) * 100;
          const elapsed = Math.max((performance.now() - start) / 1000, 0.001);
          const rate = e.loaded / elapsed;
          const remaining = e.total - e.loaded;
          const etaSec = rate > 0 ? remaining / rate : Infinity;

          bar.style.width = pct.toFixed(1) + "%";
          percent.textContent = pct.toFixed(1) + "%";
          speed.textContent = formatBytes(rate) + "/s";
          eta.textContent = "ETA " + formatTime(etaSec);
          loaded.textContent = `${formatBytes(e.loaded)} / ${formatBytes(e.total)}`;
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            bar.style.width = "100%";
            percent.textContent = "100%";
            result.innerHTML = `<span class="ok-text">Upload complete</span>`;
          } else {
            result.innerHTML = `<span class="danger-text">Upload failed</span>`;
          }

          setTimeout(() => {
            card.remove();
            cleanupUploadsIfEmpty();
            resolve();
          }, 1200);
        };

        xhr.onerror = () => {
          result.innerHTML = `<span class="danger-text">Network error</span>`;
          setTimeout(() => {
            card.remove();
            cleanupUploadsIfEmpty();
            resolve();
          }, 1800);
        };

        xhr.send(formData);
      });
    }

    function peerOptionsHtml() {
      if (!PEERS.length) {
        return `<option value="" disabled selected>No devices found</option>`;
      }
      return `<option value="" disabled selected>Send to…</option>` +
        PEERS.map(p => `<option value="${escapeHtml(p.url)}">${escapeHtml(p.name)}</option>`).join("");
    }

    async function loadPeers() {
      try {
        const res = await fetch("/api/peers");
        const data = await res.json();
        PEERS = data.peers || [];
      } catch (e) {
        PEERS = [];
      }

      if (!PEERS.length) {
        peersBox.innerHTML = `<div class="muted">No other LanShare devices found yet. Open LanShare on another device on the same network.</div>`;
        return;
      }

      peersBox.innerHTML = "";
      for (const p of PEERS) {
        const el = document.createElement("div");
        el.className = "peer-item";
        el.innerHTML = `
          <div class="row">
            <div>
              <div class="file-name"><span class="peer-dot"></span>${escapeHtml(p.name)}</div>
              <div class="file-meta">${escapeHtml(p.url)}</div>
            </div>
            <div class="file-actions">
              <a class="btn btn-secondary" href="${escapeHtml(p.url)}" target="_blank">Open</a>
            </div>
          </div>
        `;
        peersBox.appendChild(el);
      }
    }

    async function sendToPeer(filename, peerUrl, btn) {
      if (!peerUrl) {
        showToast("Pick a device first");
        return;
      }
      const original = btn.textContent;
      btn.textContent = "Sending…";
      btn.disabled = true;
      try {
        const res = await fetch("/api/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename, peer: peerUrl })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          showToast(`Sent "${filename}"`);
        } else {
          showToast(data.error || "Send failed");
        }
      } catch (e) {
        showToast("Send failed");
      } finally {
        btn.textContent = original;
        btn.disabled = false;
      }
    }

    async function loadFiles() {
      const res = await fetch("/api/files");
      const data = await res.json();
      filesBox.innerHTML = "";

      if (!data.files.length) {
        filesBox.innerHTML = `<div class="muted">No files uploaded yet.</div>`;
        return;
      }

      for (const file of data.files) {
        const item = document.createElement("div");
        item.className = "file-item";
        item.innerHTML = `
          <div class="row">
            <div>
              <div class="file-name">${escapeHtml(file.name)}</div>
              <div class="file-meta">${file.size_h}</div>
            </div>
            <div class="file-actions">
              <a class="btn btn-primary" href="/files/${encodeURIComponent(file.name)}" target="_blank">Open</a>
              <a class="btn btn-secondary" href="/download/${encodeURIComponent(file.name)}">Download</a>
              <select class="peer-select">${peerOptionsHtml()}</select>
              <button class="btn btn-secondary send-btn">Send</button>
              <button class="btn btn-danger del-btn" data-name="${escapeHtml(file.name)}">Delete</button>
            </div>
          </div>
        `;

        const select = item.querySelector(".peer-select");
        const sendBtn = item.querySelector(".send-btn");
        sendBtn.addEventListener("click", () => sendToPeer(file.name, select.value, sendBtn));

        const delBtn = item.querySelector(".del-btn");
        delBtn.addEventListener("click", async () => {
          const name = delBtn.getAttribute("data-name");
          if (!confirm(`Delete "${name}"?`)) return;
          const res = await fetch("/delete/" + encodeURIComponent(name), { method: "DELETE" });
          if (res.ok) {
            loadFiles();
          } else {
            showToast("Failed to delete file");
          }
        });

        filesBox.appendChild(item);
      }
    }

    loadPeers();
    loadFiles();
    setInterval(loadFiles, 5000);
    setInterval(loadPeers, 4000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML,
        device_name=DEVICE_NAME,
        access_urls=access_urls(),
        mdns_url=f"http://{mdns_hostname()}:{APP_PORT}",
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ready",
            "name": DEVICE_NAME,
            "id": INSTANCE_ID,
            "addresses": get_all_ipv4(),
            "port": APP_PORT,
        }
    )


@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": f"LanShare — {DEVICE_NAME}",
            "short_name": "LanShare",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#e5e7eb",
            "theme_color": "#111827",
            "icons": [],
        }
    )


@app.route("/api/files")
def api_files():
    return jsonify({"files": list_files()})


@app.route("/api/peers")
def api_peers():
    return jsonify({"peers": _peer_listener.snapshot()})


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    peer = data.get("peer", "")
    if not filename or not peer:
        return jsonify({"error": "filename and peer are required"}), 400

    target = safe_join_uploads(filename)
    if not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404

    parsed = urllib.parse.urlparse(peer)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return jsonify({"error": "Invalid peer URL"}), 400

    try:
        status, _ = push_file_to_peer(peer, target)
    except Exception as exc:
        return jsonify({"error": f"Could not reach device: {exc}"}), 502

    if 200 <= status < 300:
        return jsonify({"ok": True})
    return jsonify({"error": f"Device responded with {status}"}), 502


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file field"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    target = unique_target(filename)
    file.save(target)
    return jsonify({"ok": True, "filename": target.name})


@app.route("/files/<path:filename>")
def open_file(filename: str):
    target = safe_join_uploads(filename)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR, target.name, as_attachment=False)


@app.route("/download/<path:filename>")
def download_file(filename: str):
    target = safe_join_uploads(filename)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR, target.name, as_attachment=True)


@app.route("/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename: str):
    target = safe_join_uploads(filename)
    if not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    target.unlink(missing_ok=False)
    return jsonify({"ok": True})


@app.route("/qr")
def qr():
    access_url = f"http://{get_primary_ip()}:{APP_PORT}"
    img = qrcode.make(access_url)
    img.save(QR_PATH)
    return send_file(QR_PATH, mimetype="image/png")


def main() -> None:
    ips = get_all_ipv4()
    start_zeroconf()

    print(f"Device:  {DEVICE_NAME}")
    print(f"Local:   http://127.0.0.1:{APP_PORT}")
    print(f"By name: http://{mdns_hostname()}:{APP_PORT}")
    for ip in ips:
        kind = "wired/link-local" if ip.startswith("169.254.") else "lan"
        print(f"Access:  http://{ip}:{APP_PORT}  ({kind})")
    print(f"Uploads: {UPLOAD_DIR.resolve()}")
    print("Ready — accepting and sharing files. Open a URL above on another device.")

    try:
        from waitress import serve

        serve(app, host=APP_HOST, port=APP_PORT, threads=8)
    except ImportError:
        print("[warn] waitress not installed; using Flask's threaded dev server.")
        app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
