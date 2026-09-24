# Sistema visual de Glosa

Una *glosa* es una nota al margen que traduce o explica un texto. El sistema visual sale de esa idea: **cada pantalla es una página con margen**. En el margen van las glosas (la hora, "Ahora" y "Próxima", el estado). En la columna de texto va lo que se lee. Un filete vertical separa las dos partes. Donde el texto se está escribiendo en vivo, ese filete se engrosa y se pinta de oropimente.

Todo está en un solo archivo, `glosa/web/static/css/glosa.css`: CSS puro, sin build. Las maquetas de esta carpeta abren con doble clic, sin servidor.

| Maqueta | Qué muestra |
|---|---|
| `index.html` | La lista pública de salas, con "Ahora" y "Próxima". |
| `room-mobile.html` | La vista de sala en un celular de 390 px, en tres estados: en vivo (tema del sistema), historial pausado con "Volver al vivo" (claro) y alto contraste con la letra agrandada. |
| `room-desktop.html` | Escritorio: lista de salas al costado y bilingüe lado a lado. Abajo de 40rem pasa a bilingüe interlineal. |
| `overlay.html` | El overlay de TV sobre fondo a cuadros: 2 renglones de ~42 caracteres, con la marca de Glosa y con una marca de evento de ejemplo. |
| `admin.html` | El panel de producción: 5 salas como canales de una consola, con VU, retraso, calidad, costo, presupuesto y registro de eventos. |
| `shots/` | Capturas de referencia (escritorio y 390 px). |

`mock.css` y `mock.js` son solo para las maquetas (el marco del celular, el fondo a cuadros y la demo de A−/A+ y del tema). La app no los usa.

---

## 1. Cómo se incluye

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Alegreya:ital,wght@0,400..800;1,400..800&amp;family=Atkinson+Hyperlegible+Next:wght@400..800&amp;display=swap">
<link rel="stylesheet" href="/static/css/glosa.css">
```

- El CSS usa capas (`@layer glosa.*`). Cualquier CSS de la página que no esté en una capa le gana siempre, sin pelear por especificidad.
- **Las vistas se acomodan al contenedor, no a la ventana.** `body` es un contenedor llamado `glosa` (`container: glosa / inline-size`), y los cortes usan `@container glosa`. Para mostrar una vista dentro de un marco (por ejemplo, una vista previa de la sala en el panel), dale al marco `container: glosa / inline-size`.
- Cortes: **64rem (1024 px)** pasa a escritorio (subtítulos más grandes, margen más ancho, salas al costado). **40rem (640 px)** es el corte angosto (tabla del registro apilada, bilingüe interlineal).
- Se necesita un navegador de 2024 en adelante: `light-dark()`, `color-mix()`, container queries y subgrid.

## 2. Tipografías y por qué

| Rol | Familia | Dónde |
|---|---|---|
| **Texto** | Atkinson Hyperlegible Next (Braille Institute) | Subtítulos, interfaz, números |
| **Glosa** | Alegreya (Juan Pablo del Peral, Huerta Tipográfica, Buenos Aires) | Nombres de sala, títulos de charla, notas del margen, marca |

- **Atkinson Hyperlegible Next** está diseñada para lectores con baja visión: cada letra se distingue de sus vecinas (I l 1, O 0, rn m). Un subtítulo se lee de reojo, en movimiento y a veces de lejos, y esta familia está hecha para eso. Tiene números tabulares (`tnum`) para las métricas.
- **Alegreya** es caligráfica y tiene ritmo de pluma: es la "mano del glosador". Es argentina, como el evento. Se usa en cursiva para lo que anota (títulos de obra, horas del margen, "Ahora", "Próxima") y en redonda negrita para los nombres propios de las salas.
- **Nunca** se ponen subtítulos en Alegreya ni nombres de sala en Atkinson.
- Los títulos de charla van en cursiva, como todo título de obra en castellano (clase `.talk-title`).
- Sin mayúsculas sostenidas en etiquetas y sin monoespaciada. Las siglas de idioma (EN, ES) van en mayúscula porque son siglas.

### Escala

Escala clásica de Bringhurst, en rem: **12 · 14 · 16 · 18 · 21 · 24 · 36 · 48 · 60 · 72** (`--step--2` a `--step-7`).

Los subtítulos tienen **su propio eje**, fuera de la escala, porque el usuario lo cambia:

| Token | Celular | Escritorio (≥ 64rem) |
|---|---|---|
| `--caption-base` (bloque en vivo) | 26 px (~26 caracteres por renglón a 390 px) | `clamp(28px, 1rem + 1.35cqi, 40px)` |
| `--history-base` (historial) | 18 px | 21 px |
| `--caption-scale` (A−/A+) | 1 | 1 |
| `--leading-caption` | 1.42 | 1.42 |
| `--caption-weight` | 500 (600 en alto contraste) | |

**A−/A+** cambian `--caption-scale` en `<html>`, en estos pasos: `0.8 · 0.9 · 1 · 1.15 · 1.3 · 1.5 · 1.75`. Guardalo en `localStorage` (`glosa.scale`). Todo tamaño de subtítulo se calcula como `calc(var(--caption-base) * var(--caption-scale))`. Si una vista necesita otro tamaño base, pisá `--caption-base`, no `font-size`.

## 3. Color

Pigmentos base (los nombres vienen de los manuscritos):

| Token | Valor | Uso |
|---|---|---|
| `--tinta` | `#121335` | Fondo oscuro: tinta ferrogálica recién puesta, azul negro. |
| `--papel` | `#F6F7FB` | Fondo claro: frío, nunca crema. |
| `--luz` | `#F2F0E9` | Texto sobre tinta, apenas cálido para no encandilar en una sala a oscuras. |
| `--brand-primary` | `#3445D6` lapislázuli | Franja superior de marca, botón principal, links. |
| `--brand-accent` | `#F0B429` oropimente | La pluma y la barra del vivo. Solo eso. |

