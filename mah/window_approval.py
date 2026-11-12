"""One-shot Windows dialog for an isolated Fake Adapter approval."""
import json
import sys
from time import monotonic

from .ledger import LedgerError


WINDOW_SECONDS = 300


def review_window(ledger, card_id, content_sha256):
    card = ledger._window_candidate(card_id, content_sha256)
    deadline = monotonic() + WINDOW_SECONDS

    def check_deadline():
        if monotonic() >= deadline:
            raise LedgerError(12, "核准視窗已逾時")

    decision = show_window(json.loads(card["content_json"]), content_sha256, deadline)
    if decision == "expired":
        raise LedgerError(12, "核准視窗已逾時")
    if decision not in ("approved", "rejected"):
        raise LedgerError(10, "核准已取消")
    grant_id = ledger._record_window_decision(card_id, content_sha256, reject=decision == "rejected",
                                              check_deadline=check_deadline)
    if decision == "rejected":
        raise LedgerError(10, "使用者拒絕派工卡")
    return grant_id


def _card_details(card, digest):
    targets = "\n".join(f"  {item['adapter']} / {item['model']} / effort 上限 {item['effort_max']}"
                        for item in card["allowed_targets"])
    return "\n".join((
        f"Card ID　{card['card_id']}", f"完整卡片 SHA-256　{digest}",
        f"任務　{card['task_key']}", f"來源　{card['repo_root']}",
        f"模板　{', '.join(card['allowed_templates'])}", "允許的 Adapter 與模型", targets,
        f"工作目錄　{card['workspace_path']}", f"任務 SHA-256　{card['task_sha256']}",
        f"逾時　{card['timeout_seconds']} 秒", f"核准後有效　{card['ttl_minutes']} 分鐘",
        f"派工上限　{card['max_dispatches']} 次",
    ))


def show_window(card, digest, deadline):
    if sys.platform != "win32":
        raise LedgerError(14, "此核准視窗需要 Windows 互動桌面")
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
    except (ImportError, RuntimeError, OSError) as exc:
        raise LedgerError(14, "無法開啟 Windows 核准視窗") from exc
    except tk.TclError as exc:
        raise LedgerError(14, "無法開啟 Windows 核准視窗") from exc

    outcome = "cancelled"

    def finish(decision):
        nonlocal outcome
        outcome = "expired" if monotonic() >= deadline else decision
        root.quit()

    def tick():
        remaining = max(0, int(deadline - monotonic() + 0.999))
        timer.configure(text=f"核准視窗剩餘 {remaining} 秒")
        if remaining:
            root.after(250, tick)
        else:
            finish("expired")

    try:
        root.title(f"MultiAgentHarness 合成派工核准 | {card['card_id']}")
        root.geometry("820x525")
        root.minsize(600, 420)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", lambda: finish("cancelled"))
        root.bind("<Escape>", lambda _event: finish("cancelled"))
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="合成派工核准", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(frame, text="本次只執行本機 Fake Adapter。請親自確認下列範圍。").pack(anchor="w", pady=(3, 12))
        details_frame = ttk.Frame(frame)
        details_frame.pack(fill="both", expand=True)
        details = tk.Text(details_frame, wrap="char", font=("Segoe UI", 11), padx=8, pady=6)
        scroll = ttk.Scrollbar(details_frame, command=details.yview)
        scroll.pack(side="right", fill="y")
        details.configure(yscrollcommand=scroll.set)
        details.pack(side="left", fill="both", expand=True)
        details.insert("1.0", _card_details(card, digest))
        details.configure(state="disabled")
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        timer = ttk.Label(footer)
        timer.pack(side="left")
        for label, decision in (("核准", "approved"), ("拒絕", "rejected"), ("取消", "cancelled")):
            button = ttk.Button(footer, text=label, command=lambda action=decision: finish(action))
            button.pack(side="right", padx=(8, 0))
        tick()
        if monotonic() < deadline:
            root.mainloop()
        return outcome
    except tk.TclError as exc:
        raise LedgerError(14, "Windows 核准視窗無法完成") from exc
    finally:
        root.destroy()
