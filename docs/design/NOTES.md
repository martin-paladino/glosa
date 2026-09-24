# Sala de control v2: gestión por excepción

## El concepto en 5 líneas
1. Sigue siendo un multiviewer de sala de control, pero **la sala sana está en calma**: nombre, un LED verde, el tiempo que lleva la charla y sus subtítulos en vivo. Nada más.
2. **El color solo indica estado**: verde es en vivo, ámbar degradada, rojo caída y el contorno gris, inactiva. Todo lo demás (medidor de gasto, VU, teclas, agenda) es gris, así que el rojo y el ámbar se ven desde el otro lado de la sala.
3. Arriba, la barra **Atención** lista solo lo que pide una acción ahora, por gravedad, con una sola tecla por fila (Reconectar). Si no hay nada, dice "Todo en orden".
4. Las métricas aparecen **solo fuera de rango**, como una línea con la acción sugerida al pie del monitor. Los controles completos, todas las métricas con sus topes y el historial de la sala viven en un **cajón lateral** que se abre al hacer clic en el monitor o con las teclas 1–5.
5. La **agenda** es una fila fina por sala: lo pasado se ve apagado y solo se destacan la charla actual y la siguiente. Se ve la línea de "Ahora" y, en el encabezado, el próximo cambio automático. El registro muestra solo alertas, y los informativos quedan a un clic.

## Qué cambió respecto de v1 (lo aprobado)
| Pedido | Cómo quedó |
|---|---|
| Sala sana en calma | Sin chips de idioma y motor, sin VU, sin métricas y sin teclas. Solo nombre, LED, TC y 4 líneas de subtítulo; la primera se desvanece como en el roll-up. |
| Color solo para estado | El medidor de gasto es gris (pasa a ámbar recién al 80 %), las teclas son grises, el cursor de subtítulo es gris y el logo es blanco. |
| Barra Atención | Muestra Sala Comunidad (caída) y Sala Abasto (retraso 6,1 s; el tope es 4 s), cada una con su tecla y el tiempo desde que empezó. El estado "Todo en orden" se ve en `admin.html#calma`. |
| Menos teclas | El monitor solo muestra la acción sugerida, y solo cuando hay un problema. El resto está en el cajón (`shots/shot-admin-drawer.png`, abierto en Sala Abasto). |
| Registro colapsado | Por defecto muestra solo alertas (4). El botón "Todo 14" o "Mostrar también los 10 informativos" agrega el relevo de sesión, el glosario, etc. |
| Sin reloj grande | La hora queda en la franja superior. El próximo cambio automático está en el encabezado de la agenda y en el bloque de Sala Talleres ("en 11:14"). |
| Agenda fina | Filas de 23 px. Lo pasado va al 55 %, la charla actual va rellena y la siguiente con contorno; lo demás es texto apagado. Las charlas en manual van rayadas. |
| Sin video | El fondo es gris oscuro y parejo, sin barras SMPTE ni escenario. La sala caída muestra su último subtítulo congelado. |

## Tipografías y paleta
- **Archivo** comprimida en nombres, rótulos y teclas; **B612** (la tipografía de cabina de Airbus) en todos los subtítulos; **Martian Mono** al 75 % solo para tiempos. Es la misma identidad de v1.
- Superficies: sala `#0D1115`, monitor `#141A1F`, pie de alerta `#191F25`, líneas `#232A31` y `#323B44`.
- Tinta: `#E9ECEE`, `#A3ACB4` y `#7E8993`. Subtítulo cerrado `#F4F6F7` y frase en curso `#8E99A2` (unos 7:1 sobre el monitor).
- Estados: `#35D07F` en vivo, `#F5A524` degradada y `#F0443A` caída. El rojo nunca significa otra cosa.

## Celular
Se aplicó la misma moderación. Arriba van el nombre de la sala, un LED verde con "En vivo" y el reloj en gris; debajo, la charla en una sola línea. La columna de hora de v1 se reemplazó por una **regla de timecode por minuto** ("15:16 ────"): así el texto usa todo el ancho, unos 27 caracteres por línea a 22 px (de 18 a 34 px con A−/A+). La frase en curso va gris, con una barra gris a la izquierda y un cursor. "Volver al vivo" se enciende solo cuando te fuiste para atrás. El tema claro se ve en `room-mobile.html#dia`.

## Cómo se aplica al resto
- **Overlay para la transmisión:** 2 líneas de B612 en roll-up, sin cromo.
- **Lista de salas:** las filas de la agenda con LED y "Ahora / Próxima".
- **Vista de escritorio:** el monitor grande con las otras salas como filas de agenda al costado.
- **Configuración y glosario:** usan el mismo cajón lateral.
- **Regla general:** toda pantalla nueva arranca gris y solo se colorea el estado que pide una acción.

## Archivos y demo
- `admin.html` (1440×900) y `room-mobile.html` (390×844): autocontenidos, con Google Fonts. El JS es de demostración: reloj, subtítulos que se escriben, cajón (clic, 1–5 o Esc), filtro del registro y respuesta "Reconectando…".
- Las capturas se tomaron con Chrome headless a través de DevTools, con viewport exacto. `admin.html#sala-3` abre el cajón al cargar.
- También se probó a 1280×800: la franja superior se compacta y la agenda queda debajo del pliegue.
