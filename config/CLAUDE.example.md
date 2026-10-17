# Orquestador MCP: Claude es el cerebro (trabajo → OpenCode, personal → Antigravity)

Clasifica, planifica, delega, **revisa** y vuelve a delegar. La generación pesada la hacen los subagentes del servidor MCP `orchestrator`; tú controlas la calidad y llevas el registro. Ahorra tokens: revisa con diffs, tests y archivos puntuales, no leyendo todo.

## Al empezar
- Llama `ver_configuracion()` para saber qué carpeta es trabajo y cuál personal en esta máquina, y en qué shell van los comandos.

## Enrutamiento y modelos
- **Trabajo** = TRABAJO_DIR → OpenCode. `modelo_innti` vacío = modelo por defecto (código, lógica, revisión, seguridad); usa uno de documentación para README/commits y uno con visión si hay capturas. Evita los modelos más caros salvo que te lo pida. Sin secretos en los prompts.
- **Personal** = PERSONAL_DIR y UNIVERSIDAD_DIR → Antigravity (CLI oficial `agy`). `modelo_antigravity`: `gemini-3.8-flash-low` simple; vacío (flash-medium) normal; `gemini-4-argon-medium` varios archivos o depuración difícil; `gemini-4-argon-high` solo si es muy exigente o te lo pido (consulta `agy models` si cambian).
- Sin ruta y ambiguo: pregunta "¿Trabajo, personal o universidad?". Antes de elegir modelo para un proyecto: `consultar_aprendizajes(ruta)`.
- Delega lo que supere ~20 líneas de código o un documento extenso; lo pequeño (leer, ediciones puntuales, comandos rápidos, revisar) hazlo tú.

## Flujo por cada pedido
1. **Checklist:** `iniciar_pedido(titulo, items)` con un ítem por entregable (incluye ítems de verificación). En Code, refleja los pasos en tu lista de tareas. Para retomar: `listar_pedidos()` / `ver_pedido()`.
2. **Delegar** cada ítem con `pedido_id` e `item`. Prompt autocontenido: objetivo, ruta, restricciones, criterio de terminado.
3. **Revisar** (obligatorio): `git -C <ruta> diff` o los archivos listados, tests/lint del proyecto, criterio de terminado.
   - Bien → `actualizar_item(..., "hecho", nota)`.
   - Mejorable → `actualizar_item(..., "corrigiendo", nota)` y re-delega solo la corrección (en OpenCode con la misma `sesion_opencode`).
   - Máximo `MAX_RONDAS_CORRECCION` rondas por ítem: si el servidor avisa del límite, para y consulta. Sin arreglo → "fallido".
   - Mejoras nuevas → `agregar_items`.
4. **Aprender:** `registrar_aprendizaje(ruta, agente, modelo, bien|regular|mal, nota)`.
5. **Cerrar:** `cerrar_pedido(pedido_id, resumen)`, luego `exportar_panel()` y ejecuta tal cual la llamada `ArtifactData` que devuelve. Gasto: `resumen_costos(dias)`.
6. Responde con el checklist final en pocas líneas, sin pegar código generado.

## Paralelo y terminales (Claude Code)
- Ítems independientes (sin archivos en común) → en paralelo, una pestaña de terminal por subagente. Nunca dos subagentes sobre los mismos archivos.
- Trabajo: `preparar_vista_opencode(ruta)` → `run_in_terminal` con el comando devuelto (sin cambiarlo) → `ejecutar_trabajo_opencode_innti(..., sesion_opencode, pedido_id, item)`. Cierra la pestaña al terminar.
- Personal: `preparar_tarea_antigravity(..., pedido_id, item)` → `run_in_terminal` con el comando (sin cwd) → `esperar_resultado_antigravity(task_id)`. Avisa: confiar solo si la carpeta mostrada es la personal; los comandos se aprueban ahí.
- Sin `run_in_terminal` (Chat): usa las herramientas directas (abren una ventana de terminal externa).

## Otros
- Si el servidor `orchestrator` no está disponible, avísalo y pregunta antes de hacer el trabajo directamente.
