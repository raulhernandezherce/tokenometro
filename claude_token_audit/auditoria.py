"""Auditoría de tokens de Claude Code a partir de los transcripts locales (JSONL).

Lee ~/.claude/projects/**/*.jsonl, deduplica las respuestas del modelo,
suma tokens por día, proyecto, modelo y sesión, estima el coste y detecta
los patrones que más tokens desperdician.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRECIOS_POR_DEFECTO = Path(__file__).with_name("precios.json")


# ---------------------------------------------------------------- lectura

@dataclass
class Llamada:
    """Una respuesta del modelo (una petición a la API)."""

    id: str
    modelo: str
    fecha: datetime
    sesion: str
    proyecto: str
    subagente: bool
    input: int = 0
    output: int = 0
    cache_5m: int = 0
    cache_1h: int = 0
    cache_read: int = 0

    @property
    def contexto(self) -> int:
        """Tokens que el modelo tuvo que leer en esta llamada."""
        return self.input + self.cache_5m + self.cache_1h + self.cache_read


def directorio_por_defecto() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _fecha(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None


def _llamada_desde_linea(d: dict, proyecto_carpeta: str) -> Llamada | None:
    if d.get("type") != "assistant":
        return None
    msg = d.get("message") or {}
    uso = msg.get("usage")
    modelo = msg.get("model") or ""
    if not uso or not modelo or modelo.startswith("<"):
        return None
    fecha = _fecha(d.get("timestamp"))
    if fecha is None:
        return None
    creacion = uso.get("cache_creation") or {}
    total_creacion = int(uso.get("cache_creation_input_tokens") or 0)
    c1h = int(creacion.get("ephemeral_1h_input_tokens") or 0)
    c5m = int(creacion.get("ephemeral_5m_input_tokens") or 0)
    if c1h + c5m == 0:
        c5m = total_creacion
    cwd = d.get("cwd") or ""
    return Llamada(
        id=msg.get("id") or d.get("requestId") or d.get("uuid") or "",
        modelo=modelo,
        fecha=fecha,
        sesion=d.get("sessionId") or "",
        proyecto=Path(cwd).name if cwd else proyecto_carpeta,
        subagente=bool(d.get("isSidechain")),
        input=int(uso.get("input_tokens") or 0),
        output=int(uso.get("output_tokens") or 0),
        cache_5m=c5m,
        cache_1h=c1h,
        cache_read=int(uso.get("cache_read_input_tokens") or 0),
    )


def leer_llamadas(raiz: Path) -> list[Llamada]:
    """Lee todos los JSONL y devuelve una llamada por respuesta del modelo.

    Claude Code escribe una línea por bloque de contenido, todas con el mismo
    `message.id`; sin deduplicar, el consumo saldría multiplicado.
    """
    por_id: dict[str, Llamada] = {}
    for ruta in sorted(raiz.rglob("*.jsonl")):
        carpeta = ruta.relative_to(raiz).parts[0] if ruta.parent != raiz else ruta.parent.name
        try:
            with ruta.open(encoding="utf-8", errors="replace") as f:
                for linea in f:
                    if '"usage"' not in linea:
                        continue
                    try:
                        d = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    ll = _llamada_desde_linea(d, carpeta)
                    if ll is None or not ll.id:
                        continue
                    previa = por_id.get(ll.id)
                    if previa is None or ll.output >= previa.output:
                        por_id[ll.id] = ll
        except OSError:
            continue
    return sorted(por_id.values(), key=lambda x: x.fecha)


# ---------------------------------------------------------------- precios

def cargar_precios(ruta: Path | None = None) -> dict[str, dict[str, float]]:
    datos = json.loads((ruta or PRECIOS_POR_DEFECTO).read_text(encoding="utf-8"))
    return {k: v for k, v in datos.items() if not k.startswith("_")}


def precio_de(modelo: str, precios: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Busca el precio por prefijo más largo (claude-opus-4-8 -> claude-opus-4)."""
    mejor = None
    for clave in precios:
        if modelo.startswith(clave) and (mejor is None or len(clave) > len(mejor)):
            mejor = clave
    return precios[mejor] if mejor else None


def coste(ll: Llamada, precios: dict[str, dict[str, float]]) -> float:
    p = precio_de(ll.modelo, precios)
    if p is None:
        return 0.0
    return (
        ll.input * p["input"]
        + ll.output * p["output"]
        + ll.cache_5m * p["cache_5m"]
        + ll.cache_1h * p["cache_1h"]
        + ll.cache_read * p["cache_read"]
    ) / 1_000_000


# ---------------------------------------------------------------- análisis

