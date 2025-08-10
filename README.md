# WebMIDI Clip Manager

A local web app to **audition, analyze, trim, and export MIDI phrases** for synths and grooveboxes — with special helpers for **Yamaha Montage/MODX arps**.

- Preview via **WebMIDI** (hardware) **or Built-in Synth** (WebAudio).
- Loop playback with **tempo**, **channel**, **normalize-to-C**, **velocity scaling**, **max bars**, and **power-of-two** loop rounding.
- **Clip analysis**: key/mode (from name or analysis), **unique pitch count**, **max polyphony**, and classification (**Rhythmic**, **Mono**, **Poly**).
- **Export** selected files to `selected/`, applying **your current preview settings** when you choose *Normalize using preview settings*.
- **ZIP export** for the current selection.
- **Yamaha helpers**: 16-note warning, **force 480 PPQN** on export, and **Pack 4 tracks** builder for “Put Track to Arpeggio.”
- Inline **piano roll** (SVG, vector) with **live playhead**, **active-note highlighting**, **adaptive vertical scale**, and **note names** on the left.
  
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

### Top-Left — Controls (left side)
- **Output**: WebMIDI destination or **Built-in Synth**.
- **MIDI Channel**: dropdown 1–16.
- **Tempo (preview)**: 60 / 90 / 120 / 150 BPM.  
  > Timing uses `scale = original_bpm / selected_bpm` — 60 is slower than 120.
- **Filter**: Search name/key/mode/classification.
- **Export selected**:
  - **Normalize using preview settings** — applies your current preview options (Normalize-to-C, Max Bars, Velocity scaling) to the exported files.
  - **Force 480 PPQN (Yamaha)** — recommended for Montage/MODX.
  - **Copy →** write processed files into `selected/`.
  - **Download ZIP** — one archive of the processed selection.
  - **Pack 4 tracks (Yamaha)** — make a single SMF Type-1 at **480 PPQN**, ready for **Put Track to Arpeggio**.
- **Playback**: **Stop All** ends playback and sends All Notes Off (and kills the built‑in synth voices).

### Top-Left — Controls (right side: Preview settings)
- **Normalize to C (same mode)** — ✅ enabled by default.
- **Round loop to power-of-two bars** — ✅ enabled by default (preview-only).
- **Max bars (preview)** — truncates the loop (also used in export when *Normalize using preview settings* is checked).
- **Velocity scaling (single control)** — ✅ enabled by default for preview and export. Set **Target loudest**; others scale proportionally.

### Top-Right — Analysis
- Key/Mode (from filename when present; else by analysis), tempo, time signature, PPQ, bars, note count
- **Unique pitches** with a **>16** warning (Yamaha arp limit)
- **Max polyphony**, classification (**Rhythmic**, **Monophonic**, **Polyphonic**)
- Channels used (highlights Ch 10 to suggest **Convert Type: Fixed**)
- Semitone shift to **Transpose to C (same mode)**

### Bottom-Left — Files
- Recursive listing of `.mid` files under `--root`
- Click a name or **Play** to audition; **Stop** or **Stop All** to end
- Pills show time signature, tempo, note count, unique pitches, classification, key/mode
- Red warning pill appears when **>16 unique notes**

### Bottom-Right — Piano Roll (SVG)
- Vector-based grid and notes for crisp scaling
- **Adaptive vertical scale** (bigger lanes for compact ranges)
- **Note names** shown at left (all when compact; **C-only** when range is large)
- **Live playhead** and **active-note highlighting**
- Start markers and a red loop-end line
- Reflects current preview transforms (tempo, normalization, velocity scaling, max-bars limit, p2 rounding)

---

## Export Details

- Exports are written to `selected/` under your `--root`.
- When **Normalize using preview settings** is checked:
  - **Normalize to C** — transposes note numbers to make the detected root become **C** (mode preserved)
  - **Max bars** — trims events past the boundary and closes sustained notes
  - **Velocity scaling** — scales all note-on velocities so the **loudest** equals your **Target**; others are scaled proportionally and clamped 1–127
- **Force 480 PPQN** — recommended for Yamaha Montage/MODX
- Filenames include tags: `C <Mode>`, `max4bar`, classification, `OrgRoot=<X>`, and `VelMax=<N>` (when enabled).

> The **Round-to-P2** option is for preview only (does not change exported length).

---

## Command-Line Reference

```
usage: webmidi_clip_manager.py --root PATH [--port PORT]

--root   Root folder containing .mid files (scanned recursively)
--port   HTTP port (default 8765)
```

