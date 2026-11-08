import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
import struct
import zlib
import json
import io
import os
import sys
import getpass
import importlib.util
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from PIL import Image, ImageTk

# ==========================================
# 1. ЯДРО ФОРМАТА SUPERMGF (Версии 1, 2 и 3)
# ==========================================
MAGIC_V1 = b'MGF1'
MAGIC_V2 = b'MGF2'
MAGIC_V3 = b'MGF3'
VERSION = 3

FLAG_VEC  = 1 << 0
FLAG_RAST = 1 << 1
FLAG_ANIM = 1 << 2

CHUNK_VEC  = b'VEC '
CHUNK_RAST = b'RAST'
CHUNK_ANIM = b'ANIM'
CHUNK_META = b'META'
CHUNK_THMB = b'THMB'
CHUNK_END  = b'END '

@dataclass
class Frame:
    png_bytes: bytes
    delay_ms: int = 100

@dataclass
class SuperMGF:
    width: int = 0
    height: int = 0
    vec_svg: Optional[str] = None
    rast_png: Optional[bytes] = None
    frames: List[Frame] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)
    thumb_png: Optional[bytes] = None

    def flags(self) -> int:
        f = 0
        if self.vec_svg is not None: f |= FLAG_VEC
        if self.rast_png is not None: f |= FLAG_RAST
        if self.frames: f |= FLAG_ANIM
        return f

def _write_chunk(f, ctype: bytes, data: bytes):
    assert len(ctype) == 4, "тип чанка должен быть 4 байта"
    f.write(ctype)
    f.write(struct.pack('<I', len(data)))
    f.write(data)
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    f.write(struct.pack('<I', crc))

def save_mgf(img: SuperMGF, path: str):
    with open(path, 'wb') as f:
        f.write(MAGIC_V3)
        f.write(struct.pack('<H', VERSION))
        f.write(struct.pack('<H', img.flags()))
        f.write(struct.pack('<I', img.width))
        f.write(struct.pack('<I', img.height))

        if img.vec_svg is not None:
            _write_chunk(f, CHUNK_VEC, img.vec_svg.encode('utf-8'))
        if img.rast_png is not None:
            _write_chunk(f, CHUNK_RAST, img.rast_png)
        if img.frames:
            buf = struct.pack('<I', len(img.frames))
            for fr in img.frames:
                buf += struct.pack('<I', fr.delay_ms)
                buf += struct.pack('<I', len(fr.png_bytes))
                buf += fr.png_bytes
            _write_chunk(f, CHUNK_ANIM, buf)
        if img.meta:
            _write_chunk(f, CHUNK_META, json.dumps(img.meta).encode('utf-8'))
        if img.thumb_png is not None:
            _write_chunk(f, CHUNK_THMB, img.thumb_png)
        _write_chunk(f, CHUNK_END, b'')

def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise ValueError(f"неожиданный конец файла: нужно {n}, получено {len(data)}")
    return data

def _read_chunk(f) -> Tuple[bytes, bytes]:
    ctype = _read_exact(f, 4)
    (length,) = struct.unpack('<I', _read_exact(f, 4))
    data = _read_exact(f, length)
    (crc_stored,) = struct.unpack('<I', _read_exact(f, 4))
    crc_calc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    if crc_stored != crc_calc:
        raise ValueError(f"CRC mismatch в чанке {ctype!r}")
    return ctype, data

def load_mgf(path: str) -> SuperMGF:
    with open(path, 'rb') as f:
        magic = _read_exact(f, 4)
        if magic not in (MAGIC_V1, MAGIC_V2, MAGIC_V3):
            raise ValueError(f"не .supermgf файл (магия {magic!r})")

        (version,) = struct.unpack('<H', _read_exact(f, 2))
        (_flags,) = struct.unpack('<H', _read_exact(f, 2))
        (width,)  = struct.unpack('<I', _read_exact(f, 4))
        (height,) = struct.unpack('<I', _read_exact(f, 4))

        img = SuperMGF(width=width, height=height)
        while True:
            ctype, data = _read_chunk(f)
            if ctype == CHUNK_END: break
            elif ctype == CHUNK_VEC: img.vec_svg = data.decode('utf-8')
            elif ctype == CHUNK_RAST: img.rast_png = data
            elif ctype == CHUNK_ANIM:
                (n,) = struct.unpack_from('<I', data, 0)
                off = 4
                for _ in range(n):
                    (delay,) = struct.unpack_from('<I', data, off); off += 4
                    (ln,)    = struct.unpack_from('<I', data, off); off += 4
                    png = data[off:off+ln]; off += ln
                    img.frames.append(Frame(png, delay))
            elif ctype == CHUNK_META: img.meta = json.loads(data.decode('utf-8'))
            elif ctype == CHUNK_THMB: img.thumb_png = data
    return img

