# Sistema visual de Glosa: "Sala de control" v2

Glosa se ve como el multiviewer de una sala de control de TV. Una sala sana está **en calma**: su nombre, una luz verde y sus subtítulos. Todo lo demás es gris. El color aparece solo cuando algo pide atención, y el rojo se ve desde el otro lado de la sala.

Todo está en un solo archivo, `glosa/web/static/css/glosa.css`: CSS puro, sin build. Sirve para las vistas de la audiencia (ya aplicadas) y para el panel de producción (clases listas para la Tarea 12).

| Archivo | Qué es |
|---|---|
| `admin.html` | **Maqueta aprobada** del panel, 1440 × 900, autocontenida. `#sala-3` abre el cajón; `#calma` muestra "Todo en orden". |
| `room-mobile.html` | **Maqueta aprobada** de la sala en el celular, 390 × 844. `#dia` muestra el tema claro. |
| `NOTES.md` | Las notas del diseñador: el concepto, qué cambió respecto de v1 y cómo se aplica al resto. |
| `shots/` | Las capturas aprobadas (`shot-admin.png`, `shot-admin-drawer.png`, `shot-mobile.png`). |
| `admin-classes.html` | La maqueta del panel armada **solo con las clases de `glosa.css`**. Es la referencia de markup para la Tarea 12. |
| `overlay.html` | El overlay de TV (Tarea 14) con las clases `.overlay`, sobre fondo a cuadros y sobre video claro. |

Las maquetas abren con doble clic, sin servidor. Las capturas de la app real están en `docs/screenshots/`.

---

## 1. Reglas

1. **Gestión por excepción.** Una sala sana muestra su nombre, su luz y su texto; nada más. Las métricas aparecen solo fuera de rango, como una línea al pie del monitor con la acción sugerida. Los controles completos, las métricas con sus topes y el historial viven en el cajón lateral. La barra **Atención** lista solo lo que pide una acción ahora, por gravedad; si no hay nada, dice "Todo en orden".
2. **El color codifica solo el estado.** Verde es en vivo, ámbar degradada y rojo caída; el contorno gris es inactiva. El medidor de gasto, el VU, las teclas, el cursor, la agenda y la marca son grises. **El rojo nunca significa otra cosa.** Una tecla se pinta solo cuando es la acción sugerida para una sala con problemas, y lleva el color de ese estado.
3. **Los subtítulos son el producto.** Ocupan el vidrio (`--glass`), a todo el ancho, en B612. Nada compite con ellos.
4. **Nada se mueve cuando llega texto.** El borde izquierdo queda fijo; historial y vivo van al mismo tamaño, así que un párrafo que pasa al historial no se corre ni una letra. La frase en curso cambia de color al cerrarse, no de forma.
5. **Una sola cosa se mueve sola:** el cursor de la frase en curso. En la audiencia el reloj marca HH:MM para no sumar un segundero. `prefers-reduced-motion` apaga el cursor y las transiciones.
6. **Toda pantalla nueva arranca gris** y solo se colorea el estado que pide una acción.
7. **La estructura es información.** Los filetes separan zonas, no decoran. No hay sombras salvo en lo que flota ("Volver al vivo"), no hay degradés de adorno y no hay mayúsculas sostenidas en rótulos. El único degradé es funcional: el desvanecido de arriba del roll-up.
8. **Formato rioplatense:** voseo ("Elegí una sala"), coma decimal ("6,1 s", "US$ 41,80"), horas de 24 h, mayúscula solo al principio de la frase y en nombres propios.
9. **Foco siempre visible:** contorno de 2 px en `--ink` (3 px en alto contraste, también con `prefers-contrast: more`). Teclas de lectura de 46 px de alto en el celular.