@dataclass
class Grupo:
    llamadas: int = 0
    input: int = 0
    output: int = 0
    cache_escritura: int = 0
    cache_read: int = 0
    coste: float = 0.0
    contexto_max: int = 0
    modelos: set = field(default_factory=set)

    def sumar(self, ll: Llamada, c: float) -> None:
        self.llamadas += 1
        self.input += ll.input
        self.output += ll.output
        self.cache_escritura += ll.cache_5m + ll.cache_1h
        self.cache_read += ll.cache_read
        self.coste += c
        self.contexto_max = max(self.contexto_max, ll.contexto)
        self.modelos.add(ll.modelo)

    @property
    def leidos(self) -> int:
        return self.input + self.cache_escritura + self.cache_read

    @property
    def acierto_cache(self) -> float:
        return self.cache_read / self.leidos if self.leidos else 0.0

    @property
    def contexto_medio(self) -> float:
        return self.leidos / self.llamadas if self.llamadas else 0.0

    def a_dict(self) -> dict:
        return {
            "llamadas": self.llamadas,
            "input": self.input,
            "output": self.output,
            "cache_escritura": self.cache_escritura,
            "cache_lectura": self.cache_read,
            "acierto_cache": round(self.acierto_cache, 4),
            "contexto_medio": round(self.contexto_medio),
            "contexto_max": self.contexto_max,
            "coste_usd": round(self.coste, 4),
            "modelos": sorted(self.modelos),
        }


@dataclass
class Informe:
    total: Grupo
    por_dia: dict[str, Grupo]
    por_proyecto: dict[str, Grupo]
    por_modelo: dict[str, Grupo]
    por_sesion: dict[str, Grupo]
    subagentes: Grupo
    consejos: list[str]


def analizar(llamadas: list[Llamada], precios: dict, dias: int | None = None,
             proyecto: str | None = None, ahora: datetime | None = None) -> Informe:
    ahora = ahora or datetime.now(timezone.utc)
    if dias:
        limite = ahora - timedelta(days=dias)
        llamadas = [ll for ll in llamadas if ll.fecha >= limite]
    if proyecto:
        llamadas = [ll for ll in llamadas if proyecto.lower() in ll.proyecto.lower()]

    total, sub = Grupo(), Grupo()
    por_dia: dict[str, Grupo] = defaultdict(Grupo)
    por_proy: dict[str, Grupo] = defaultdict(Grupo)
    por_mod: dict[str, Grupo] = defaultdict(Grupo)
    por_ses: dict[str, Grupo] = defaultdict(Grupo)
    for ll in llamadas:
        c = coste(ll, precios)
        total.sumar(ll, c)
        por_dia[ll.fecha.astimezone().strftime("%Y-%m-%d")].sumar(ll, c)
        por_proy[ll.proyecto].sumar(ll, c)
        por_mod[ll.modelo].sumar(ll, c)
        por_ses[ll.sesion].sumar(ll, c)
        if ll.subagente:
            sub.sumar(ll, c)

    inf = Informe(total, dict(por_dia), dict(por_proy), dict(por_mod), dict(por_ses), sub, [])
    inf.consejos = consejos(inf)
    return inf


def consejos(inf: Informe) -> list[str]:
    """Reglas sencillas sobre los patrones que más tokens gastan."""
    out: list[str] = []
    t = inf.total
    if not t.llamadas:
        return ["No hay llamadas en el periodo elegido."]

    if t.acierto_cache < 0.80:
        out.append(
            f"Acierto de caché del {t.acierto_cache:.0%} (lo normal en Claude Code es >90 %). "
            "Algo invalida la caché: cambiar de modelo o de esfuerzo a mitad de sesión, "
            "editar CLAUDE.md durante la sesión o volver tras más de 1 h sin actividad."
        )

    if t.cache_escritura > t.cache_read * 0.5:
        out.append(
            "Escribes en caché casi tanto como lees: muchas sesiones cortas o reinicios. "
            "Agrupa tareas relacionadas en la misma sesión en vez de abrir una nueva cada vez."
        )

    grandes = [s for s, g in inf.por_sesion.items() if g.contexto_medio > 150_000 and g.llamadas >= 20]
    if grandes:
        out.append(
            f"{len(grandes)} sesión(es) trabajan con más de 150 k tokens de contexto por llamada. "
            "Usa /compact al terminar cada subtarea o /clear al cambiar de tema: "
            "cada llamada relee todo el historial."
        )

    if inf.por_modelo:
        principal, g = max(inf.por_modelo.items(), key=lambda kv: kv[1].coste)
        cuota = g.coste / t.coste if t.coste else 0
        if cuota > 0.9 and ("opus" in principal or "fable" in principal) and inf.subagentes.llamadas < t.llamadas * 0.1:
            out.append(
                f"El {cuota:.0%} del coste es {principal} y casi no usas subagentes. "
                "Delega búsquedas, lecturas y tests a un subagente con Haiku o Sonnet."
            )

    ratio = t.output / t.leidos if t.leidos else 0
    if ratio < 0.003 and t.llamadas > 50:
        out.append(
            f"Por cada token que genera, el modelo lee {1 / ratio:,.0f}. "
            "El coste está en el contexto, no en las respuestas: recorta CLAUDE.md, "
            "desactiva servidores MCP que no uses y evita pegar archivos enteros."
        )

    if not out:
        out.append("Buen uso: caché alta y contexto contenido. Nada urgente que cambiar.")
    return out