Los componentes nunca usan los pigmentos directo. Usan **roles**, que se resuelven por tema con `light-dark()`:

`--bg` · `--surface` · `--surface-2` · `--text` · `--text-2` · `--text-open` (frase en curso) · `--rule` · `--rule-strong` · `--primary` · `--accent` · `--live-mark` · `--focus` · `--meter`

Estados (reservados, siempre con **forma + palabra**, nunca solo color):

| Estado | Token | Forma | Palabra en el panel | Palabra para el público |
|---|---|---|---|---|
| 🟢 En vivo | `--ok` | ● círculo | En vivo | En vivo |
| 🟡 Degradado | `--warn` | ▲ triángulo | Degradada | En vivo (el público no lo ve) |
| 🔴 Caído | `--fail` | ■ cuadrado | Caída | Reconectando |
| ⚪ Inactiva | `--idle` | ○ anillo | Inactiva | Entre charlas / la hora de la próxima |

Contrastes medidos (WCAG): texto 16:1 en claro y en oscuro; texto secundario 8:1 y 9,5:1; **frase en curso 4,6:1 en claro y 6,3:1 en oscuro** (sigue siendo AA). En el overlay, aunque el video de atrás sea blanco, el texto da 12:1 y la frase en curso 7:1.

### Marca del evento

La configuración pisa cuatro variables, inyectadas en un `<style>` sin capa o en un `style` en `<html>`:

```html
<style>:root { --brand-primary: #007673; --brand-accent: #00ACA8; --brand-on-primary: #FFFFFF; --brand-on-accent: #00201F; }</style>
```

- `--brand-primary` pinta la franja superior de 4 px, los botones principales y los links. En oscuro se aclara sola (`color-mix` con blanco).
- `--brand-accent` pinta la pluma, la barra del vivo y el filete del overlay. En claro se oscurece sola para llegar a 3:1.
- `--brand-on-primary` y `--brand-on-accent` son el color del texto sobre esos fondos. Si el primario del evento es claro, poné `--brand-on-primary: #000`.
- El logo del evento va en `<img class="event-logo">` (28 px de alto). Si no hay logo, el nombre va en `.event-name`.
- La marca **nunca** colorea el texto de los subtítulos ni los estados.