## 2. Cómo se incluye

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,400..800&amp;family=B612:wght@400;700&amp;family=Martian+Mono:wdth,wght@75,400&amp;display=swap">
<link rel="stylesheet" href="/static/css/glosa.css">
```

- `base.html` ya lo hace. El reloj de la casa es `static/js/clock.js`: llena cada `[data-clock]` con la hora local (`data-clock="hms"` para HH:MM:SS) y le saca el `hidden`.
- **Capas**, de menor a mayor prioridad: `glosa.reset`, `glosa.tokens`, `glosa.base`, `glosa.components`, `glosa.captions`, `glosa.audience`, `glosa.admin`, `glosa.views`. Cualquier CSS de la página que no esté en una capa le gana siempre.
- **Las vistas se acomodan al contenedor, no a la ventana.** `body` es el contenedor `glosa` (`container: glosa / inline-size`). Para mostrar una vista dentro de un marco, dale al marco `container: glosa / inline-size`.
- **Cortes:** 64rem (1024 px) pasa la sala a escritorio. 40rem (640 px) es el corte angosto (bilingüe interlineal, pared de una columna). El panel se compacta a 86,25rem (1380 px) y pasa a dos columnas a 73,75rem (1180 px).
- Se necesita un navegador de 2024 en adelante: `light-dark()`, `color-mix()`, `:has()`, container queries y subgrid.

## 3. Tipografías

| Rol | Familia | Dónde |
|---|---|---|
| **Rótulos** | Archivo, angosta (`font-stretch` de 80 a 90 %) | Nombres de sala, títulos de charla, rótulos, teclas, toda la interfaz |
| **Subtítulos** | B612 (la tipografía de cabina de Airbus) | Todos los subtítulos: audiencia, monitores, cajón y overlay. También la marca `glosa`. |
| **Tiempos** | Martian Mono al 75 % de ancho | Solo tiempos: reloj, reglas de minuto, horas de "Próxima", timecode, cuenta regresiva, registro, `kbd` |

- **B612** se diseñó para leerse de reojo, con vibración y con poca luz: cada letra se distingue de sus vecinas. Un subtítulo se lee exactamente así. Mide 0,535em por carácter en promedio.
- **Archivo angosta** entra en franjas de 46 px sin cortar nombres largos. Cuanto más grande el texto, más angosta: `--narrow-1` (90 %) en la interfaz, `--narrow-2` (86 %) en títulos de charla, `--narrow-3` (80 %) en nombres de sala.
- **Martian Mono** no se usa para nada que no sea un tiempo.
- Las siglas de idioma (EN, ES) van en mayúscula porque son siglas.

### Escala

Interfaz, en rem: **11 · 12,5 · 13,5 · 15 · 17 · 19 · 25 · 32 · 44 px** (`--step--2` a `--step-6`). La base del panel es 13,5 px (`--step-0`).

Los subtítulos tienen **su propio eje**, porque los cambia quien lee:

| Token | Celular | Escritorio (≥ 64rem) |
|---|---|---|
| `--caption-base` | 22 px (~27 caracteres por renglón a 390 px) | `clamp(24px, 12px + 1.3cqi, 36px)`; en bilingüe, `clamp(20px, 8px + 1.05cqi, 28px)` |
| `--caption-scale` (A−/A+) | 0.8 · 0.9 · 1 · 1.15 · 1.3 · 1.5 · 1.75 (de 18 a 38 px en el celular) | igual |
| `--caption-leading` | 1.45 | 1.45 |
| `--caption-measure` | 30em (~56 caracteres de B612) | 30em |
| `--caption-weight` | 400 (700 en alto contraste) | |

`--caption-size` se calcula en `.transcript` (no en `:root`) para que tome el `--caption-base` de la vista. Si una vista necesita otro tamaño, pisá `--caption-base`, no `font-size`.

## 4. Color

Los componentes usan roles, nunca hex. Cada rol se resuelve por tema con `light-dark()`.

| Rol | Oscuro | Claro ("día") | Uso |
|---|---|---|---|
| `--room` | `#0D1115` | `#DDE2E6` | La sala: fondo de página |
| `--panel` | `#141A1F` | `#EDF0F2` | Monitor, franjas, barras, paneles |
| `--panel-2` | `#191F25` | `#E3E7EA` | Pie de alerta, teclas del panel |
| `--drawer` | `#12171C` | `#F3F5F6` | Cajón lateral |
| `--well` | `#0F1418` | `#F7F8F9` | Hueco de subtítulos dentro del cajón |
| `--glass` | `#000000` | `#FFFFFF` | El vidrio: donde se leen los subtítulos |
| `--key` / `--key-hover` | `#1B2127` / `#222930` | `#FFFFFF` / `#F4F6F7` | Teclas de lectura |
| `--sel` | `#29313A` | `#D3D9DE` | Lo actual: la charla en curso en la agenda, la sala abierta en la lista |
| `--line` / `--line-2` | `#232A31` / `#323B44` | `#C6CDD3` / `#A9B2BA` | Filetes y bordes |
| `--ink` / `--ink-2` / `--ink-3` | `#E9ECEE` / `#A3ACB4` / `#7E8993` | `#0D1114` / `#3F4952` / `#5A656F` | Texto: principal, secundario, de reojo |
| `--cap` | `#F4F6F7` | `#0A0D10` | Frase cerrada |
| `--cap-live` | `#8E99A2` | `#56616B` | Frase en curso |

