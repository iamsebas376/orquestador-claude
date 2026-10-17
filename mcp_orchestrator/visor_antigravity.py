"""Visor propio para el modo ANTIGRAVITY_VISTA=visor (agy sin ventana) y el modo api de respaldo.

En el modo por defecto (tui) no se usa: la tarea corre en el CLI oficial de Antigravity.
Uso: python visor_antigravity.py <archivo.events>
El servidor escribe <base>.meta (modelo, instrucción), va agregando eventos JSON (formato stream-json de
`agy`) a <base>.events y crea <base>.done con una línea de estado al terminar.
"""
import json
import sys
import time
from pathlib import Path

from rich.align import Align
from rich.box import HEAVY, ROUNDED
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# Degradado característico de Gemini/Antigravity: azul → violeta → rosa
GRADIENT = ["#4796e3", "#6f86e8", "#9177c7", "#b06fc1", "#d96570", "#e8826a"]
BLUE, VIOLET, PINK, GREEN, AMBER = "#8ab4f8", "#c58af9", "#f28b82", "#81c995", "#fdd663"
MUTED, BAR_BG, CARD = "#8a91a5", "#161a23", "#2a3040"

theme = Theme({
    "markdown.h1": "bold #ffffff on #4f5bd5", "markdown.h1.border": VIOLET,
    "markdown.h2": f"bold {VIOLET}", "markdown.h3": f"bold {BLUE}", "markdown.h4": f"bold {PINK}",
    "markdown.strong": f"bold {AMBER}", "markdown.em": f"italic {PINK}",
    "markdown.code": "bold #f6c177 on #232837", "markdown.link": f"underline {BLUE}", "markdown.link_url": BLUE,
    "markdown.item.bullet": f"bold {PINK}", "markdown.item.number": f"bold {BLUE}",
    "markdown.block_quote": f"italic {MUTED}", "markdown.hr": VIOLET,
    "markdown.table.header": f"bold {VIOLET}", "markdown.table.border": "#4a5168",
})
console = Console(theme=theme)

GLYPHS = {
    "A": ["▄▀█", "█▀█"], "N": ["█▄ █", "█ ▀█"], "T": ["▀█▀", " █ "], "I": ["█", "█"], "G": ["█▀▀", "█▄█"],
    "R": ["█▀█", "█▀▄"], "V": ["█ █", "▀▄▀"], "Y": ["█▄█", " █ "],
}
LOGO = [" ".join(GLYPHS[c][row] for c in "ANTIGRAVITY") for row in range(2)]

TOOL_STYLE = [  # (fragmento del nombre, icono, etiqueta, color)
    ("write", "✎", "crear", GREEN), ("replace", "✎", "editar", GREEN), ("edit", "✎", "editar", GREEN),
    ("view", "◉", "leer", BLUE), ("read", "◉", "leer", BLUE), ("command", "$", "comando", AMBER),
    ("list", "▤", "listar", BLUE), ("grep", "⌕", "buscar", VIOLET), ("find", "⌕", "buscar", VIOLET),
    ("search", "⌕", "buscar", VIOLET), ("browser", "◍", "navegador", PINK), ("ask", "?", "pregunta", PINK),
]
PARAM_KEYS = ("TargetFile", "AbsolutePath", "CommandLine", "Query", "SearchPath", "Url", "DirectoryPath")


