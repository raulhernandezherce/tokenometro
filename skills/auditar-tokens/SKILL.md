---
name: auditar-tokens
description: Analiza cuántos tokens ha gastado Claude Code en este equipo (por día, proyecto, modelo y sesión), estima el coste equivalente por API y da consejos concretos para ahorrar. Úsala cuando el usuario pregunte cuánto gasta, por qué se le acaba el límite, cómo ahorrar tokens o qué sesión le salió más cara.
---

# Auditar el consumo de tokens

1. Ejecuta el analizador (solo lee archivos locales, no llama a ninguna API):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/claude_token_audit/auditoria.py" --dias 7
   ```

   Opciones útiles: `--dias 0` (todo el historial), `--proyecto <nombre>`, `--json`.

2. Resume al usuario en pocas líneas: tokens leídos y generados, acierto de caché, coste equivalente y el proyecto o la sesión que más gasta.

3. Explica cada consejo del bloque «Consejos» con una acción concreta para **su** caso. Prioriza por ahorro:
   - **Contexto grande** → `/compact` al cerrar subtareas, `/clear` al cambiar de tema.
   - **Caché baja** → no cambiar de modelo ni de esfuerzo a mitad de sesión; no editar CLAUDE.md con la sesión abierta.
   - **Todo en el modelo grande** → subagentes con Haiku o Sonnet para búsquedas, lecturas y tests.
   - **Mucho más leído que generado** → CLAUDE.md más corto, menos servidores MCP activos.

4. Aclara que el coste es una estimación a precio de API: con una suscripción Pro o Max no se paga así, pero sirve para comparar sesiones y ver qué agota el límite.