**Estados.** Son el único color de la interfaz, y siempre van con una palabra (salvo "en vivo" en el monitor del panel, que es el caso sano: luz llena y ninguna palabra).

| Estado | Rol | Oscuro | Claro | Alto contraste | Palabra en el panel | Palabra para el público |
|---|---|---|---|---|---|---|
| En vivo | `--on` | `#35D07F` | `#0F8A4A` | `#3DF08F` | (ninguna) | En vivo |
| Degradada | `--warn` | `#F5A524` | `#8A5300` | `#FFB23F` | Degradada | En vivo (el público no lo ve) |
| Caída | `--fault` | `#F0443A` | `#B42318` | `#FF6B61` | Caída | Reconectando |
| Inactiva | contorno `--ink-3` | | | | Abre a las 15:29 | Entre charlas / la hora de la próxima |

El texto sobre una tecla ámbar o roja es `--on-warn` / `--on-fault` (casi negro en oscuro: 9,1:1 y 5,3:1; blanco en claro: 6,3:1 y 6,6:1).

**Contrastes medidos (WCAG).** En oscuro, sobre el monitor: `--ink` 14,8:1, `--ink-2` 7,6:1, `--ink-3` 4,9:1; sobre el vidrio, `--cap` 19,4:1 y la frase en curso 7,2:1. En claro: `--ink-3` 5,2:1 sobre el panel y la frase en curso 6,3:1 sobre el vidrio. El ámbar y el rojo como texto pasan 4,5:1 sobre el panel en los dos temas. Alto contraste: frase en curso 13,4:1 sobre negro.

### Temas

- Sin atributo, sigue al sistema (`prefers-color-scheme`); **el diseño base es el oscuro**, que es lo que se usa si el sistema no pide claro. Con `prefers-contrast: more`, alto contraste.
- `data-theme="light" | "dark" | "contrast"` en `<html>` fuerza uno; `room.js` lo guarda en `localStorage` (`glosa.theme`). También funciona en cualquier elemento, para mostrar un tema dentro de otro.
- **El panel es siempre oscuro:** `.admin` fija `color-scheme: dark`.
- **Alto contraste:** negro, blanco y los tres estados más vivos, sin transparencias, filetes de 2 px, barra del vivo de 5 px, subtítulos en B612 Bold. La frase en curso no se apaga: queda en gris claro (13,4:1).
- `forced-colors` (alto contraste de Windows) también está cubierto.

### Marca del evento

La configuración sigue inyectando `--brand-primary`, `--brand-accent`, `--brand-on-primary` y `--brand-on-accent` (`pages._brand_css`), pero **la v2 no las usa en la interfaz en vivo**: el color está reservado para el estado, y un primario verde o rojo del evento se leería como "en vivo" o "caída". La identidad del evento va por el logo (`<img class="event-logo">`, 24 px de alto) o por el nombre (`.event-name`). Las variables quedan disponibles para piezas impresas o proyectadas, como la página del QR.

## 5. Cómo se ve el texto en vivo

La regla más importante del sistema. `room.js` escribe este markup (las clases salen de su mapa `CLASS`; la lógica encuentra todo por `data-*`):

```html
<ol class="history" role="log">
  <li class="line">
    <time class="line__time">15:16</time>          <!-- solo en el primer párrafo de cada minuto -->
    <p class="line__text">Esto es lo que gastamos en infraestructura el año pasado, mes por mes.</p>
  </li>
</ol>
<div class="line line--live" data-live aria-live="off">
  <time class="line__time"></time>                 <!-- oculta: la regla aparece al pasar al historial -->
  <p class="line__text">
    <span class="phrase" data-seg="41">El control plane costó esto, solo en marzo.</span>
    <span class="phrase phrase--open" data-seg="42" data-open>Y eso sin contar el tráfico entre zonas,</span>
  </p>
</div>
```

