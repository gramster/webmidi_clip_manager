# WebMIDI Clip Manager

A local web app to **audition, analyze, trim, and export MIDI phrases** for synths and grooveboxes — with special helpers for **Yamaha Montage/MODX arps**.

- Preview via **WebMIDI** (hardware) **or Built-in Synth** (WebAudio).
- Loop playback with **tempo**, **channel**, **normalize-to-C**, **velocity scaling**, **max bars**, and **power-of-two** loop rounding.
- **Clip analysis**: key/mode (from name or analysis), **unique pitch count**, **max polyphony**, and classification (**Rhythmic**, **Mono**, **Poly**).
- **Export** selected files to `selected/`, with optional **normalization**, **bar-length truncation**, and **velocity scaling (export)**.
- **ZIP export** for the current selection.
- **Yamaha helpers**: 16-note warning, **force 480 PPQN** on export, and **Pack 4 tracks** builder for “Put Track to Arpeggio.”
- Inline **piano roll** (SVG, vector) with **live playhead** and **active-note highlighting**, plus an **analysis** panel.
  
![Screenshot](screenshot.png)

---

## Requirements

- **Python 3.8+**
- Python packages:
  ```bash
  pip install flask mido
  ```
- Browser: **Chrome** or **Edge** (WebMIDI support).

> The server only serves files. MIDI output is handled entirely in your browser (WebMIDI) or through the built-in WebAudio synth.

---

## Quick Start

1. Place `webmidi_clip_manager.py` somewhere convenient.
2. Run it pointing at your MIDI folder:
   ```bash
   python webmidi_clip_manager.py --root "/path/to/your/midis" --port 8765
   ```
3. Open **http://localhost:8765** in Chrome/Edge.
4. Choose **Built-in Synth** or a WebMIDI device when prompted.

---

## UI Overview (2×2 layout)

- **Top-Left — Controls**
  - **Output**: WebMIDI destination or **Built-in Synth**.
  - **MIDI Channel**: 1–16 in compact 2×8 grid.
  - **Tempo (preview)**: 60 / 90 / 120 / 150 BPM.  
    > Preview timing uses `scale = original_bpm / selected_bpm` — 60 is slower than 120.
  - **Normalize to C (preview)**: Toggle + **Undo**. Preserves mode.
    - To bake normalization into exports, use **Apply normalization** in the export box.
  - **Velocity scaling (preview)**: Toggle + **Target loudest** (default 100).
  - **Max bars (preview)**: Truncates loop length at the current tempo.
  - **Round loop to power-of-two bars** (preview): Extends loop to 1/2/4/8… bars.
  - **Yamaha mode (export)**: **Force 480 PPQN** (recommended for Montage/MODX).
  - **Export velocity scaling** (new): scale note-on velocities so **loudest = target** in the exported file.
  - **Playback**: **Stop All** immediately stops playback, kills synth voices, and sends All Notes Off.
  - **Filter**: Search name/key/mode/classification.
  - **Export selected**:
    - **Apply normalization** — transpose so the root becomes **C** (same mode).
    - **Max bars** — truncate exports to this length.
    - **Copy →** write processed files into `selected/`.
    - **Download ZIP** — generate a single ZIP of the processed selection.
    - **Pack 4 tracks (Yamaha)** — make a single SMF Type‑1 at **480 PPQN**, ready for **Put Track to Arpeggio**.

- **Top-Right — Analysis**
  - Key/Mode (from filename when present; else by analysis)
  - Tempo, Time Signature, PPQ, Estimated Bars, Note Count
  - **Unique pitches** with a **>16 warning** (Yamaha arp limit)
  - **Max polyphony**, classification (**Rhythmic**, **Monophonic**, **Polyphonic**)
  - Channels used (highlights Ch 10 to suggest **Convert Type: Fixed**)
  - Semitone shift to **Transpose to C (same mode)**

- **Bottom-Left — Files**
  - Recursive listing of `.mid` files under `--root`
  - Click a name or **Play** to audition; **Stop** or **Stop All** to end
  - Pills show time signature, tempo, note count, unique pitches, classification, key/mode
  - A red warning pill appears when **>16 unique notes**

- **Bottom-Right — Piano Roll (SVG)**
  - Vector-based grid and notes for crisp scaling
  - **Live playhead** and **active-note highlighting**
  - Velocity-tinted notes, start markers, and a red loop-end line
  - Reflects current preview transforms (tempo, normalization, velocity scaling, max-bars limit, p2 rounding)

---

## Export Details

- Exports are written to `selected/` under your `--root`.
- **Copy**:
  - Optional **Apply normalization** (transpose note numbers only)
  - Optional **Max bars** (trims events past the boundary and closes sustained notes)
  - Optional **Force 480 PPQN**
  - Optional **Velocity scaling (export)** — scales all note-on velocities so the **loudest** equals your **Target**; others are scaled proportionally and clamped 1–127.
  - Filenames include helpful tags: `C <Mode>`, `max4bar`, classification, `OrgRoot=<X>`, and `VelMax=<N>` when enabled.
- **Download ZIP**:
  - Directly downloads a ZIP containing the processed versions of your current selection.
- **Pack 4 tracks (Yamaha)**:
  - Builds a single **Type-1** file at **480 PPQN** with up to 4 tracks
  - Per-track: optional normalization to C and truncation to max bars
  - Output filename includes source stems + tags

> Preview velocity scaling is **separate** from export scaling. Enable each where needed.

---

## Command-Line Reference

```
usage: webmidi_clip_manager.py --root PATH [--port PORT]

--root   Root folder containing .mid files (scanned recursively)
--port   HTTP port (default 8765)
```