def _lerp(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def gradient(text: str, shift: float = 0.0, bold: bool = True) -> Text:
    out, n = Text(), max(1, len(text) - 1)
    for i, ch in enumerate(text):
        pos = ((i / n) + shift) % 1.0 * (len(GRADIENT) - 1)
        k = min(int(pos), len(GRADIENT) - 2)
        out.append(ch, style=("bold " if bold else "") + _lerp(GRADIENT[k], GRADIENT[k + 1], pos - k))
    return out


def chip(label: str, bg: str, fg: str = "#0d1017") -> Text:
    return Text(f" {label} ", style=f"bold {fg} on {bg}")


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


events_path = Path(sys.argv[1])
base = events_path.with_suffix("")
meta, done = base.with_suffix(".meta"), base.with_suffix(".done")


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


for _ in range(50):
    if meta.exists():
        break
    time.sleep(0.1)
model, task = (read(meta).splitlines() + ["antigravity", ""])[:2]
start = time.monotonic()


class State:
    """Estado reconstruido a partir de los eventos stream-json de agy."""

    def __init__(self) -> None:
        self.offset, self.model = 0, model
        self.reset()

    def reset(self) -> None:
        self.text, self.steps, self.usage = "", {}, {"input_tokens": 0, "output_tokens": 0}
        self.result = None

    def feed(self) -> None:
        try:
            with open(events_path, encoding="utf-8") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return
        if not chunk.endswith("\n"):  # deja la línea incompleta para la próxima lectura
            cut = chunk.rfind("\n") + 1
            chunk = chunk[:cut]
        self.offset += len(chunk.encode("utf-8"))
        for line in chunk.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "restart":
                self.reset()
                self.model = ev.get("model", self.model)
            elif kind == "init":
                self.model = (ev.get("init") or {}).get("model", self.model)
            elif kind == "result":
                self.result = ev.get("result") or {}
                self.usage = self.result.get("usage") or self.usage
            elif kind == "step_update":
                st = ev.get("step_update") or {}
                self.text += st.get("text_delta") or ""
                if st.get("state") == "DONE" and st.get("usage") and not self.result:
                    for k in ("input_tokens", "output_tokens"):
                        self.usage[k] = self.usage.get(k, 0) + st["usage"].get(k, 0)
                if st.get("step_type") == "tool":
                    idx = st.get("step_index", len(self.steps))
                    prev = self.steps.get(idx, {})
                    self.steps[idx] = {**prev, "name": st.get("tool_name") or prev.get("name", "tool"),
                                       "info": st.get("tool_info") or prev.get("info") or {},
                                       "state": st.get("state", "ACTIVE"),
                                       "dur": st.get("duration_seconds", prev.get("dur"))}


S = State()


def clock() -> str:
    s = int(time.monotonic() - start)
    return f"{s // 60:02d}:{s % 60:02d}"


def logo(shift: float) -> Group:
    return Group(*[Align.center(gradient(line, shift + i * 0.05)) for i, line in enumerate(LOGO)])


def header(finished: bool, ok: bool) -> Table:
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(ratio=1)
    t.add_column(justify="right")
    left = Text.assemble(gradient(" ✦ Antigravity", 0.0), "  ", chip(f"◆ {S.model}", VIOLET), " ",
                         chip("plan Pro · vía orquestador", CARD, "#d7dbe6"))
    state = chip("● TRABAJANDO", BLUE) if not finished else chip("✓ LISTO", GREEN) if ok else chip("✖ ERROR", PINK)
    t.add_row(left, Text.assemble(state, " ", chip(f"⏱ {clock()}", AMBER), " "))
    return t


def task_card() -> Panel:
    return Panel(Text(task, style="#e6e9f2"), title=Text.assemble(("❯ ", f"bold {PINK}"), ("tarea", f"bold {BLUE}")),
                 title_align="left", box=ROUNDED, border_style=BLUE, padding=(0, 1))


def tool_row(step: dict, frame: int) -> Text:
    name = (step.get("name") or "tool").lower()
    icon, label, color = next(((i, l, c) for k, i, l, c in TOOL_STYLE if k in name), ("⚙", name, MUTED))
    params = (step.get("info") or {}).get("parameters") or {}
    arg = next((str(params[k]) for k in PARAM_KEYS if params.get(k)), "")
    arg = arg.replace("/", "\\") if ":" in arg[:3] else arg
    if step.get("state") == "DONE":
        mark = Text("✓", style=f"bold {GREEN}")
        dur = Text(f" {step['dur']:.1f}s" if step.get("dur") else "", style=MUTED)
    elif step.get("state") in ("ERROR", "FAILED", "CANCELED"):
        mark, dur = Text("✖", style=f"bold {PINK}"), Text("")
    else:
        mark, dur = Text("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[frame % 10], style=f"bold {BLUE}"), Text(" …", style=MUTED)
    return Text.assemble(" ", mark, "  ", (f"{icon} ", f"bold {color}"), (f"{label:<9}", f"bold {color}"),
                         (arg[-90:], "#e6e9f2"), dur)


def steps_panel(frame: int, limit: int | None = None) -> Panel:
    rows = [tool_row(s, frame) for _, s in sorted(S.steps.items())]
    if limit:
        rows = rows[-limit:]
    title = Text.assemble(("⚙ ", f"bold {AMBER}"), ("pasos del agente", f"bold {AMBER}"),
                          (f"  {len(S.steps)}", MUTED))
    return Panel(Group(*rows), title=title, title_align="left", box=ROUNDED, border_style="#5b5130", padding=(0, 1))


def body_md(text: str) -> Markdown:
    return Markdown(text, code_theme="dracula", inline_code_theme="dracula", hyperlinks=False)


class _Tail:
    """Muestra solo las últimas líneas que caben del Markdown renderizado (auto-scroll)."""

    def __init__(self, text: str, height: int):
        self.text, self.height = text, height

    def __rich_console__(self, cons, options):
        lines = cons.render_lines(body_md(self.text), options, pad=False)[-self.height:]
        for i, line in enumerate(lines):
            yield from line
            if i < len(lines) - 1:
                yield Text("\n")


def body_panel(height: int, finished: bool) -> Panel:
    if S.text:
        content = _Tail(S.text, max(1, height - 2))
    else:
        msg = "  usando herramientas…" if S.steps else "  pensando…"
        content = Align.center(Spinner("dots12", text=gradient(msg, (time.monotonic() - start) / 3, bold=False),
                                       style=VIOLET), vertical="middle")
    title = Text.assemble(("✦ ", f"bold {VIOLET}"), ("respuesta", f"bold {VIOLET}"))
    sub = None if finished else Text("▼ desplazamiento automático", style=MUTED)
    return Panel(content, title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 box=HEAVY, border_style=GREEN if finished else VIOLET, padding=(0, 2))


def footer(final: str | None) -> Table:
    files = sum(1 for s in S.steps.values() if any(k in (s.get("name") or "") for k in ("write", "replace", "edit")))
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(ratio=1)
    t.add_column(justify="right")
    left = Text.assemble(" ", chip(f"⬆ {fmt(S.usage.get('input_tokens', 0))} tokens", BLUE), " ",
                         chip(f"⬇ {fmt(S.usage.get('output_tokens', 0))} tokens", VIOLET), " ",
                         chip(f"⚙ {len(S.steps)} pasos", AMBER), " ", chip(f"✎ {files} ediciones", GREEN))
    if final:
        right = Text(final[:80] + " ", style=f"bold {GREEN if final.startswith('✓') else PINK}")
    else:
        right = Text.assemble(("salida → ", MUTED), ("carpetas personales ", BLUE))
    t.add_row(left, right)
    return t


layout = Layout()
compact = console.size.height < 34
layout.split_column(
    Layout(name="logo", size=0 if compact else 4),
    Layout(name="header", size=1),
    Layout(name="gap", size=1),
    Layout(name="task", size=4),
    Layout(name="steps", size=0),
    Layout(name="body", ratio=1),
    Layout(name="footer", size=1),
)
layout["gap"].update(Text(""))

frame, finished_at = 0, None
with Live(layout, console=console, screen=True, refresh_per_second=12):
    while True:
        S.feed()
        finished = done.exists()
        if S.result is not None and finished_at is None:
            finished_at = time.monotonic()
        if finished_at and time.monotonic() - finished_at > 8:
            finished = True  # el servidor no escribió .done; da por terminado
        ok = read(done).startswith("✓") if done.exists() else bool(S.result and S.result.get("status") == "SUCCESS")
        if not compact:
            layout["logo"].update(Padding(logo((time.monotonic() - start) / 6), (1, 0, 0, 0)))
        layout["header"].update(Padding(header(finished, ok), 0, style=f"on {BAR_BG}"))
        layout["task"].update(task_card())
        n_steps = min(len(S.steps), 6)
        layout["steps"].size = n_steps + 2 if n_steps else 0
        if n_steps:
            layout["steps"].update(steps_panel(frame, limit=6))
        body_h = console.size.height - (0 if compact else 4) - 1 - 1 - 4 - (n_steps + 2 if n_steps else 0) - 1
        layout["body"].update(body_panel(body_h, finished))
        layout["footer"].update(Padding(footer(None), 0, style=f"on {BAR_BG}"))
        frame += 1
        if finished:
            time.sleep(0.8)
            break
        time.sleep(0.08)

# Transcripción completa en la consola normal, para poder desplazarse.
estado = read(done).strip() or ("✓ Terminado" if ok else "✖ Terminó sin estado")
console.print(logo(0.0))
console.print(Padding(header(True, estado.startswith("✓")), 0, style=f"on {BAR_BG}"))
console.print()
console.print(task_card())
if S.steps:
    console.print(steps_panel(0))
console.print(Panel(body_md(S.text or "_(sin respuesta de texto)_"), title=Text("✦ respuesta", style=f"bold {GREEN}"),
                    title_align="left", box=HEAVY, border_style=GREEN, padding=(1, 2)))
console.print(Padding(footer(estado), 0, style=f"on {BAR_BG}"))
console.print(Text.assemble(("\n  ", ""), ("⏎ ", f"bold {PINK}"), ("Presiona Enter para cerrar", MUTED)))
try:
    input()
except EOFError:
    pass