1. **Regla de timecode por minuto.** La hora no va en un margen: es una regla ("15:16 ────") arriba del primer párrafo de cada minuto, así el texto usa todo el ancho.
2. **Frase en curso:** `.phrase--open`, en `--cap-live`, termina en un **cursor** de bloque gris que parpadea (1,1 s, en pasos). El cursor es un inline vacío con relleno, no un inline-block: así nunca queda solo en un renglón nuevo.
3. **El bloque en vivo** lleva una barra gris a la izquierda, **fuera** de la columna de texto (`inset-inline-start: -12px`), así el texto no se corre cuando el párrafo pasa al historial.
4. **Frase cerrada:** se le saca `phrase--open`; el color pasa a `--cap` en 400 ms. Cambia solo el color, nunca el peso ni el tamaño.
5. Siempre hay **una sola** `.phrase--open`, y es la última. Lo nuevo se agrega al final: nunca se reescribe lo que ya está en pantalla.
6. En texto en vivo **no se usan** `text-wrap: balance` ni `pretty`, ni texto centrado, ni corte de palabras automático.
7. **Roll-up:** el vidrio se desvanece en sus 64 px de arriba (en el monitor del panel, en su tercio de arriba). En las páginas enfrentadas del bilingüe (desde 40rem) no, porque arriba van los rótulos de cada página; en el bilingüe interlineal del celular, sí.
8. **Auto-scroll:** si el lector sube más de 48 px, se pausa y aparece `.to-live` ("Volver al vivo"), una tecla gris con borde claro que flota abajo del vidrio. Al tocarla, vuelve abajo y se esconde.
9. **Bilingüe:** dos páginas enfrentadas (`.transcript--facing` con dos `.page`), original a la izquierda, cada una con su rótulo pegado arriba (`.line--head`). Abajo de 40rem, el original queda como un renglón chico y gris arriba del vivo de la traducción.

### Overlay de TV

- `/overlay/{sala}?lang=es&lines=2&size=48` (Tarea 14): `<body class="overlay-page">` (fondo transparente, caja abajo y al centro) y la caja `.overlay`, con `--overlay-lines` y `--overlay-size` en un `style`.
- **Sin cromo:** solo la caja de negro al 78 % y el texto en B612. **Ancho fijo** (`--overlay-measure: 22.5em`, ≈ 42 caracteres) y **alto fijo** (N renglones de 1,35em). El texto está anclado abajo y lo viejo sale por arriba, como en la tele. La caja no cambia de tamaño mientras llega texto.
- Colores fijos, no dependen del tema: texto `#F4F6F7` (11,7:1 aun sobre video blanco) y frase en curso `#C4CBD1` (7:1). Sin cursor.

## 6. Componentes

Los estados usan siempre las mismas cuatro palabras en los modificadores: `--live`, `--degraded`, `--down`, `--idle`. `RoomStatus.state` se traduce así: `green → live`, `yellow → degraded`, `red → down`, `idle → idle`.

### Compartidos

| Pieza | Clases |
|---|---|
| Luz de estado | `.led` + `--live`, `--degraded`, `--down`, `--idle` (contorno), `--info` (contorno tenue, para el registro) |
| Estado con palabra | `.status` + `--live`, `--degraded`, `--down`, `--idle`. Sana, la palabra va en gris; degradada y caída van en su color. |
| Marca | `.wordmark` (el bloque blanco `glosa`), `.event-logo`, `.event-name` |
| Reloj | `.clock` (+ `--big` en el panel), con `data-clock` y `clock.js` |
| Charla | `.talk-title`, `.talk-meta`, `.direction` (EN → ES, con `<abbr>` y la flecha SVG) |
| Teclas del panel | `.btn` (gris, 38 px) + `--compact` (32 px), `--degraded` y `--down` (la acción sugerida, rellena), `--link` (texto subrayado) |
| Selector segmentado | `.segmented` con `<button aria-pressed>` |
| Panel | `.panel` (+ `--sunk`: fondo de sala), `.panel__head` (46 px), `.panel__title` |
| Tiempos | `.tc` (Martian Mono al 75 %), `kbd` |
| Otros | `.visually-hidden`, `.num` |

### Subtítulos

| Pieza | Clases |
|---|---|
| Transcripción | `.transcript` (+ `--facing`), `.page`, `.history`, `.line`, `.line--live`, `.line--head`, `.line__time`, `.line__text`, `.line__label` |
| Frases | `.phrase`, `.phrase--open` |
| Monitor del panel | `.monitor__cc` (roll-up de 4 renglones, 17 px) + `--frozen` (último subtítulo en gris, con `<small class="tc">`) |
| Overlay | `.overlay-page`, `.overlay`, `.overlay__window`, `.overlay__text` |

### Audiencia

