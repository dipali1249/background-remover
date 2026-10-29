# Background Remover

**Use it right now: <https://dipali1249.github.io/background-remover/>** — open the page and
drop a photo. Nothing to install; the model runs inside your browser and your images are
never uploaded.

There is also a local version (below) that gives you the command line, batch folders,
background-image replacement and model choice.

---

# Local tool

A local image background remover — a drag-and-drop web app plus a command-line tool.
The subject is found with a U²-Net / ISNet segmentation model, then composited onto a
transparent, solid-colour, blurred or custom-image background.

Everything runs on your own machine. No uploads, no accounts, no API keys, no limits.

---

## 1. Install

You need Python 3.9 or newer.

```bash
pip install "rembg[cpu]" pillow
```

or, from a clone of this repo:

```bash
pip install -r requirements.txt
```

That is the whole setup. The first time you remove a background, the chosen model
downloads once (4.5 MB for the default) and is cached in `~/.u2net/` for later runs.

## 2. The web app (easiest)

```bash
python bg_remover_app.py
```

It opens `http://127.0.0.1:8000` in your browser. Then:

- **Drop images** onto the page, or click to browse, or press Ctrl+V to paste a screenshot.
  Drop several at once and they queue up as a batch.
- **Drag the handle** across the preview to compare the original with the cutout.
- **Background**: transparent, a solid colour, or a blurred copy of the original.
- **Edge softness** smooths a jagged cut; **grow/shrink** fixes a halo or a chewed-in edge.
- **Crop to subject** trims the empty canvas away.
- **Download** saves the current image; **Download all** saves the whole batch.

Changing any setting re-processes the current image immediately, so you can tune the
edges and watch the result.

Options:

```bash
python bg_remover_app.py --port 8080          # different port
python bg_remover_app.py --no-browser         # don't auto-open the browser
python bg_remover_app.py --host 0.0.0.0       # reachable from other devices on your network
python bg_remover_app.py --max-mb 80          # allow bigger uploads
```

## 3. The command line

```bash
# transparent PNG next to the original: photo_nobg.png
python bg_remove.py photo.jpg

# white background, JPEG out
python bg_remove.py photo.jpg -o photo.jpg --bg color --bg-color white

# brand colour and trimmed to the subject
python bg_remove.py photo.jpg -o cut.png --bg color --bg-color "#0f172a" --crop

# put the subject in front of another photo
python bg_remove.py photo.jpg -o out.jpg --bg image --bg-image studio.jpg

# keep the original background but blur it
python bg_remove.py photo.jpg -o out.jpg --bg blur --blur 30

# a whole folder
python bg_remove.py photos/ -o cutouts/ --bg color --bg-color white
```

Useful flags:

| Flag | What it does |
| --- | --- |
| `-m, --model` | `u2netp` (default), `u2net`, `isnet-general-use`, `silueta` |
| `--bg` | `transparent`, `color`, `image`, `blur` |
| `--bg-color` | name, `#rrggbb`, or `r,g,b` |
| `--feather` | edge softening in px (default 1, `0` = hard edge) |
| `--grow` | grow the subject edge (`+`) or shrink it (`-`), in px |
| `--crop` | trim the canvas to the subject |
| `--crop-padding` | padding around a cropped subject, as a fraction |
| `--max-size` | downscale so the longest side is at most N px (speeds up big photos) |
| `--format`, `--quality` | `png` / `jpg` / `webp`, and quality for the lossy ones |
| `--suffix` | naming for batch outputs (default `_nobg`) |

Batch mode never stops on one bad file: it reports the failure and carries on, then
exits with code 1.

## 4. Which model?

| Model | Size | Use it for |
| --- | --- | --- |
| `u2netp` | 4.5 MB | default; fast, good on people and products |
| `u2net` | 176 MB | sharper edges when you have the time and RAM |
| `isnet-general-use` | 170 MB | best all-rounder for awkward subjects (hair, fur, thin straps) |
| `silueta` | 43 MB | small and quick, decent on people |

`u2net` and `isnet-general-use` need roughly 1.5–2 GB of free RAM per run; the default
`u2netp` needs a few hundred MB.

## 5. Using it from your own Python

```python
from PIL import Image
from bg_remove import remove_background

img = Image.open("photo.jpg")
cut = remove_background(img, background="transparent")          # RGBA
cut.save("photo_nobg.png")

flat = remove_background(img, background="color", bg_color="#0f172a", crop=True)
flat.save("photo_dark.jpg")
```

`cutout_mask(img, model)` returns just the soft alpha matte if you want to do your own
compositing.

## 6. Troubleshooting

**"rembg not installed"** — run `pip install "rembg[cpu]" pillow`. On a machine without
AVX support try plain `pip install rembg`.

**First run is slow** — the model is downloading. Later runs are fast (about 1–3 s per
photo for `u2netp`).

**Out of memory** — use `u2netp`, add `--max-size 2000`, or close other apps. Very large
photos (30 MP+) are the usual cause.

**Halo or leftover fringe** — set `--grow -1` to pull the edge in slightly, or add
`--feather 1.5`.

**Part of the subject vanished** — the model guesses from contrast. Try
`--model isnet-general-use`; if it still fails, the subject is genuinely similar in
colour to its background and needs a manual mask.

**HEIC photos from an iPhone** — install `pillow-heif` and the tool reads them.

## 7. Speed

The hosted version is built around one idea: the model runs **once per photo**.
Its 320x320 matte is kept in memory, so changing the background, the colour, the crop,
the edge softness or the output format re-composites from that matte in a fraction of a
second instead of re-running the network. On a test machine, changing the background
colour went from about 10 s to about 0.25 s.

Two other things that help:

- The 4.6 MB model is stored in the browser's Cache Storage, so a return visit is ready
  in a second or two rather than re-downloading it.
- Big photos are scaled to 2400 px on the long edge before processing, and the subject
  bounds and edge softening are computed on the small matte rather than at full
  resolution.

What it can't do: inference itself runs on single-threaded WebAssembly. Multi-threaded
WASM requires the page to be cross-origin isolated (COOP/COEP response headers), and
GitHub Pages cannot send custom headers. A WebGPU path would be considerably faster on
suitable hardware, but it is not shipped here because it could not be tested — the
browser available in this project's environment has no GPU adapter.

## 8. Files

```
bg_remove.py        engine + command-line tool
bg_remover_app.py   local web server
ui.html             the web interface (served by the app)
docs/index.html     the hosted, browser-only version (GitHub Pages)
requirements.txt    dependencies
README.md           this file
```

The hosted version in `docs/` needs no Python and no build step: a single HTML file that
pulls ONNX Runtime Web (pinned to 1.20.1, with a subresource-integrity hash) and the
U²-Net model from public mirrors, then does everything on the visitor's device. The model
is pinned to a specific commit and its SHA-256 is checked in the browser before use.
It is served by GitHub Pages from the `docs` folder.

Credits: segmentation by [rembg](https://github.com/danielgatis/rembg), built on
U²-Net and ISNet models. Interface and image compositing written here.