# ---------------------------------------------------------------- salida

def _k(n: float) -> str:
    for unidad, div in (("M", 1_000_000), ("k", 1_000)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unidad}"
    return f"{n:.0f}"


def _barra(valor: float, maximo: float, ancho: int = 24) -> str:
    if maximo <= 0:
        return ""
    lleno = valor / maximo * ancho
    return "█" * int(lleno) + ("▌" if lleno - int(lleno) >= 0.5 else "")


def _tabla(titulo: str, grupos: dict[str, Grupo], top: int, ordenar_por_clave: bool = False) -> list[str]:
    if not grupos:
        return []
    items = sorted(grupos.items()) if ordenar_por_clave else sorted(grupos.items(), key=lambda kv: -kv[1].coste)
    items = items[-top:] if ordenar_por_clave else items[:top]
    maximo = max(g.coste for _, g in items) or 1
    ancho = min(32, max(len(k) for k, _ in items))
    lineas = [f"\n{titulo}", "─" * (ancho + 60)]
    for k, g in items:
        lineas.append(
            f"{k[:ancho]:<{ancho}}  {g.coste:>8.2f} $  {_k(g.leidos):>7} leídos  "
            f"{_k(g.output):>6} gen.  caché {g.acierto_cache:>4.0%}  {_barra(g.coste, maximo)}"
        )
    return lineas


def informe_texto(inf: Informe, top: int = 10) -> str:
    t = inf.total
    lineas = [
        "claude-token-audit · consumo de Claude Code",
        "═" * 60,
        f"Llamadas al modelo   {t.llamadas:,}",
        f"Tokens leídos        {_k(t.leidos)}  (caché: {_k(t.cache_read)} lecturas, {_k(t.cache_escritura)} escrituras)",
        f"Tokens generados     {_k(t.output)}",
        f"Acierto de caché     {t.acierto_cache:.1%}",
        f"Coste equivalente    {t.coste:,.2f} $  (precio API; con suscripción no lo pagas así)",
        f"Subagentes           {inf.subagentes.llamadas:,} llamadas · {inf.subagentes.coste:,.2f} $",
    ]
    lineas += _tabla("Por día", inf.por_dia, top, ordenar_por_clave=True)
    lineas += _tabla("Por proyecto", inf.por_proyecto, top)
    lineas += _tabla("Por modelo", inf.por_modelo, top)
    ses = {k[:8]: g for k, g in inf.por_sesion.items()}
    lineas += _tabla("Sesiones más caras", ses, min(top, 5))
    lineas += ["", "Consejos", "─" * 60]
    lineas += [f"• {c}" for c in inf.consejos]
    return "\n".join(lineas)


def informe_json(inf: Informe) -> dict:
    return {
        "total": inf.total.a_dict(),
        "subagentes": inf.subagentes.a_dict(),
        "por_dia": {k: g.a_dict() for k, g in sorted(inf.por_dia.items())},
        "por_proyecto": {k: g.a_dict() for k, g in inf.por_proyecto.items()},
        "por_modelo": {k: g.a_dict() for k, g in inf.por_modelo.items()},
        "por_sesion": {k: g.a_dict() for k, g in inf.por_sesion.items()},
        "consejos": inf.consejos,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-token-audit",
        description="Audita cuántos tokens gasta Claude Code y dónde puedes ahorrar.",
    )
    p.add_argument("--dir", type=Path, default=None, help="carpeta de proyectos (por defecto ~/.claude/projects)")
    p.add_argument("--dias", type=int, default=30, help="periodo a analizar (0 = todo). Por defecto 30")
    p.add_argument("--proyecto", help="filtra por nombre de proyecto")
    p.add_argument("--top", type=int, default=10, help="filas por tabla")
    p.add_argument("--precios", type=Path, help="tabla de precios JSON alternativa")
    p.add_argument("--json", action="store_true", help="salida JSON")
    a = p.parse_args(argv)

    raiz = a.dir or directorio_por_defecto()
    if not raiz.is_dir():
        print(f"No encuentro {raiz}. ¿Has usado Claude Code en este equipo?", file=sys.stderr)
        return 2
    inf = analizar(leer_llamadas(raiz), cargar_precios(a.precios), a.dias or None, a.proyecto)
    if a.json:
        print(json.dumps(informe_json(inf), ensure_ascii=False, indent=2))
    else:
        print(informe_texto(inf, a.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
