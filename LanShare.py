from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Final

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

APP_HOST: Final[str] = "0.0.0.0"
APP_PORT: Final[int] = 8080
UPLOAD_DIR: Final[Path] = Path("uploads")
QR_PATH: Final[Path] = Path("server_qr.png")

UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024 * 1024  # 50 GB


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


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


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>LAN File Server</title>
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
      margin-top: 18px;
      border: 1px solid var(--panel-border);
      border-radius: 18px;
      background: rgba(17, 24, 39, 0.03);
      padding: 14px 16px;
      font-size: 0.95rem;
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

    .phone-note {
      margin-top: 12px;
      font-size: 0.94rem;
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
    .files {
      display: grid;
      gap: 12px;
    }

    .upload-item,
    .file-item {
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

    .file-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
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
      <div class="top">
        <div class="panel">
          <h1>LAN File Server</h1>
          <p class="lead">
            Drag files here from your PC or open this on your phone using the QR code.
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
          <div class="eyebrow">Open on phone</div>

          <div class="qr-wrap">
            <img src="/qr" alt="Server QR code">
          </div>

          <div class="linkbox">
            <a href="{{ access_url }}" target="_blank">{{ access_url }}</a>
          </div>

          <p class="phone-note">
            Make sure phone and PC are on the same Wi-Fi.
          </p>
        </div>
      </div>

      <div class="sections">
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

  <script>
    const dropzone = document.getElementById("dropzone");
    const pickBtn = document.getElementById("pickBtn");
    const fileInput = document.getElementById("fileInput");
    const uploadList = document.getElementById("uploadList");
    const filesBox = document.getElementById("files");

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
            <div class="file-name">${file.name}</div>
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
            result.textContent = "Upload complete";
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
              <div class="file-name">${file.name}</div>
              <div class="file-meta">${file.size_h}</div>
            </div>
            <div class="file-actions">
              <a class="btn btn-primary" href="/files/${encodeURIComponent(file.name)}" target="_blank">Open</a>
              <a class="btn btn-secondary" href="/download/${encodeURIComponent(file.name)}">Download</a>
              <button class="btn btn-danger" data-name="${file.name}">Delete</button>
            </div>
          </div>
        `;
        filesBox.appendChild(item);
      }

      document.querySelectorAll("button[data-name]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const name = btn.getAttribute("data-name");
          const ok = confirm(`Delete "${name}"?`);
          if (!ok) return;

          const res = await fetch("/delete/" + encodeURIComponent(name), {
            method: "DELETE"
          });

          if (res.ok) {
            loadFiles();
          } else {
            alert("Failed to delete file");
          }
        });
      });
    }

    loadFiles();
    setInterval(loadFiles, 5000);
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    ip = get_local_ip()
    access_url = f"http://{ip}:{APP_PORT}"
    return render_template_string(HTML, access_url=access_url)


@app.route("/api/files")
def api_files():
    return jsonify({"files": list_files()})


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

    target = UPLOAD_DIR / filename
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        target = UPLOAD_DIR / f"{stem}-{timestamp}{suffix}"

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
    ip = get_local_ip()
    access_url = f"http://{ip}:{APP_PORT}"

    img = qrcode.make(access_url)
    img.save(QR_PATH)
    return send_file(QR_PATH, mimetype="image/png")


if __name__ == "__main__":
    ip = get_local_ip()
    print(f"Local:   http://127.0.0.1:{APP_PORT}")
    print(f"LAN:     http://{ip}:{APP_PORT}")
    print(f"Uploads: {UPLOAD_DIR.resolve()}")
    print("Open the LAN URL on your phone if both devices are on the same Wi-Fi.")
    app.run(host=APP_HOST, port=APP_PORT, debug=False)