### Temas

- Sin atributo, se sigue `prefers-color-scheme`. Con `prefers-contrast: more`, se usa alto contraste.
- `data-theme="light" | "dark" | "contrast"` en `<html>` fuerza uno. Guardalo en `localStorage` (`glosa.theme`). También funciona en cualquier elemento, para mostrar un tema dentro de otro.
- **Alto contraste** es negro, blanco y amarillo, sin transparencias, con filetes de 2 px, barra del vivo de 6 px, foco de 4 px y peso 600 en los subtítulos. La frase en curso **no se apaga**: pasa a amarillo (16:1).
- `forced-colors` (alto contraste de Windows) también está cubierto.

## 4. Cómo se ve el texto en vivo

Esta es la regla más importante del sistema.

```html
<div class="line line--live" aria-live="off">
  <time class="line__time">14:06</time>
  <p class="line__text">
    <span class="phrase">La mitad se fue en etcd y en logs de auditoría que nadie leía.</span>
    <span class="phrase phrase--open">Así que lo primero que hicimos fue apagar</span>
  </p>
</div>
```

1. **Frase en curso:** `.phrase.phrase--open`, en `--text-open` (más tenue, pero AA). Termina en la **pluma**, un trazo inclinado de oropimente que respira despacio. Es lo único que se mueve en toda la interfaz.
2. **Frase cerrada:** se le saca `phrase--open`. El color pasa a sólido en 480 ms. **Cambia solo el color, nunca el peso ni el tamaño**, así que ninguna letra se corre.
3. Siempre hay **una sola** `.phrase--open`, y es la última. El texto nuevo se agrega al final del mismo `<span>`: nunca se reescribe lo que ya está en pantalla.
4. En texto en vivo **no se usan** `text-wrap: balance` ni `pretty`, ni texto centrado, ni corte de palabras automático. Cualquiera de esas cosas vuelve a acomodar renglones que el lector ya estaba leyendo.
5. Cuando el bloque en vivo junta ~3 frases cerradas, esas frases pasan al historial como un `<li class="line">` nuevo, con la hora de la primera frase. Si el `<li>` es del mismo minuto que el anterior, va sin `<time>`: el filete sigue y el margen queda vacío.
6. El historial es `role="log"`: un lector de pantalla anuncia párrafos terminados, no cada palabra. El bloque en vivo tiene `aria-live="off"`.
7. **Auto-scroll:** si el usuario sube más de ~48 px, se pausa y aparece `.to-live` ("Volver al vivo", sin `hidden`). Al tocarlo, vuelve abajo y se esconde.

### Overlay de TV

- `/overlay/{sala}?lang=es&lines=2&size=48`: el template pone `--overlay-lines` y `--overlay-size` en un `style` sobre `.overlay`, y `body` lleva la clase `.overlay-page` (fondo transparente, caja abajo y al centro).
- La caja tiene **ancho fijo** (`--overlay-measure: 19em`, ≈ 42 caracteres de Atkinson a peso 600) y **alto fijo** (N renglones). El texto está anclado abajo y lo viejo sale por arriba, como en los subtítulos "roll-up" de la tele. La caja nunca cambia de tamaño mientras llega texto.
- Colores fijos, no dependen del tema: tinta al 86 %, texto blanco, frase en curso `#C3C6EC`, filete izquierdo en `--brand-accent`.

## 5. Componentes

