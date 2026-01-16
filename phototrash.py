import json
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageStat, ImageTk


@dataclass
class PhotoCandidate:
    path: Path
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class ClusterGroup:
    key: str
    items: list[PhotoCandidate] = field(default_factory=list)

    def sorted_items(self) -> list[PhotoCandidate]:
        return sorted(self.items, key=lambda item: item.score)


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def compute_signature(image: Image.Image, size: int = 9) -> list[int]:
    grayscale = ImageOps.grayscale(image)
    resized = grayscale.resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(resized.getdata())
    signature = []
    for row in range(size):
        row_pixels = pixels[row * size : (row + 1) * size]
        for col in range(size - 1):
            signature.append(1 if row_pixels[col] > row_pixels[col + 1] else 0)
    return signature


def signature_hash(signature: list[int]) -> str:
    return "".join("1" if bit else "0" for bit in signature)


def load_thumbnail(path: Path, max_size: int = 240) -> ImageTk.PhotoImage:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_size, max_size))
        return ImageTk.PhotoImage(image.copy())


def score_image(path: Path) -> PhotoCandidate:
    reasons = []
    score = 0.0
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        if width > 0 and height > 0:
            aspect_ratio = width / height
            if 0.45 <= aspect_ratio <= 0.65:
                score += 0.35
                reasons.append("Похоже на вертикальный скриншот (пропорции).")
            elif 1.6 <= aspect_ratio <= 1.9:
                score += 0.2
                reasons.append("Широкий формат — возможно скриншот.")

        grayscale = ImageOps.grayscale(image)
        stats = ImageStat.Stat(grayscale)
        brightness = stats.mean[0] / 255
        contrast = stats.stddev[0] / 128
        if brightness > 0.85:
            score += 0.2
            reasons.append("Очень светлое изображение (много белого).")
        if contrast > 0.5:
            score += 0.15
            reasons.append("Много контраста — похоже на интерфейс.")

    return PhotoCandidate(path=path, score=score, reasons=reasons)


def group_similar_photos(paths: list[Path]) -> list[ClusterGroup]:
    groups: dict[str, ClusterGroup] = {}
    for path in paths:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                signature = compute_signature(image)
                signature_key = signature_hash(signature)
        except OSError:
            continue

        candidate = score_image(path)
        group = groups.setdefault(signature_key, ClusterGroup(key=signature_key))
        group.items.append(candidate)

    clustered = [group for group in groups.values() if len(group.items) >= 5]
    return sorted(clustered, key=lambda group: len(group.items), reverse=True)


class PhotoTrashApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PhotoTrash AI")
        self.geometry("1100x720")
        self.minsize(980, 640)
        self.folder_path = tk.StringVar(value="Не выбрана")
        self.status_text = tk.StringVar(value="Выберите папку и нажмите «Анализировать».")
        self.candidates: list[PhotoCandidate] = []
        self.groups: list[ClusterGroup] = []
        self.preview_cache: dict[Path, ImageTk.PhotoImage] = {}
        self.worker_queue: queue.Queue = queue.Queue()
        self._build_ui()

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=12)
        header.pack(fill=tk.X)

        title = ttk.Label(
            header,
            text="PhotoTrash AI",
            font=("Segoe UI", 20, "bold"),
        )
        title.pack(anchor=tk.W)

        subtitle = ttk.Label(
            header,
            text="ИИ подсказывает, что можно удалить: скриншоты и серии похожих фото.",
            font=("Segoe UI", 11),
        )
        subtitle.pack(anchor=tk.W, pady=(4, 0))

        controls = ttk.Frame(self, padding=(12, 6))
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Папка:").pack(side=tk.LEFT)
        ttk.Label(controls, textvariable=self.folder_path).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Button(controls, text="Выбрать папку", command=self.select_folder).pack(side=tk.LEFT)
        ttk.Button(controls, text="Анализировать", command=self.start_analysis).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(controls, text="Экспорт отчета", command=self.export_report).pack(
            side=tk.LEFT, padx=8
        )

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        left_panel = ttk.Frame(body)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right_panel = ttk.Frame(body, padding=(12, 0, 0, 0))
        right_panel.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(left_panel, text="Список кандидатов на удаление", font=("Segoe UI", 12, "bold")).pack(
            anchor=tk.W
        )

        columns = ("score", "reason", "path")
        self.candidate_table = ttk.Treeview(left_panel, columns=columns, show="headings")
        self.candidate_table.heading("score", text="Скоринг")
        self.candidate_table.heading("reason", text="Причина")
        self.candidate_table.heading("path", text="Файл")
        self.candidate_table.column("score", width=80, anchor=tk.CENTER)
        self.candidate_table.column("reason", width=320, anchor=tk.W)
        self.candidate_table.column("path", width=420, anchor=tk.W)
        self.candidate_table.pack(fill=tk.BOTH, expand=True)
        self.candidate_table.bind("<<TreeviewSelect>>", self.on_select_candidate)

        ttk.Label(right_panel, text="Превью", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        self.preview_label = ttk.Label(right_panel)
        self.preview_label.pack(pady=8)

        ttk.Separator(right_panel).pack(fill=tk.X, pady=8)

        ttk.Label(right_panel, text="Серии похожих фото", font=("Segoe UI", 12, "bold")).pack(
            anchor=tk.W
        )
        self.group_list = tk.Listbox(right_panel, height=12)
        self.group_list.pack(fill=tk.BOTH, expand=True)
        self.group_list.bind("<<ListboxSelect>>", self.on_select_group)

        self.group_details = ttk.Label(right_panel, text="", justify=tk.LEFT, wraplength=280)
        self.group_details.pack(anchor=tk.W, pady=6)

        status = ttk.Frame(self, padding=(12, 6))
        status.pack(fill=tk.X)
        ttk.Label(status, textvariable=self.status_text).pack(anchor=tk.W)

    def select_folder(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.folder_path.set(selected)
            self.status_text.set("Готово к анализу.")

    def start_analysis(self) -> None:
        folder = self.folder_path.get()
        if not folder or folder == "Не выбрана":
            messagebox.showwarning("PhotoTrash AI", "Пожалуйста, выберите папку с фотографиями.")
            return

        self.status_text.set("Анализируем изображения...")
        self.candidate_table.delete(*self.candidate_table.get_children())
        self.group_list.delete(0, tk.END)
        self.preview_label.configure(image="")
        self.preview_cache.clear()
        self.candidates = []
        self.groups = []

        thread = threading.Thread(target=self._run_analysis, args=(Path(folder),), daemon=True)
        thread.start()
        self.after(100, self._poll_queue)

    def _run_analysis(self, folder: Path) -> None:
        paths = [path for path in folder.rglob("*") if path.is_file() and is_image_file(path)]
        if not paths:
            self.worker_queue.put(("done", "Фотографии не найдены."))
            return

        scored = [score_image(path) for path in paths]
        candidates = sorted(scored, key=lambda item: item.score, reverse=True)
        groups = group_similar_photos(paths)
        self.worker_queue.put(("result", (candidates, groups)))

    def _poll_queue(self) -> None:
        try:
            message_type, payload = self.worker_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_queue)
            return

        if message_type == "done":
            self.status_text.set(payload)
            return

        if message_type == "result":
            self.candidates, self.groups = payload
            self._populate_candidates()
            self._populate_groups()
            self.status_text.set("Готово! Проверьте кандидатов и серии.")

    def _populate_candidates(self) -> None:
        for candidate in self.candidates:
            reasons = " / ".join(candidate.reasons) if candidate.reasons else "Нет явных признаков"
            self.candidate_table.insert(
                "",
                tk.END,
                values=(f"{candidate.score:.2f}", reasons, candidate.path.name),
                tags=(str(candidate.path),),
            )

    def _populate_groups(self) -> None:
        for group in self.groups:
            display = f"Серия из {len(group.items)} фото"
            self.group_list.insert(tk.END, display)

    def _get_candidate_by_path(self, path_str: str) -> PhotoCandidate | None:
        for candidate in self.candidates:
            if str(candidate.path) == path_str:
                return candidate
        return None

    def on_select_candidate(self, _event: tk.Event) -> None:
        selection = self.candidate_table.selection()
        if not selection:
            return
        item_id = selection[0]
        tags = self.candidate_table.item(item_id, "tags")
        if not tags:
            return
        candidate = self._get_candidate_by_path(tags[0])
        if not candidate:
            return
        self._show_preview(candidate.path)

    def on_select_group(self, _event: tk.Event) -> None:
        selection = self.group_list.curselection()
        if not selection:
            return
        group = self.groups[selection[0]]
        sorted_items = group.sorted_items()
        first_path = sorted_items[0].path
        self._show_preview(first_path)
        self.group_details.configure(
            text="В серии: "
            + ", ".join(item.path.name for item in sorted_items[:3])
            + (" ..." if len(sorted_items) > 3 else "")
        )

    def _show_preview(self, path: Path) -> None:
        if path not in self.preview_cache:
            try:
                self.preview_cache[path] = load_thumbnail(path)
            except OSError:
                return
        self.preview_label.configure(image=self.preview_cache[path])
        self.preview_label.image = self.preview_cache[path]

    def export_report(self) -> None:
        if not self.candidates:
            messagebox.showinfo("PhotoTrash AI", "Сначала выполните анализ.")
            return
        data = {
            "folder": self.folder_path.get(),
            "candidates": [
                {
                    "path": str(candidate.path),
                    "score": candidate.score,
                    "reasons": candidate.reasons,
                }
                for candidate in self.candidates
                if candidate.score >= 0.2
            ],
            "series": [
                {
                    "count": len(group.items),
                    "files": [str(item.path) for item in group.sorted_items()],
                }
                for group in self.groups
            ],
        }
        target = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not target:
            return
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        messagebox.showinfo("PhotoTrash AI", f"Отчет сохранен: {target}")


def main() -> None:
    app = PhotoTrashApp()
    app.mainloop()


if __name__ == "__main__":
    main()
