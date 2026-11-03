# Edwin's Budget

PWA de presupuesto personal, optimizada para **Galaxy Z Fold**.

## Instalar en el teléfono
1. Publica la carpeta con GitHub Pages (Settings → Pages → rama `main`, carpeta `/`).
2. Abre la URL en Chrome del Fold → menú ⋮ → **Agregar a pantalla principal / Instalar app**.
3. Mantén presionado el ícono para ir directo a **Plan**, **Deudas** o **Resumen**.

## Experiencia en el Fold
| Estado | Qué ves |
|---|---|
| Cerrado (pantalla exterior) | Una columna, barra de navegación abajo (zona del pulgar), desliza ← → entre secciones |
| Abierto (pantalla interior) | Menú lateral + tarjetas en 2 columnas (3 en horizontal) |
| Modo Flex (doblado a medias) | Arriba: dinero disponible y deuda; abajo: controles |
| Al plegar/desplegar | La app se reacomoda sola sin perder lo que estabas haciendo |

## Otras mejoras
- Los datos **se guardan** en el teléfono (antes se perdían al cerrar).
- Editar montos con una hoja inferior y teclado numérico, con botones ±5 / ±25.
- Funciona **sin internet** (service worker).
- Exportar / importar respaldo en **Resumen → Respaldo de datos**.
- Vibración ligera al tocar, botones más grandes (≥ 44 px), respeta la barra de gestos de Android.

`pwa-budget.zip` contiene la versión original.