| Pieza | Clases |
|---|---|
| Marca | `.wordmark` (con `.nib` adentro), `.event-name`, `.event-logo` |
| Encabezado de sala | `.room-head`, `__bar`, `__brand`, `__event` (solo escritorio), `__rooms` (solo celular), `__name`, `__talk`, `__meta` |
| Dirección de traducción | `.direction` (EN → ES, con `<abbr>` y la flecha SVG) |
| Transcripción | `.transcript` (+ `--bilingual`), `.history`, `.line`, `.line--live`, `.line--head`, `.line__time`, `.line__text`, `.line__label` |
| Frases | `.phrase`, `.phrase--open` |
| Controles | `.controls`, `.control`, `.control--select`, `.control-group`, `.control__a--small/--big`, `.to-live` (+ `.controls__to-live` para flotar arriba de la barra) |
| Salas al costado | `.room-nav`, `__title`, `__list`, `__item` (`aria-current="page"`), `__row`, `__name`, `__talk`, `__foot` |
| Lista pública | `.room-list`, `.room-card` (+ `--live`), `__head`, `__name`, `__link` (toda la tarjeta es el link), `.agenda`, `.slot` (+ `--now`), `__when`, `__what`, `__empty` |
| Estado | `.status` + `--live`, `--degraded`, `--down`, `--idle` |
| Panel | `.console`, `.strip` + `--live/--degraded/--down/--idle`, `__head`, `__name`, `__reason`, `__talk`, `__tags`, `__actions`, `.segmented` (con `aria-pressed`) |
| Métricas | `.metrics`, `.metric` (+ `--warn`, `--fail`) |
| Medidor de audio | `.vu` con `--level` y `--peak` de 0 a 1 (0 = −60 dBFS, 1 = 0 dBFS); `__label`, `__track`, `__peak`, `__value`, `__alarm`. Desde −6 dBFS (`--hot: 0.9`) el relleno pasa a `--warn`. |
| Calidad (Jev) | `.quality` + `--good` (≥ 0,7), `--fair` (0,5 a 0,7), `--low` (< 0,5), `--none` ("sin datos"); `--q` de 0 a 1; `.quality__bar` marca el umbral de alarma en 0,5 |
| Presupuesto | `.budget` (+ `--warn` desde el 80 %, `--over`) con `--spent` y `--warn-at` de 0 a 1; `__figure`, `__spent`, `__of`, `__bar`, `__scale`, `__mark` |
| Registro | `.log__table`, `.log__time`, `.log__room`, `.log__row--error`, `.level` + `--warn`, `--error` |
| Botones | `.btn` + `--primary`, `--danger`, `--quiet`; `kbd` |
| Overlay | `.overlay-page`, `.overlay`, `.overlay__window`, `.overlay__text` |
| Pantallas | `.room` (+ `.room__stage`), `.index`, `.admin` (+ `__bar`, `__main`, `__lower`), `.panel` |
| Otros | `.talk-title`, `.talk-meta`, `.chip`, `.colophon`, `.visually-hidden`, `.num` |

Los valores de los medidores se pasan como variables en `style`: `style="--level: 0.66; --peak: 0.78"`. Los medidores llevan `role="meter"` con `aria-valuenow` y `aria-valuetext` en unidades reales ("−20 dBFS", "US$ 41,80 de 120").

## 6. Reglas de diseño

1. **Los subtítulos son el producto; todo lo demás es margen.** Ningún adorno compite con el texto en vivo.
2. **Nada se mueve cuando llega texto.** El borde de lectura queda fijo a la izquierda, las cajas no cambian de tamaño y lo en curso cambia de color, no de forma.
3. **Una sola cosa en movimiento:** la pluma. Nada de entradas animadas ni efectos al pasar el mouse sobre tarjetas. `prefers-reduced-motion` apaga la pluma y la transición de color.
4. **El margen tiene información, no decoración.** Si algo va en el margen, es una hora, una etiqueta de tiempo ("Ahora", "Próxima") o un estado.
5. **Los radios siguen la jerarquía:** 0 en superficies y franjas, 4 px en chips y botones del panel, 10 px en teclas de la barra de controles, píldora solo en "Volver al vivo".
6. **El panel es una consola, no un tablero de tarjetas.** Las salas son canales pegados, separados por filetes, con la luz de estado arriba. Nada de sombras ni tarjetas redondeadas sueltas.
7. **Formato rioplatense:** voseo ("Elegí una sala"), coma decimal ("2,4 s", "US$ 41,80"), horas de 24 h, mayúscula solo al principio de la frase y en nombres propios.
8. **Foco siempre visible:** contorno de 3 px en `--focus` (lapislázuli en claro, oropimente en oscuro, amarillo de 4 px en alto contraste). Blancos táctiles de 44 px en la barra de controles.