def _png_size(data: bytes) -> Tuple[int, int]:
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError("не PNG")
    w, h = struct.unpack('>II', data[16:24])
    return w, h

def from_png(path: str) -> SuperMGF:
    with open(path, 'rb') as f: data = f.read()
    w, h = _png_size(data)
    return SuperMGF(width=w, height=h, rast_png=data)

def from_svg(path: str, width=0, height=0) -> SuperMGF:
    with open(path, 'r', encoding='utf-8') as f: text = f.read()
    return SuperMGF(width=width, height=height, vec_svg=text)

def from_gif(path: str) -> SuperMGF:
    im = Image.open(path)
    frames = []
    w, h = im.size
    for i in range(im.n_frames):
        im.seek(i)
        buf = io.BytesIO()
        im.convert('RGBA').save(buf, 'PNG')
        frames.append(Frame(buf.getvalue(), im.info.get('duration', 100)))
    return SuperMGF(width=w, height=h, frames=frames)

def from_mp4(path: str, max_frames: int = 50) -> SuperMGF:
    import cv2
    cap = cv2.VideoCapture(path)
    frames = []
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    delay = int(1000 / fps) if fps > 0 else 100
    count = 0
    while cap.isOpened() and count < max_frames:
        ret, frame = cap.read()
        if not ret: break
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(frame_rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format='PNG')
        frames.append(Frame(buf.getvalue(), delay))
        count += 1
    cap.release()
    return SuperMGF(width=w, height=h, frames=frames)

# ==========================================
# 2. СИСТЕМА ПЛАГИНОВ (libraries)
# ==========================================
def load_libraries(ui_parent=None):
    """Сканирует папку libraries и загружает плагины"""
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
    lib_dir = os.path.join(base_dir, "libraries")
    
    if not os.path.exists(lib_dir):
        os.makedirs(lib_dir)
        example = """# Это пример библиотеки для SuperMGF Studio.\n# Вы можете создавать свои скрипты и добавлять поддержку новых форматов!\ndef get_info():\n    return "Пример библиотеки"\n"""
        with open(os.path.join(lib_dir, "example.py"), "w", encoding="utf-8") as f:
            f.write(example)
        return

    print("Загрузка библиотек...")
    for filename in os.listdir(lib_dir):
        if filename.endswith(".py") and filename != "example.py":
            filepath = os.path.join(lib_dir, filename)
            try:
                spec = importlib.util.spec_from_file_location(filename[:-3], filepath)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                print(f"Загружена библиотека: {filename}")
                
                if ui_parent and hasattr(module, "register_ui"):
                    # Передаем класс и функцию из главного файла напрямую
                    new_btn = module.register_ui(ui_parent, SuperMGF, save_mgf)
                    if new_btn:
                        new_btn.pack(fill="x", ipady=3, pady=5)
            except Exception as e:
                print(f"Ошибка загрузки {filename}: {e}")