| Pieza | Clases |
|---|---|
| Encabezado de sala | `.room-head`, `__bar` (marca, evento y "Cambiar de sala"), `__brand`, `__event`, `__rooms` (solo celular), `__umd` (nombre, estado y reloj en celular), `__name`, `__talk`, `__meta`; `.room__clock` (el reloj en escritorio, a la derecha de las teclas) |
| Teclas de lectura | `.controls`, `.control` (46 px; 40 px en escritorio), `.control--select`, `.control--fullscreen` (solo escritorio), `.control-group`, `.control__word` (se oculta abajo de 23,5rem) |
| Volver al vivo | `.to-live` (+ `.room__to-live` para flotar sobre el vidrio) |
| Escenario vacío | `.stage-note`, `.stage-agenda` |
| "Ahora" y "Próxima" | `.agenda`, `.slot` (+ `--now`), `__when` (con `<time>`), `__what`, `__empty` |
| Salas al costado | `.room-nav`, `__title`, `__list`, `__item` (`aria-current="page"` = `--sel`), `__row`, `__name`, `__talk`, `__foot` |
| Lista pública | `.room-list`, `.room-card`, `__head`, `__name`, `__link` (toda la fila es el link) |
| Pie | `.colophon` |

### Panel de producción (Tarea 12)

Referencia de markup: `admin-classes.html`.

| Pieza | Clases |
|---|---|
| Pantalla | `.admin`: grilla de 52 px, Atención, pared y agenda; siempre oscura |
| Franja maestra | `.masthead`, `__cell` (celdas separadas por filetes), `__event`, `__push` (empuja a la derecha) |
| Salas por estado | `.tally`, `.tally__item` (`.led` + `<strong>` + palabra) |
| Gasto | `.budget` (+ `--warn` desde el 80 %, `--over`) con `--spent` y `--warn-at` de 0 a 1; `__label`, `__value`, `__of`, `__bar` (`role="meter"`) |
| Operadores | `.operators`, `.operators__list` |
| Atención | `.attn` (+ `--calm`: muestra `.attn__ok`), `__head`, `__list`, `__row` + `--degraded`/`--down`, `__room`, `__what` (con `<em>` para lo secundario), `__age`, `__ok` |
| Pared | `.wall` (3 columnas), `.monitor` + `--live`/`--degraded`/`--down`/`--idle`, `aria-expanded="true"` = abierto en el cajón |
| Monitor | `__head`, `__name`, `__state` (palabra, solo si no está sano), `__tc` (timecode), `kbd` (atajo 1–9), `__cc`, `__issue` (pie con la acción sugerida), `__wait` + `__count` (sala inactiva) |
| Registro | `.panel.panel--sunk` + `.log` (+ `--all`: muestra los informativos; `--compact`: en el cajón), `.log__item` (+ `--info`), `__time`, `__text` (`<b>` sala, `<em>` "Resuelto."), `.log__more` |
| Agenda | `.panel.schedule`, `.schedule__head`, `__next`, `.legend`, `.swatch--manual`/`--now`, `.timeline` (`--span`, `--t` en minutos desde el comienzo de la ventana), `__axis` (`--m`), `__row`, `__room`, `__track`, `__talk` (`--s`, `--d`) + `--past`/`--now`/`--next`/`--manual`, `__now` |
| Cajón | `.scrim` y `.drawer` (+ `--open`), `.drawer__sec`, `__head`, `__title`, `__close`, `__state`, `__talk`, `__who`, `__meta`, `__mode`, `__keys`, `__hint`, `__cc` (+ `--frozen`), `__h` |
| Métricas | `.metrics` (`<dl>`: `dt`, `dd.metrics__value` + `--none`, `dd.metrics__note`); fuera de rango, una `.led` del estado adelante del valor |
| Medidor de audio | `.vu` con `--level` de 0 a 1: gris, porque es un nivel y no un estado |

Los valores de los medidores se pasan como variables en `style` (`style="--spent: 0.348"`, `style="--level: 0.8"`) y llevan `role="meter"` con `aria-valuenow` y `aria-valuetext` en unidades reales ("US$ 41,80 de 120", "−12 dBFS").

**Alias de v1.** `glosa.css` define `--bg`, `--surface`, `--surface-2`, `--text`, `--text-2`, `--rule`, `--rule-strong`, `--ok`, `--fail`, `--idle`, `--radius-s` y `--radius-m` como alias de los roles v2, para que el admin mínimo de la Tarea 7 no se rompa. Se borran con la Tarea 12.
