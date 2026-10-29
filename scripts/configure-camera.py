"""Local-only camera credential entry and ONVIF configuration discovery.

Run with the project's Train interpreter. Passwords never enter command-line
arguments, reports, or stdout; the existing ignored .env is the private store.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
STATE_DIR = ROOT / "data" / "camera-setup"
REPORT_FILE = STATE_DIR / "status.json"


def save_private_values(values: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.is_file() and not (STATE_DIR / "env-before-camera.bak").exists():
        shutil.copy2(ENV_FILE, STATE_DIR / "env-before-camera.bak")
    temporary = STATE_DIR / "env.tmp"
    if ENV_FILE.is_file():
        shutil.copy2(ENV_FILE, temporary)
    else:
        temporary.write_text("", encoding="utf-8")
    try:
        for key, value in values.items():
            set_key(str(temporary), key, value, quote_mode="always", encoding="utf-8")
        os.replace(temporary, ENV_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def report(phase: str, **details) -> dict:
    payload = {"phase": phase, **details}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, REPORT_FILE)
    return payload


def discover_from_env(profile_token: str | None = None) -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.camera_setup import CameraSetupError, discover_camera

    values = dotenv_values(ENV_FILE)
    endpoint = values.get("CAMERA_ONVIF_DEVICE_URL", "") or "http://192.168.1.108/onvif/device_service"
    parts = urlsplit(endpoint)
    username = values.get("CAMERA_ONVIF_USERNAME", "") or ""
    password = values.get("CAMERA_ONVIF_PASSWORD", "") or ""
    if not username or not password:
        raise CameraSetupError("请先在本机窗口填写摄像头账号和密码")
    result = discover_camera(
        parts.hostname or "192.168.1.108", username, password,
        port=parts.port or (443 if parts.scheme == "https" else 80),
        rtsp_port=int(values.get("CAMERA_RTSP_PORT", "554") or "554"),
        profile_token=profile_token or values.get("CAMERA_ONVIF_PROFILE_TOKEN") or None,
        device_service_url=endpoint,
    )
    updates = dict(result["env_values"])
    updates.update({
        "LIVE_CAMERA_ID": values.get("LIVE_CAMERA_ID", "") or "camera-01",
        "LIVE_CAMERA_NAME": "河道远程摄像头",
        "LIVE_CAPTURE_FRAME_STEP": values.get("LIVE_CAPTURE_FRAME_STEP", "") or "3",
        "LIVE_CAPTURE_FPS": values.get("LIVE_CAPTURE_FPS", "") or "25",
        "OPENCV_FFMPEG_CAPTURE_OPTIONS": "rtsp_transport;tcp",
    })
    save_private_values(updates)
    return report("discovered", summary=result["summary"])


def credential_window(*, credentials_only: bool = False) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    values = dotenv_values(ENV_FILE)
    existing = urlsplit(values.get("CAMERA_ONVIF_DEVICE_URL", "") or "http://192.168.1.108/onvif/device_service")
    root = tk.Tk()
    root.title("Vessel 摄像头接入 — 仅在本机保存")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=22)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="填写河边摄像头的登录信息", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
    ttk.Label(frame, text="账号和密码仅保存到本机项目的私有 .env，\n不会写入诊断报告或提交到 GitHub。", justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 15))
    fields = {}
    entries = {}
    for row, (key, label, default) in enumerate((
        ("host", "摄像头 IP", existing.hostname or "192.168.1.108"),
        ("http_port", "HTTP / ONVIF 端口", str(existing.port or 80)),
        ("rtsp_port", "RTSP 端口", values.get("CAMERA_RTSP_PORT", "554") or "554"),
        ("username", "摄像头用户名", values.get("CAMERA_ONVIF_USERNAME", "") or "admin"),
        ("password", "摄像头密码", ""),
    ), start=2):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
        fields[key] = tk.StringVar(value=default)
        entry = ttk.Entry(frame, textvariable=fields[key], width=34, show="●" if key == "password" else "")
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        entries[key] = entry
    status = tk.StringVar(value="请填写后点击“保存到本机配置”。")
    ttk.Label(frame, textvariable=status, wraplength=430).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 10))

    def save() -> None:
        host = fields["host"].get().strip()
        username = fields["username"].get().strip()
        password = fields["password"].get()
        try:
            import ipaddress
            ipaddress.ip_address(host)
            http_port = int(fields["http_port"].get())
            rtsp_port = int(fields["rtsp_port"].get())
            if not 1 <= http_port <= 65535 or not 1 <= rtsp_port <= 65535:
                raise ValueError
            if not username or not password or any(c in username + password for c in "\r\n\x00"):
                raise ValueError
        except ValueError:
            messagebox.showerror("请检查填写内容", "请填写有效 IP、端口、用户名和密码。", parent=root)
            return
        host_part = f"[{host}]" if ":" in host else host
        try:
            save_private_values({
                "CAMERA_ONVIF_DEVICE_URL": f"http://{host_part}:{http_port}/onvif/device_service",
                "CAMERA_ONVIF_USERNAME": username,
                "CAMERA_ONVIF_PASSWORD": password,
                "CAMERA_RTSP_PORT": str(rtsp_port),
            })
            report("credentials_saved", host=host, http_port=http_port, rtsp_port=rtsp_port)
        except OSError:
            messagebox.showerror("保存失败", "无法写入本机私有配置，请检查项目目录权限。", parent=root)
            return
        fields["password"].set("")
        if not credentials_only:
            status.set("已保存，正在读取设备服务与视频参数……")
            root.update_idletasks()
            try:
                discover_from_env()
            except Exception as error:
                sys.path.insert(0, str(ROOT / "backend"))
                from app.live_capture import safe_camera_error
                from app.config import get_effective_settings
                safe = safe_camera_error(error, get_effective_settings())
                report("discovery_failed", error=safe)
                messagebox.showerror("设备检查未完成", safe, parent=root)
                return
        root.destroy()

    ttk.Button(frame, text="保存到本机配置", command=save).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    root.bind("<Return>", lambda _event: save())
    root.update_idletasks()
    root.geometry(f"+{max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)}+{max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)}")
    root.lift()
    entries["password"].focus_set()
    report("awaiting_credentials")
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true", help="Discover camera services using saved private credentials, without a window")
    parser.add_argument("--credentials-only", action="store_true", help="Only collect credentials in the local window")
    parser.add_argument("--profile-token", help="Explicit discovered media profile token, required when resolutions tie")
    options = parser.parse_args()
    if options.discover:
        try:
            print(json.dumps(discover_from_env(options.profile_token), ensure_ascii=True))
        except Exception as error:
            # Credential-bearing library exceptions must not reach stdout.
            try:
                from app.camera_setup import CameraSetupError
                message = str(error) if isinstance(error, CameraSetupError) else "Camera discovery failed; check the device, credentials and network."
            except ImportError:
                message = "Camera discovery module is unavailable."
            print(json.dumps(report("discovery_failed", error=message), ensure_ascii=True))
            sys.exit(1)
    else:
        credential_window(credentials_only=options.credentials_only)