# ==========================================
# 3. ГРАФИЧЕСКИЙ ИНТЕРФЕЙС
# ==========================================
class PackerTab:
    def __init__(self, parent):
        self.parent = parent
        self.png_path = tk.StringVar()
        self.svg_path = tk.StringVar()
        self.gif_path = tk.StringVar()

        tk.Label(parent, text="1. Выберите файлы для упаковки:", font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 10))
        self.create_file_row("PNG", self.png_path, [("PNG", "*.png")])
        self.create_file_row("SVG", self.svg_path, [("SVG", "*.svg")])
        self.create_file_row("GIF", self.gif_path, [("GIF", "*.gif")])

        tk.Label(parent, text="2. Собрать файл:", font=("Arial", 10, "bold")).pack(anchor="w", pady=(20, 10))
        tk.Button(parent, text="Создать .supermgf (v3)", font=("Arial", 11, "bold"), bg="#4CAF50", fg="white", 
                  command=self.pack_files).pack(fill="x", ipady=5)
                  
        tk.Label(parent, text="--- Дополнительные инструменты ---", font=("Arial", 9, "italic"), fg="gray").pack(pady=(15, 5))
        tk.Button(parent, text="🎬 Конвертировать из MP4 (без звука)", bg="#FF9800", fg="white", 
                  command=self.convert_mp4).pack(fill="x", ipady=3)

    def create_file_row(self, label, var, filetypes):
        frame = tk.Frame(self.parent)
        frame.pack(fill="x", pady=2)
        tk.Button(frame, text=f"Выбрать {label}", width=12, command=lambda: self.browse(var, filetypes)).pack(side="left")
        tk.Label(frame, textvariable=var, fg="gray").pack(side="left", padx=10)

    def browse(self, var, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path: var.set(path)

    def pack_files(self):
        png, svg, gif = self.png_path.get(), self.svg_path.get(), self.gif_path.get()
        if not (png or svg or gif):
            messagebox.showwarning("Ошибка", "Выберите хотя бы один файл!"); return

        out_path = filedialog.asksaveasfilename(defaultextension=".supermgf", filetypes=[("SuperMGF", "*.supermgf")])
        if not out_path: return

        try:
            w = h = 0
            vec_data = rast_data = None
            frames = []

            if png: rast = from_png(png); w, h = rast.width, rast.height; rast_data = rast.rast_png
            if svg: vec = from_svg(svg); w = w or vec.width; h = h or vec.height; vec_data = vec.vec_svg
            if gif: anim = from_gif(gif); w = w or anim.width; h = h or anim.height; frames = anim.frames

            current_user = getpass.getuser()

            final_img = SuperMGF(width=w, height=h, vec_svg=vec_data, rast_png=rast_data, frames=frames)
            final_img.meta = {"user": current_user, "created_by": "SuperMGF Studio v3"}
            
            save_mgf(final_img, out_path)
            messagebox.showinfo("Успех", f"Файл сохранен (MGF3):\nАвтор: {current_user}\n{out_path}")
            self.png_path.set(""); self.svg_path.set(""); self.gif_path.set("")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def convert_mp4(self):
        path = filedialog.askopenfilename(filetypes=[("MP4 видео", "*.mp4")])
        if not path: return
        out_path = filedialog.asksaveasfilename(defaultextension=".supermgf", filetypes=[("SuperMGF", "*.supermgf")])
        if not out_path: return

        try:
            max_frames = simpledialog.askinteger("Настройка", "Сколько кадров вытащить из видео?", initialvalue=50)
            if not max_frames: return

            img = from_mp4(path, max_frames=max_frames)
            img.meta = {"user": getpass.getuser(), "created_by": "SuperMGF Studio v3"}
            save_mgf(img, out_path)
            messagebox.showinfo("Успех", f"Видео упаковано!\nКадров: {len(img.frames)}")
        except ImportError:
            messagebox.showerror("Ошибка", "Установите opencv-python:\npip install opencv-python")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

class ViewerTab:
    def __init__(self, parent):
        self.parent = parent
        self.current_filepath = None
        
        top_frame = tk.Frame(parent)
        top_frame.pack(fill="x", pady=(10, 5))
        tk.Button(top_frame, text="Открыть .supermgf", command=self.load_file).pack(side="left")
        self.info_label = tk.Label(top_frame, text="Файл не выбран", fg="gray")
        self.info_label.pack(side="left", padx=10)

        export_frame = tk.Frame(parent)
        export_frame.pack(fill="x", pady=5)
        tk.Button(export_frame, text="💾 Экспорт PNG", command=self.export_png).pack(side="left", padx=2)
        tk.Button(export_frame, text="💾 Экспорт SVG", command=self.export_svg).pack(side="left", padx=2)
        tk.Button(export_frame, text="💾 Экспорт GIF", command=self.export_gif).pack(side="left", padx=2)

        self.canvas = tk.Canvas(parent, bg='#2b2b2b', height=300)
        self.canvas.pack(fill="both", expand=True)

        self.img = None
        self.frames = []
        self.photo = None
        self.anim_job = None

    def load_file(self, path=None):
        if path is None:
            path = filedialog.askopenfilename(filetypes=[("SuperMGF", "*.supermgf")])
        if not path: return

        self.current_filepath = path
        try:
            self.img = load_mgf(path)
            
            author = self.img.meta.get("user", "Неизвестный")
            current_user = getpass.getuser()
            
            if author != "Неизвестный" and author.lower() != current_user.lower():
                ans = messagebox.askyesno(
                    "ВНИМАНИЕ!",
                    f"Эта картинка была сделана другим пользователем ({author}).\n"
                    f"В ней может быть плохой контент.\n\n"
                    f"Хотите ли вы посмотреть её?"
                )
                if not ans:
                    self.canvas.delete("all")
                    self.info_label.config(text="Файл отклонен пользователем", fg="red")
                    self.img = None
                    return

            self.info_label.config(text=f"{self.img.width}x{self.img.height}, кадров: {len(self.img.frames)} | Автор: {author}", fg="black")
            self.canvas.delete("all")
            self.frames.clear()
            
            if self.anim_job:
                self.parent.after_cancel(self.anim_job)
                self.anim_job = None

            if self.img.frames:
                for fr in self.img.frames:
                    self.frames.append((Image.open(io.BytesIO(fr.png_bytes)), fr.delay_ms))
                self.play_frame(0)
            elif self.img.rast_png:
                pil_img = Image.open(io.BytesIO(self.img.rast_png))
                self._draw_pil(pil_img)
            elif self.img.vec_svg:
                try:
                    import resvg_py
                    png_data = resvg_py.svg_to_bytes(svg_string=self.img.vec_svg, width=400, height=400)
                    pil_img = Image.open(io.BytesIO(png_data))
                    self._draw_pil(pil_img)
                except ImportError:
                    self.canvas.create_text(150, 150, text="Установите resvg-py", fill="white")
                except Exception as e:
                    self.canvas.create_text(150, 150, text=f"Ошибка SVG: {e}", fill="red")
            else:
                self.canvas.create_text(150, 150, text="Пустой файл", fill="white")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def export_png(self):
        if not self.img or not self.img.rast_png:
            messagebox.showinfo("Инфо", "В этом файле нет PNG."); return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if path:
            with open(path, 'wb') as f: f.write(self.img.rast_png)
            messagebox.showinfo("Успех", "PNG извлечен!")

    def export_svg(self):
        if not self.img or not self.img.vec_svg:
            messagebox.showinfo("Инфо", "В этом файле нет SVG."); return
        path = filedialog.asksaveasfilename(defaultextension=".svg", filetypes=[("SVG", "*.svg")])
        if path:
            with open(path, 'w', encoding='utf-8') as f: f.write(self.img.vec_svg)
            messagebox.showinfo("Успех", "SVG извлечен!")

    def export_gif(self):
        if not self.img or not self.img.frames:
            messagebox.showinfo("Инфо", "В этом файле нет анимации."); return
        path = filedialog.asksaveasfilename(defaultextension=".gif", filetypes=[("GIF", "*.gif")])
        if path:
            pils = [Image.open(io.BytesIO(fr.png_bytes)).convert('RGBA') for fr in self.img.frames]
            delays = [fr.delay_ms for fr in self.img.frames]
            pils[0].save(path, save_all=True, append_images=pils[1:], duration=delays, loop=0)
            messagebox.showinfo("Успех", "GIF извлечен!")

    def _draw_pil(self, pil_img):
        if pil_img.width > 480 or pil_img.height > 280: pil_img.thumbnail((480, 280))
        self.photo = ImageTk.PhotoImage(pil_img)
        self.canvas.create_image(10, 10, anchor="nw", image=self.photo)

    def play_frame(self, idx):
        if not self.frames: return
        pil_img, delay = self.frames[idx]
        self.photo = ImageTk.PhotoImage(pil_img)
        self.canvas.delete("all")
        self.canvas.create_image(10, 10, anchor="nw", image=self.photo)
        next_idx = (idx + 1) % len(self.frames)
        self.anim_job = self.parent.after(delay, lambda: self.play_frame(next_idx))

def create_main_window(initial_file=None):
    root = tk.Tk()
    root.title("SuperMGF Studio v3.1 (MultiGraphics + Plugins)")
    root.geometry("650x600")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    tab1 = tk.Frame(notebook)
    tab2 = tk.Frame(notebook)
    notebook.add(tab1, text="Упаковщик")
    notebook.add(tab2, text="Просмотрщик и Экспорт")

    PackerTab(tab1)
    viewer = ViewerTab(tab2)

    # Загружаем библиотеки и передаем им вкладку для добавления кнопок
    load_libraries(ui_parent=tab1)

    if initial_file:
        notebook.select(1)
        root.after(100, lambda: viewer.load_file(initial_file))

    root.mainloop()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith('.supermgf'):
        create_main_window(initial_file=sys.argv[1])
    else:
        create_main_window()