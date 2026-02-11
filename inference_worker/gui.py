"""Tkinter GUI window — control panel for the Grid Inference Worker."""

import sys
import threading
import webbrowser
from pathlib import Path

from . import service


def _enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # Give the app its own taskbar identity so it shows our icon, not Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("aipowergrid.grid-inference-worker")
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if hasattr(ctypes.windll.user32, "SetProcessDpiAwarenessContext"):
            ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        else:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _icon_path():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "favicon.ico"
    return Path(__file__).resolve().parent.parent / "favicon.ico"


def _logo_png_path():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "inference_worker" / "web" / "static" / "logo.png"
    return Path(__file__).resolve().parent / "web" / "static" / "logo.png"


def run(url: str, ready: threading.Event = None):
    """Show the Tkinter control window. Server is already running."""
    _enable_dpi_awareness()

    import tkinter as tk

    root = tk.Tk(className="grid-inference-worker")
    root.tk.call("tk", "appname", "grid-inference-worker")
    root.title("Grid Inference Worker")
    root.resizable(False, False)
    root.minsize(320, 280)
    root.configure(bg="#1e293b")

    ico = _icon_path()
    if not ico.is_file() and getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        ico = exe_dir / "favicon.ico"
    if ico.is_file():
        try:
            root.iconbitmap(str(ico))
            root.wm_iconbitmap(str(ico))
            root.update_idletasks()
        except Exception:
            pass

    logo_png = _logo_png_path()
    if logo_png.is_file() and sys.platform != "win32":
        try:
            root.iconphoto(True, tk.PhotoImage(file=str(logo_png)))
        except Exception:
            pass

    main_f = tk.Frame(root, bg="#1e293b", padx=20, pady=20)
    main_f.pack(fill=tk.BOTH, expand=True)

    if logo_png.is_file():
        try:
            logo_img = tk.PhotoImage(file=str(logo_png))
            h = logo_img.height()
            if h > 64:
                subsample = (h + 63) // 64
                logo_img = logo_img.subsample(subsample, subsample)
            main_f._logo_img = logo_img
            tk.Label(main_f, image=logo_img, bg="#1e293b").pack(pady=(0, 10))
        except Exception:
            pass

    title_font = ("Segoe UI", 11, "bold") if sys.platform == "win32" else ("TkDefaultFont", 11, "bold")
    tk.Label(main_f, text="Grid Inference Worker", fg="#f1f5f9", bg="#1e293b", font=title_font).pack(pady=(0, 16))

    btn_f = tk.Frame(main_f, bg="#1e293b")
    btn_f.pack(fill=tk.X)

    def _update_service_label():
        if service.is_installed():
            svc_btn.configure(text="Remove Service")
        else:
            svc_btn.configure(text="Install Service")

    def install_service_action():
        import tkinter.messagebox as mb
        if service.is_installed():
            ok = mb.askyesno(
                "Remove Service",
                "Remove the background service?\n\n"
                "The worker will stop running in the background.\n"
                "The app will restart in user mode.",
                parent=root,
            )
            if not ok:
                return
            if service.uninstall(verbose=False):
                # Relaunch in user mode so the web server starts fresh
                import subprocess
                if getattr(sys, "frozen", False):
                    subprocess.Popen([sys.executable])
                else:
                    subprocess.Popen([sys.executable, "-m", "inference_worker.cli"])
                root.destroy()
                sys.exit(0)
            else:
                mb.showerror("Service", "Could not remove service.", parent=root)
            _update_service_label()
        else:
            ok = mb.askyesno(
                "Install Service",
                "Install as a background service?\n\n"
                "\u2022 Worker runs automatically when you log in\n"
                "\u2022 This window will close\n"
                "\u2022 The service takes over immediately\n"
                f"\u2022 Dashboard stays at {url}\n\n"
                "Continue?",
                parent=root,
            )
            if not ok:
                return
            if service.install(verbose=False, start=False):
                service.schedule_start()
                root.destroy()
                sys.exit(0)
            else:
                mb.showerror("Service", "Could not install service.", parent=root)

    def clear_config_action():
        import tkinter.messagebox as mb
        from .env_utils import ENV_PATH
        ok = mb.askyesno(
            "Clear Config",
            "This will erase your API key, model selection, and all worker settings.\n\n"
            "Are you sure?",
            icon="warning",
            parent=root,
        )
        if ok:
            try:
                ENV_PATH.unlink(missing_ok=True)
                mb.showinfo("Clear Config", "Config cleared. Close and reopen to set up again.", parent=root)
            except Exception as e:
                mb.showerror("Clear Config", f"Could not clear config: {e}", parent=root)

    buttons = [
        ("Open Dashboard", lambda: webbrowser.open(url)),
        ("Install Service", install_service_action),
        ("Exit", lambda: (root.destroy(), sys.exit(0))),
    ]

    is_mac = sys.platform == "darwin"
    btn_widgets = []
    for label, cmd in buttons:
        if is_mac:
            b = tk.Button(
                btn_f, text=label, command=cmd,
                highlightbackground="#1e293b",
                padx=12, pady=6, cursor="hand2",
                font=("TkDefaultFont", 9),
            )
        else:
            b = tk.Button(
                btn_f, text=label, command=cmd,
                bg="#334155", fg="#f1f5f9",
                activebackground="#475569", activeforeground="#f1f5f9",
                highlightbackground="#1e293b", highlightcolor="#1e293b",
                relief=tk.FLAT, borderwidth=0, padx=12, pady=6, cursor="hand2",
                font=("Segoe UI", 9),
            )
        b.pack(fill=tk.X, pady=4)
        btn_widgets.append((label, b))

    # Set initial service button label
    svc_btn = btn_widgets[1][1]
    _update_service_label()

    # Red "Clear Config" button — only enabled if worker is configured
    from .env_utils import is_configured
    if is_mac:
        clear_btn = tk.Button(
            btn_f, text="Clear Config", command=clear_config_action,
            highlightbackground="#1e293b",
            padx=12, pady=6, cursor="hand2",
            font=("TkDefaultFont", 9),
        )
    else:
        clear_btn = tk.Button(
            btn_f, text="Clear Config", command=clear_config_action,
            bg="#7f1d1d", fg="#fca5a5",
            activebackground="#991b1b", activeforeground="#fecaca",
            highlightbackground="#1e293b", highlightcolor="#1e293b",
            relief=tk.FLAT, borderwidth=0, padx=12, pady=6, cursor="hand2",
            font=("Segoe UI", 9),
        )
    clear_btn.pack(fill=tk.X, pady=4)
    if not is_configured():
        clear_btn.configure(state=tk.DISABLED)

    # Disable "Open Dashboard" until the server is ready
    dash_btn = btn_widgets[0][1]
    if ready and not ready.is_set():
        dash_btn.configure(state=tk.DISABLED, text="Starting...")

        def check_ready():
            if ready.is_set():
                dash_btn.configure(state=tk.NORMAL, text="Open Dashboard")
            else:
                root.after(250, check_ready)

        root.after(250, check_ready)

    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(), sys.exit(0)))

    root.deiconify()
    root.lift()
    root.focus_force()
    root.mainloop()
