#!/usr/bin/env python3
# webmidi_clip_manager.py
#
# Local web app to preview & manage MIDI phrases for hardware synths (incl. Yamaha Montage/MODX arps).
#
# New in this build:
# - "Stop All" button
# - Velocity scaling baked into EXPORT (optional; preview scaling still separate)
# - ZIP export of processed selections
# - Piano roll live indicators: moving playhead + active note highlighting
#
import argparse
import re
import json
import shutil
import io
import zipfile
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set

from flask import Flask, send_file, request, jsonify, Response

import mido

app = Flask(__name__)

ROOT = Path.cwd()

NOTE_NAME = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
PC = {'c':0,'c#':1,'db':1,'d':2,'d#':3,'eb':3,'e':4,'f':5,'f#':6,'gb':6,'g':7,'g#':8,'ab':8,'a':9,'a#':10,'bb':10,'b':11}

def norm_mode(s: str) -> str:
    s = s.lower()
    if s in ('maj','major'): return 'ionian'
    if s in ('min','minor','m'): return 'aeolian'
    return s

def parse_key_from_name(name: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    base = re.sub(r'\(.*?\)', '', name)
    base = base.replace('.mid','').strip()
    patterns = [
        r'(?P<root>[A-Ga-g](?:#|b)?)\s*(?P<mode>lydian|mixolydian|dorian|phrygian|ionian|major|minor)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)(?P<mode>lydian|mixolydian|dorian|phrygian|ionian|major|minor)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)\s*(?P<mode>maj|min|m)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)(?P<mode>maj|min|m)\b',
        r'\b(?P<root>[A-Ga-g](?:#|b)?)\s*$',
    ]
    for pat in patterns:
        m = re.search(pat, base, flags=re.IGNORECASE)
        if m:
            root = m.group('root')
            mode = m.groupdict().get('mode', 'ionian')
            mode = norm_mode(mode) if mode else 'ionian'
            root_pc = PC.get(root.lower(), None)
            return root, root_pc, mode
    m2 = re.search(r'([A-Ga-g](?:#|b)?)\s*(lydian|mixolydian|dorian|phrygian|ionian|major|minor|maj|min|m)', base, flags=re.IGNORECASE)
    if m2:
        root = m2.group(1); mode = norm_mode(m2.group(2))
        root_pc = PC.get(root.lower(), None)
        return root, root_pc, mode
    return None, None, None

MAJOR_PROFILE = [6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88]
MINOR_PROFILE = [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]

def guess_major_minor(pitches_dur: List[Tuple[int,int]]) -> Optional[Tuple[int,str,float]]:
    if not pitches_dur:
        return None
    pc_hist = [0.0]*12
    for p,d in pitches_dur:
        pc_hist[p%12] += d
    s = sum(pc_hist) or 1.0
    pc_hist = [x/s for x in pc_hist]
    def correlate(profile):
        best = (-1.0, 0)
        for r in range(12):
            rot = profile[-r:] + profile[:-r]
            score = sum(a*b for a,b in zip(pc_hist, rot))
            if score > best[0]:
                best = (score, r)
        return best
    maj_s, maj_r = correlate(MAJOR_PROFILE)
    min_s, min_r = correlate(MINOR_PROFILE)
    if maj_s >= min_s:
        return (maj_r, 'ionian', maj_s)
    else:
        return (min_r, 'aeolian', min_s)

def ticks_per_bar(ppq: int, numer: int, denom: int) -> int:
    return int(round(numer * (ppq * 4 / denom)))

def analyze_midi(path: Path) -> Dict:
    try:
        mid = mido.MidiFile(path)
    except Exception as e:
        return {"filename": path.name, "relpath": str(path.relative_to(ROOT)), "error": f"open failed: {e}"}

    msgs = []
    for tr in mid.tracks:
        t = 0
        for m in tr:
            t += m.time
            mm = m.copy(time=t)
            msgs.append(mm)

    tempo_msg = next((m for m in msgs if m.type=='set_tempo'), None)
    tempo_bpm = round(mido.tempo2bpm(tempo_msg.tempo),2) if tempo_msg else 120.0
    ts_msg = next((m for m in msgs if m.type=='time_signature'), None)
    numer, denom = (ts_msg.numerator, ts_msg.denominator) if ts_msg else (4,4)
    ppq = mid.ticks_per_beat
    bar_ticks = ticks_per_bar(ppq, numer, denom)

    note_on_map: Dict[Tuple[int,int], List[Tuple[int,int]]] = {}
    unique_pitches: Set[int] = set()
    channels_used: Set[int] = set()
    events = []

    notes = []
    for m in msgs:
        if m.type == 'note_on' and m.velocity>0:
            k=(getattr(m,'channel',0), m.note)
            note_on_map.setdefault(k, []).append((m.time, m.velocity))
            unique_pitches.add(m.note)
            channels_used.add(getattr(m,'channel',0))
            events.append((m.time, +1))
        elif m.type in ('note_off','note_on') and (m.type=='note_off' or m.velocity==0):
            k=(getattr(m,'channel',0), m.note)
            if note_on_map.get(k):
                st, vel = note_on_map[k].pop(0)
                if m.time>st:
                    notes.append((st, m.time, m.note, vel, k[0]))
                    events.append((m.time, -1))

    notes.sort(key=lambda x:(x[0], x[2]))
    note_count = len(notes)
    end_ticks = max((n[1] for n in notes), default=0)
    est_bars = (end_ticks / bar_ticks) if bar_ticks>0 else 0

    max_poly = 0
    cur = 0
    for t, delta in sorted(events, key=lambda x:(x[0], -x[1])):
        cur += delta
        if cur > max_poly: max_poly = cur
    if len(unique_pitches) <= 1:
        classification = 'rhythmic_single_note'
    elif max_poly <= 1:
        classification = 'monophonic_melodic'
    else:
        classification = 'polyphonic_chordal'

    root_name, root_pc, mode = parse_key_from_name(path.name)
    key_source = None
    if root_pc is not None and mode:
        key_source = 'filename'
    else:
        pd = [(p, max(1, e - s)) for (s, e, p, vel, ch) in notes]
        gm = guess_major_minor(pd)
        if gm:
            root_pc, mode, _ = gm
            root_name = NOTE_NAME[root_pc]
            key_source = 'analysis'

    transpose_to_c = (0 - root_pc) % 12 if root_pc is not None else None
    over16 = len(unique_pitches) > 16
    uses_ch10 = (9 in channels_used)

    return {
        "filename": path.name,
        "relpath": str(path.relative_to(ROOT)),
        "tempo_bpm": tempo_bpm,
        "time_signature": f"{numer}/{denom}",
        "ppq": ppq,
        "bars_estimate": round(est_bars, 3),
        "note_count": note_count,
        "root": root_name,
        "root_pc": root_pc,
        "mode": mode,
        "key_source": key_source,
        "transpose_to_C_same_mode": transpose_to_c,
        "unique_pitches": len(unique_pitches),
        "over16_unique": over16,
        "channels": sorted(list(channels_used)),
        "uses_ch10": uses_ch10,
        "max_polyphony": max_poly,
        "classification": classification
    }

def iter_midis(root: Path):
    for p in sorted(root.rglob('*.mid')):
        yield p

@app.route('/')
def index():
    return Response(INDEX_HTML, mimetype='text/html')

@app.route('/api/files')
def api_files():
    files = [analyze_midi(p) for p in iter_midis(ROOT)]
    return jsonify({"root": str(ROOT), "count": len(files), "files": files})

@app.route('/api/raw')
def api_raw():
    rel = request.args.get('file')
    if not rel:
        return "Missing ?file=...", 400
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        return "Forbidden", 403
    if not p.exists() or p.suffix.lower()!='.mid':
        return "Not found", 404
    return send_file(p, mimetype='audio/midi', as_attachment=False, download_name=p.name)

def rescaled_abs_events(track: mido.MidiTrack, factor: float) -> List[Tuple[int, mido.Message]]:
    events = []
    t = 0
    for msg in track:
        t += msg.time
        tt = int(round(t * factor)) if factor != 1.0 else t
        events.append((tt, msg.copy(time=0)))
    return events

def rebuild_track_from_abs(events: List[Tuple[int, mido.Message]]) -> mido.MidiTrack:
    events.sort(key=lambda x:(x[0], 0 if getattr(x[1],'type','')=='note_on' else 1))
    newt = mido.MidiTrack()
    last = 0
    for t_abs, m in events:
        dt = max(0, t_abs - last)
        last = t_abs
        newt.append(m.copy(time=dt))
    newt.append(mido.MetaMessage('end_of_track', time=0))
    return newt

def write_transposed_truncated_forcedppq(src: Path, dst: Path, semitones: Optional[int], max_bars: Optional[float], force_ppq: Optional[int] = None, vel_target: Optional[int] = None):
    """Write transposed+truncated copy; optionally resample to force_ppq and scale velocities so loudest==vel_target."""
    mid = mido.MidiFile(src)
    numer, denom = 4, 4
    for tr in mid.tracks:
        for msg in tr:
            if msg.type == 'time_signature':
                numer, denom = msg.numerator, msg.denominator
                break
        else:
            continue
        break
    ppq = mid.ticks_per_beat
    limit_ticks = None
    if max_bars and max_bars > 0:
        limit_ticks = int(max_bars * ticks_per_bar(ppq, numer, denom))

    target_ppq = force_ppq or ppq
    factor = (target_ppq / ppq) if target_ppq != ppq else 1.0

    # Pass 1: find max velocity if scaling requested
    max_vel = 0
    if vel_target is not None:
        for tr in mid.tracks:
            t = 0
            for msg in tr:
                t += msg.time
                if msg.type == 'note_on' and msg.velocity>0:
                    if msg.velocity > max_vel:
                        max_vel = msg.velocity
        if max_vel <= 0:
            vel_target = None  # nothing to scale

    out = mido.MidiFile(type=mid.type, ticks_per_beat=target_ppq)
    for tr in mid.tracks:
        abs_events = rescaled_abs_events(tr, factor)
        processed = []
        active = {}
        for t_abs, msg in abs_events:
            m = msg.copy()
            if m.type in ('note_on','note_off') and semitones is not None:
                m.note = (m.note + semitones) % 128
            if limit_ticks is not None and t_abs > int(limit_ticks * (target_ppq/ppq)):
                continue
            if vel_target is not None and m.type=='note_on' and m.velocity>0 and max_vel>0:
                # scale proportionally: loudest -> vel_target
                scaled = int(round(m.velocity * (vel_target / max_vel)))
                m.velocity = max(1, min(127, scaled))
            processed.append((t_abs, m))
            if m.type == 'note_on' and getattr(m,'velocity',0) > 0:
                active[(getattr(m,'channel',0), m.note)] = True
            elif m.type in ('note_off','note_on') and (getattr(m,'velocity',0)==0 or m.type=='note_off'):
                active.pop((getattr(m,'channel',0), m.note), None)
        if limit_ticks is not None:
            t_limit = int(limit_ticks * (target_ppq/ppq))
            for (ch, note) in list(active.keys()):
                processed.append((t_limit, mido.Message('note_off', note=note, velocity=0, channel=ch, time=0)))
        newt = rebuild_track_from_abs(processed)
        out.tracks.append(newt)
    out.save(dst)

@app.route('/api/copy', methods=['POST'])
def api_copy():
    data = request.get_json(silent=True) or {}
    files = data.get('files', [])
    normalize = bool(data.get('normalize', False))
    max_bars = data.get('max_bars', None)
    force480 = bool(data.get('force480', True))
    vel_scale = bool(data.get('vel_scale', False))
    vel_target = data.get('vel_target', None)
    try:
        max_bars = float(max_bars) if max_bars not in (None, '') else None
    except Exception:
        max_bars = None
    try:
        vel_target = int(vel_target) if vel_target not in (None, '') else None
        if vel_target is not None:
            vel_target = max(1, min(127, vel_target))
    except Exception:
        vel_target = None

    dest_dir = ROOT / 'selected'
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    errors = []
    for rel in files:
        try:
            src = (ROOT / rel).resolve()
            if not str(src).startswith(str(ROOT.resolve())):
                raise RuntimeError("Outside root")
            if not src.exists() or src.suffix.lower()!='.mid':
                raise RuntimeError("not a .mid or missing")
            info = analyze_midi(src)
            semis = None
            orgroot = None
            if normalize and info.get('transpose_to_C_same_mode') is not None:
                semis = int(info['transpose_to_C_same_mode']) % 12
                orgroot = 'C'
            elif info.get('root'):
                orgroot = info['root']
            mode = info.get('mode') or ''
            suffix_parts = []
            if semis is not None:
                suffix_parts.append(f"C {mode.capitalize()}" if mode else "C")
            if max_bars and max_bars > 0:
                suffix_parts.append(f"max{int(max_bars)}bar" if float(max_bars).is_integer() else f"max{max_bars}bar")
            class_map = {'rhythmic_single_note':'Rhythmic','monophonic_melodic':'Mono','polyphonic_chordal':'Poly'}
            ctag = class_map.get(info.get('classification',''), '')
            if ctag: suffix_parts.append(ctag)
            if orgroot: suffix_parts.append(f"OrgRoot={orgroot}")
            if vel_scale and vel_target is not None:
                suffix_parts.append(f"VelMax={vel_target}")
            dstname = src.stem + (' - ' + ' '.join(suffix_parts) if suffix_parts else '') + src.suffix
            dst = dest_dir / dstname
            write_transposed_truncated_forcedppq(src, dst, semis, max_bars, 480 if force480 else None, vel_target if vel_scale else None)
            copied.append(str(dst.relative_to(ROOT)))
        except Exception as e:
            errors.append({"file": rel, "error": str(e)})
    return jsonify({"copied": copied, "errors": errors, "dest": str(dest_dir.relative_to(ROOT)), "normalized": normalize, "max_bars": max_bars, "force480": force480, "vel_scaled": vel_scale, "vel_target": vel_target})

@app.route('/api/export_zip', methods=['POST'])
def api_export_zip():
    data = request.get_json(silent=True) or {}
    files = data.get('files', [])
    normalize = bool(data.get('normalize', False))
    max_bars = data.get('max_bars', None)
    force480 = bool(data.get('force480', True))
    vel_scale = bool(data.get('vel_scale', False))
    vel_target = data.get('vel_target', None)
    try:
        max_bars = float(max_bars) if max_bars not in (None, '') else None
    except Exception:
        max_bars = None
    try:
        vel_target = int(vel_target) if vel_target not in (None, '') else None
        if vel_target is not None:
            vel_target = max(1, min(127, vel_target))
    except Exception:
        vel_target = None

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            try:
                src = (ROOT / rel).resolve()
                if not str(src).startswith(str(ROOT.resolve())):
                    raise RuntimeError("Outside root")
                if not src.exists() or src.suffix.lower()!='.mid':
                    raise RuntimeError("not a .mid or missing")
                info = analyze_midi(src)
                semis = None
                orgroot = None
                if normalize and info.get('transpose_to_C_same_mode') is not None:
                    semis = int(info['transpose_to_C_same_mode']) % 12
                    orgroot = 'C'
                elif info.get('root'):
                    orgroot = info['root']
                mode = info.get('mode') or ''
                suffix_parts = []
                if semis is not None:
                    suffix_parts.append(f"C {mode.capitalize()}" if mode else "C")
                if max_bars and max_bars > 0:
                    suffix_parts.append(f"max{int(max_bars)}bar" if float(max_bars).is_integer() else f"max{max_bars}bar")
                class_map = {'rhythmic_single_note':'Rhythmic','monophonic_melodic':'Mono','polyphonic_chordal':'Poly'}
                ctag = class_map.get(info.get('classification',''), '')
                if ctag: suffix_parts.append(ctag)
                if orgroot: suffix_parts.append(f"OrgRoot={orgroot}")
                if vel_scale and vel_target is not None:
                    suffix_parts.append(f"VelMax={vel_target}")
                dstname = src.stem + (' - ' + ' '.join(suffix_parts) if suffix_parts else '') + src.suffix

                # Write processed MIDI into memory, then add to zip
                tmp_buf = io.BytesIO()
                mid = mido.MidiFile()
                # We'll reuse file writer function but to a temporary path; to avoid FS, we replicate logic:
                # Simpler: write to a temp file on disk then read; but in-memory is better.
                # We'll write to a NamedTemporaryFile? For simplicity, process to a temp path then read bytes.
                tmp_path = ROOT / ('._tmp_export_' + dstname)
                write_transposed_truncated_forcedppq(src, tmp_path, semis, max_bars, 480 if force480 else None, vel_target if vel_scale else None)
                with open(tmp_path, 'rb') as fh:
                    zf.writestr(dstname, fh.read())
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            except Exception as e:
                # Write a small text error into the zip to signal which file failed
                zf.writestr(f"ERROR_{Path(rel).name}.txt", str(e))

    mem_zip.seek(0)
    return send_file(mem_zip, mimetype='application/zip', as_attachment=True, download_name='exported_clips.zip')

INDEX_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>WebMIDI Clip Manager</title>
  <style>
    :root { --bg:#0b0d10; --fg:#e6edf3; --muted:#a9b1ba; --card:#12161a; --accent:#6bb3ff; --grid:#1a2530; }
    html, body { height:100%; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
           background:var(--bg); color:var(--fg); }
    h1 { margin:0; font-size:16px; letter-spacing:.2px; }
    .grid { display:grid; height:98vh; grid-template-columns: 1fr 1fr;
            grid-template-rows: 0.42fr 0.58fr;
            grid-template-areas: "controls details" "list roll"; gap:10px; padding:10px 12px; }
    .panel { background:var(--card); border:1px solid #182028; border-radius:12px; overflow:hidden; display:flex; flex-direction:column; min-height:0; }
    .panel header { padding:8px 10px; background:#0f141a; border-bottom:1px solid #182028; }
    .panel header h2 { margin:0; font-size:13px; color:#cdd6df; }
    .body { padding:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; overflow:auto; }
    .tool { background:#0f151b; padding:8px 10px; border-radius:10px; border:1px solid #1a222c; }
    .tool label { font-size:11px; color:#a9b1ba; display:block; margin-bottom:4px; }
    .tool select, .tool .opts, .tool input { font-size:13px; }
    .btn { background:#1a73e8; color:#fff; border:none; border-radius:8px; padding:8px 10px; cursor:pointer; font-weight:600; }
    .btn.secondary { background:#1f2937; color:#e6edf3; border:1px solid #2b3542; }
    .btn.destructive { background:#d14343; }
    .btn:disabled { opacity:.6; cursor:not-allowed; }
    .status { font-size:12px; color:#a9b1ba; }
    input[type="text"], input[type="number"] { background:#0d131a; color:#e6edf3; border:1px solid #202833; border-radius:8px; padding:6px 8px; }
    #chanOpts { display:grid; grid-template-columns: repeat(8, auto); column-gap:6px; row-gap:2px; }
    #fileList { overflow:auto; }
    .row { display:grid; grid-template-columns: 24px 1fr auto auto; align-items:center; gap:8px; padding:8px 10px; border-bottom:1px solid #151b21; }
    .row:hover { background:#0f1419; }
    .name { cursor:pointer; font-weight:600; font-size:13px; }
    .pill { font-size:10px; padding:2px 6px; border-radius:12px; background:#0e1620; border:1px solid #1c2228; color:#cfd6dd; margin-left:4px; white-space:nowrap; }
    .pill.warn { border-color:#e55353; color:#ff9a9a; }
    .controls button { margin-left:6px; background:#1f2937; color:#e6edf3; border:1px solid #2b3542; border-radius:6px; padding:4px 8px; cursor:pointer; }
    .controls button:hover { border-color:#3c4858; }
    .playing { color:#7ee787; }
    #rollWrap { overflow:auto; height:100%; background:#0b1016; }
    #rollSvg { width:100%; height:100%; }
    .gridline { stroke: #1a2530; stroke-width:1; }
    .barline { stroke: #233141; stroke-width:1.2; }
    .loopline { stroke: #ff5555; stroke-width:1.5; }
    .note { stroke: rgba(0,0,0,0.4); stroke-width:0.5; }
    .note.active { stroke: #ffffff; stroke-width:1; }
    .marker { fill: #d1e4ff; }
    .playhead { stroke: #88c0ff; stroke-width:1.5; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/@tonejs/midi@2.0.28/build/Midi.min.js"></script>
</head>
<body>
  <div class="grid">
    <section class="panel" style="grid-area:controls;">
      <header><h2>Controls</h2></header>
      <div class="body">
        <div class="tool">
          <label>Output</label>
          <select id="midiOut"></select>
        </div>
        <div class="tool">
          <label>MIDI Channel</label>
          <div class="opts" id="chanOpts"></div>
        </div>
        <div class="tool">
          <label>Tempo</label>
          <div class="opts">
            <label><input type="radio" name="tempo" value="60">60</label>
            <label><input type="radio" name="tempo" value="90">90</label>
            <label><input type="radio" name="tempo" value="120" checked>120</label>
            <label><input type="radio" name="tempo" value="150">150</label>
          </div>
        </div>
        <div class="tool">
          <label>Normalize to C (preview)</label>
          <div>
            <button class="btn secondary" id="toggleNormalize">Off</button>
            <button class="btn secondary" id="undoNormalize">Undo</button>
          </div>
        </div>
        <div class="tool">
          <label>Velocity scaling (preview)</label>
          <div>
            <label><input type="checkbox" id="velScaleToggle"> Enable</label>
            <label>Target loudest: <input type="number" id="velTarget" min="1" max="127" value="100" style="width:70px"></label>
          </div>
        </div>
        <div class="tool">
          <label>Max bars (preview)</label>
          <div><input type="number" id="maxBars" min="1" step="0.5" value="4" style="width:70px"></div>
        </div>
        <div class="tool">
          <label>Round loop to power-of-two bars</label>
          <div><input type="checkbox" id="roundP2"></div>
        </div>
        <div class="tool">
          <label>Yamaha mode (export)</label>
          <div><label><input type="checkbox" id="force480" checked> Force 480 PPQN</label></div>
        </div>
        <div class="tool">
          <label>Export velocity scaling</label>
          <div>
            <label><input type="checkbox" id="velScaleExport"> Enable</label>
            <label>Target loudest: <input type="number" id="velTargetExport" min="1" max="127" value="100" style="width:70px"></label>
          </div>
        </div>
        <div class="tool">
          <label>Playback</label>
          <button class="btn destructive" id="stopAll">Stop All</button>
        </div>
        <div class="tool">
          <label>Filter</label>
          <input type="text" id="filterBox" placeholder="Search name/key/mode"/>
        </div>
        <div class="tool">
          <label>Export selected</label>
          <div class="opts" style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <label><input type="checkbox" id="normalizeOnCopy"/> Apply normalization</label>
            <label>Max bars: <input type="number" id="maxBarsCopy" min="1" step="0.5" value="4" style="width:70px"></label>
            <button id="copySelected">Copy → <b>/selected</b></button>
            <button id="zipSelected">Download ZIP</button>
            <button id="pack4">Pack 4 tracks (Yamaha)</button>
          </div>
        </div>
        <div class="status" id="status">Loading…</div>
      </div>
    </section>

    <section class="panel" style="grid-area:details;">
      <header><h2>Analysis</h2></header>
      <div class="body" id="details" style="min-height:110px"></div>
    </section>

    <section class="panel" style="grid-area:list;">
      <header><h2>Files</h2></header>
      <div class="body" id="fileList"></div>
    </section>

    <section class="panel" style="grid-area:roll;">
      <header><h2>Piano Roll</h2></header>
      <div id="rollWrap">
        <svg id="rollSvg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60" preserveAspectRatio="xMinYMin meet"></svg>
      </div>
    </section>
  </div>

<script>
let midiAccess = null;
let currentOut = null;
let useSynth = false;
let ac = null, master = null;
let currentChannel = 1;
let currentTempo = 120;
let playing = null; // { id, timers:[], loopLen, startMs, playheadTimer }
let fileData = [];
let normalizePreview = false;
let normalizeHistory = [];
let activeVoices = new Map();
let velScaleEnabled = false;
let velTarget = 100;
let roundP2 = false;

let currentRenderNotes = []; // for drawing + highlight [{t,d,p,idx}...]
let currentRectMap = new Map(); // idx -> rect element
let playheadTimer = null;

// Channels 2x8
(function buildChanRadios(){
  const cont = document.getElementById('chanOpts');
  for(let i=1;i<=16;i++){
    const lab=document.createElement('label');
    lab.innerHTML = '<input type="radio" name="chan" value="'+i+'" '+(i===1?'checked':'')+'>'+i;
    cont.appendChild(lab);
  }
  cont.addEventListener('change', e => { if(e.target.name==='chan'){ currentChannel = parseInt(e.target.value); } });
})();

// Tempo
document.querySelectorAll('input[name="tempo"]').forEach(r => {
  r.addEventListener('change', e => { currentTempo = parseInt(e.target.value); if(playing){ restartCurrent(); } });
});

// Velocity preview
document.getElementById('velScaleToggle').addEventListener('change', e => { velScaleEnabled = !!e.target.checked; if(playing){ restartCurrent(); } });
document.getElementById('velTarget').addEventListener('change', e => {
  const v = parseInt(e.target.value); velTarget = Math.max(1, Math.min(127, isNaN(v)?100:v));
  e.target.value = velTarget; if(playing){ restartCurrent(); }
});

// Normalize toggle
const toggleBtn = document.getElementById('toggleNormalize');
const undoBtn = document.getElementById('undoNormalize');
function updateNormalizeUI(){ toggleBtn.textContent = normalizePreview ? 'On' : 'Off'; toggleBtn.classList.toggle('secondary', true); }
toggleBtn.addEventListener('click', () => { normalizeHistory.push(normalizePreview); normalizePreview = !normalizePreview; updateNormalizeUI(); if(playing){ restartCurrent(); } });
undoBtn.addEventListener('click', () => { if(normalizeHistory.length){ normalizePreview = normalizeHistory.pop(); updateNormalizeUI(); if(playing){ restartCurrent(); } } });
updateNormalizeUI();

// P2 rounding
document.getElementById('roundP2').addEventListener('change', e => { roundP2 = !!e.target.checked; if(playing){ restartCurrent(); } });

function restartCurrent(){
  if(!playing) return;
  const row = document.getElementById('row-'+cssEscape(playing.id));
  if(row){
    const rel = decodeURIComponent(row.querySelector('.play').dataset.rel);
    playLoop(rel, playing.id);
  }
}

// Built-in synth
function ensureAC(){
  if(!ac){
    const Ctx = window.AudioContext || window.webkitAudioContext;
    ac = new Ctx();
    master = ac.createGain();
    master.gain.value = 0.8;
    master.connect(ac.destination);
  }
}
function hzFromMidi(n){ return 440 * Math.pow(2, (n-69)/12); }
function synthNoteOn(note, vel=100){
  ensureAC();
  const t = ac.currentTime;
  const osc = ac.createOscillator();
  const gain = ac.createGain();
  osc.type = 'sawtooth';
  osc.frequency.setValueAtTime(hzFromMidi(note), t);
  const v = Math.max(0.03, Math.pow((vel/127), 1.3) * 0.35);
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(v, t + 0.01);
  osc.connect(gain).connect(master);
  osc.start(t);
  activeVoices.set(note, {osc, gain});
}
function synthNoteOff(note){
  if(!ac) return;
  const v = activeVoices.get(note);
  if(!v) return;
  const t = ac.currentTime;
  v.gain.gain.cancelScheduledValues(t);
  v.gain.gain.setTargetAtTime(0.0001, t, 0.03);
  v.osc.stop(t + 0.1);
  activeVoices.delete(note);
}
function synthAllNotesOff(){ if(!ac) return; for(const note of Array.from(activeVoices.keys())) synthNoteOff(note); }

// WebMIDI
async function initMIDI(){
  try{ midiAccess = await navigator.requestMIDIAccess({ sysex:false }); }catch(e){ midiAccess = null; }
  refreshOutputs();
  if(midiAccess){ midiAccess.onstatechange = refreshOutputs; }
}
function refreshOutputs(){
  const sel = document.getElementById('midiOut');
  const was = sel.value; sel.innerHTML = '';
  const optSynth = document.createElement('option'); optSynth.value='builtin'; optSynth.text='Built-in Synth'; sel.appendChild(optSynth);
  if(midiAccess){ [...midiAccess.outputs.values()].forEach(o=>{ const opt=document.createElement('option'); opt.value=o.id; opt.text=o.name; sel.appendChild(opt); }); }
  let pickVal = (was && [...sel.options].some(o=>o.value===was)) ? was : 'builtin';
  sel.value = pickVal; setOutput(pickVal);
}
function setOutput(id){
  useSynth = (id === 'builtin');
  if(!useSynth && midiAccess){ currentOut = [...midiAccess.outputs.values()].find(o=>o.id===id) || null; } else { currentOut = null; }
}
document.getElementById('midiOut').addEventListener('change', e => setOutput(e.target.value));

// MIDI send
function noteOn(note, vel=100){
  if(useSynth){ synthNoteOn(note, vel); return; }
  if(!currentOut) return;
  const ch = (currentChannel-1) & 0x0F;
  currentOut.send([0x90 | ch, note & 0x7F, vel & 0x7F]);
}
function noteOff(note){
  if(useSynth){ synthNoteOff(note); return; }
  if(!currentOut) return;
  const ch = (currentChannel-1) & 0x0F;
  currentOut.send([0x80 | ch, note & 0x7F, 0]);
}

// Helpers
function cssEscape(s){ return s.replace(/[^a-zA-Z0-9_-]/g, '_'); }
function pill(text){ return `<span class="pill">${text}</span>`; }
function labelMode(m){ const map={ionian:'Maj', aeolian:'min', lydian:'Lyd', mixolydian:'Mix', dorian:'Dor', phrygian:'Phr'}; return map[m] || m; }
function barDurationSeconds(ts, bpm){ const [num, den] = ts.split('/').map(x=>parseInt(x,10)); const q = 60 / bpm; return num * q * (4/den); }
function nextP2(bars){ let p=1; while(p<bars) p<<=1; return p; }

// SVG Piano Roll
function drawRollSVG(renderNotes, loopLenSec, barSec){
  const svg = document.getElementById('rollSvg');
  while(svg.firstChild) svg.removeChild(svg.firstChild);
  if(!renderNotes.length){ svg.setAttribute('viewBox','0 0 100 60'); return; }
  let minPitch = Math.min(...renderNotes.map(n=>n.p)), maxPitch = Math.max(...renderNotes.map(n=>n.p));
  const prange = Math.max(12, maxPitch-minPitch+1);
  const bars = Math.max(1, Math.ceil(loopLenSec / barSec));
  const pxPerBar = 220;
  const rowH = 8;
  const W = bars*pxPerBar + 60;
  const H = prange*rowH + 24;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const bg = document.createElementNS('http://www.w3.org/2000/svg','rect');
  bg.setAttribute('x',0); bg.setAttribute('y',0); bg.setAttribute('width',W); bg.setAttribute('height',H); bg.setAttribute('fill','#0a0f14');
  svg.appendChild(bg);

  for(let b=0;b<=bars;b++){
    const x = b*pxPerBar;
    const gl = document.createElementNS('http://www.w3.org/2000/svg','line');
    gl.setAttribute('x1',x+0.5); gl.setAttribute('y1',0); gl.setAttribute('x2',x+0.5); gl.setAttribute('y2',H);
    gl.setAttribute('class', b%4===0 ? 'barline' : 'gridline');
    svg.appendChild(gl);
    const t = document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x', x+4); t.setAttribute('y', 10); t.setAttribute('fill','#7a8696'); t.setAttribute('font-size','10'); t.textContent = String(b);
    svg.appendChild(t);
  }
  for(let i=0;i<prange;i++){
    const pc = (minPitch + i) % 12;
    if(pc===1||pc===3||pc===6||pc===8||pc===10){
      const y = (prange-1-i)*rowH + 18;
      const r = document.createElementNS('http://www.w3.org/2000/svg','rect');
      r.setAttribute('x',0); r.setAttribute('y',y-rowH); r.setAttribute('width',W); r.setAttribute('height',rowH);
      r.setAttribute('fill','rgba(255,255,255,0.02)');
      svg.appendChild(r);
    }
  }
  function velColor(v){ const t=v/127; const r=Math.round(80+t*110), g=Math.round(120+t*80), b=Math.round(200-t*120); return `rgb(${r},${g},${b})`; }
  currentRectMap.clear();
  for(const n of renderNotes){
    const x = (n.t / barSec) * pxPerBar;
    if(x >= W) continue;
    const widthSec = Math.min(n.d, Math.max(0, loopLenSec - n.t));
    if(widthSec <= 0) continue;
    const w = Math.max(2, Math.min((widthSec / barSec) * pxPerBar, pxPerBar*8));
    const y = (maxPitch - n.p) * rowH + 18;
    const rect = document.createElementNS('http://www.w3.org/2000/svg','rect');
    rect.setAttribute('x',x); rect.setAttribute('y',y-6); rect.setAttribute('width',w); rect.setAttribute('height',6);
    rect.setAttribute('fill', velColor(n.v)); rect.setAttribute('class','note');
    svg.appendChild(rect);
    const m = document.createElementNS('http://www.w3.org/2000/svg','circle');
    m.setAttribute('cx', x+1.5); m.setAttribute('cy', y-3); m.setAttribute('r', 1.5); m.setAttribute('class','marker');
    svg.appendChild(m);
    currentRectMap.set(n.idx, rect);
  }
  const loopX = (loopLenSec / barSec) * pxPerBar;
  const ll = document.createElementNS('http://www.w3.org/2000/svg','line');
  ll.setAttribute('x1',loopX+0.5); ll.setAttribute('y1',0); ll.setAttribute('x2',loopX+0.5); ll.setAttribute('y2',H); ll.setAttribute('class','loopline');
  svg.appendChild(ll);

  // Playhead line (updated by timer)
  const ph = document.createElementNS('http://www.w3.org/2000/svg','line');
  ph.setAttribute('x1',0); ph.setAttribute('y1',0); ph.setAttribute('x2',0); ph.setAttribute('y2',H); ph.setAttribute('class','playhead');
  ph.setAttribute('id','playhead');
  svg.appendChild(ph);
}

// Playback with live indicators
async function playLoop(relpath, rowId){
  stopAll();
  const url = '/api/raw?file='+encodeURIComponent(relpath);
  const midi = await Midi.fromUrl(url);
  const origBpm = (midi.header.tempos && midi.header.tempos.length) ? midi.header.tempos[0].bpm : 120;
  const scale = origBpm / currentTempo;

  const meta = fileData.find(f => f.relpath===relpath) || {};
  const normSemis = (normalizePreview && Number.isFinite(meta.transpose_to_C_same_mode)) ? (meta.transpose_to_C_same_mode % 12) : 0;
  const ts = meta.time_signature || '4/4';
  const barSec = barDurationSeconds(ts, currentTempo);
  const maxBarsInput = parseFloat(document.getElementById('maxBars').value);
  const limitSecs = (!isNaN(maxBarsInput) && maxBarsInput>0) ? maxBarsInput * barSec : Infinity;

  const notes=[]; let maxVelSeen = 1;
  midi.tracks.forEach(tr => {
    tr.notes.forEach(n => {
      let pitch = n.midi + normSemis;
      while(pitch<0) pitch+=12; while(pitch>127) pitch-=12;
      const v = Math.round((n.velocity||0.8)*127);
      if(v>maxVelSeen) maxVelSeen=v;
      notes.push({ time: n.time*scale, duration:n.duration*scale, midi:pitch, vel:v });
    });
  });
  notes.sort((a,b)=>a.time-b.time);
  const velFactor = (velScaleEnabled && maxVelSeen>0) ? (velTarget/maxVelSeen) : 1;
  const natural = midi.duration * scale;
  const baseLoop = Math.min(natural, limitSecs);
  const bars = baseLoop / barSec;
  const finalLoop = roundP2 ? Math.max(barSec, nextP2(Math.max(1, bars)) * barSec) : baseLoop;

  // Build renderNotes + draw
  currentRenderNotes = notes.map((n, idx)=>({ t:n.time, d:n.duration, p:n.midi, v:Math.max(1, Math.min(127, Math.round(n.vel * velFactor))), idx }))
                            .filter(n=>n.t < baseLoop);
  drawRollSVG(currentRenderNotes, finalLoop, barSec);

  // Schedule playback and highlighting
  const timers=[];
  const startMs = performance.now();
  currentRenderNotes.forEach(n => {
    const v = n.v;
    const onT = n.t; const offT = Math.min(n.t + n.d, baseLoop);
    timers.push(setTimeout(()=>{ noteOn(n.p, v); const r = currentRectMap.get(n.idx); if(r){ r.classList.add('active'); } }, Math.max(0, onT*1000)));
    timers.push(setTimeout(()=>{ noteOff(n.p); const r = currentRectMap.get(n.idx); if(r){ r.classList.remove('active'); } }, Math.max(0, offT*1000)));
  });
  // Loop
  timers.push(setTimeout(()=>{ if(playing && playing.id===rowId){ playLoop(relpath, rowId); } }, Math.max(0, finalLoop*1000)));
  // Playhead updater
  const svg = document.getElementById('rollSvg');
  const ph = () => {
    const phEl = document.getElementById('playhead');
    if(!playing || !phEl) return;
    const elapsed = (performance.now() - playing.startMs) / 1000;
    const t = elapsed % finalLoop;
    const barsWide = finalLoop / barSec;
    const pxPerBar = 220;
    const x = (t / barSec) * pxPerBar;
    const vb = svg.getAttribute('viewBox').split(' ').map(Number);
    const H = vb[3];
    phEl.setAttribute('x1', x+0.5); phEl.setAttribute('x2', x+0.5); phEl.setAttribute('y1', 0); phEl.setAttribute('y2', H);
  };
  playheadTimer = setInterval(ph, 33);

  playing = { id: rowId, timers, loopLen: finalLoop, startMs };

  document.querySelectorAll('.row .name').forEach(el=>el.classList.remove('playing'));
  const nameEl = document.querySelector(`#row-${cssEscape(rowId)} .name`);
  if(nameEl){ nameEl.classList.add('playing'); }
}

function stopAll(){
  if(playing){
    playing.timers.forEach(clearTimeout);
    playing=null;
  }
  if(playheadTimer){ clearInterval(playheadTimer); playheadTimer=null; }
  if(useSynth){ synthAllNotesOff(); }
  else if(currentOut){
    for(let ch=0; ch<16; ch++){ currentOut.send([0xB0|ch,123,0]); currentOut.send([0xB0|ch,120,0]); }
  }
  document.querySelectorAll('.row .name').forEach(el=>el.classList.remove('playing'));
  // Remove active highlight
  currentRectMap.forEach(rect => rect.classList.remove('active'));
}
document.getElementById('stopAll').addEventListener('click', stopAll);
window.addEventListener('beforeunload', stopAll);

// Files
async function loadFiles(){
  const res = await fetch('/api/files');
  const data = await res.json();
  fileData = data.files;
  renderList(fileData);
  document.getElementById('status').textContent = `${data.count} file(s) in ${data.root}`;
}
function renderList(files){
  const box = document.getElementById('fileList'); box.innerHTML='';
  files.forEach((f) => {
    const rowId = f.relpath;
    const div = document.createElement('div');
    div.className='row'; div.id = 'row-'+cssEscape(rowId);
    const keytxt = f.root && f.mode ? (f.root + ' ' + labelMode(f.mode)) : 'key: n/a';
    const classMap = {rhythmic_single_note:'Rhythmic', monophonic_melodic:'Mono', polyphonic_chordal:'Poly'};
    const classTxt = classMap[f.classification] || '';
    const warn16 = f.over16_unique ? '<span class="pill warn">>16 notes</span>' : '';
    const drumHint = f.uses_ch10 ? '<span class="pill">Suggest: Fixed</span>' : '';
    div.innerHTML = `
      <div><input type="checkbox" class="pick" data-rel="${encodeURIComponent(f.relpath)}"></div>
      <div class="name" title="Click to play">${f.filename}
        ${ warn16 }
        ${ drumHint }
        ${ f.time_signature ? `<span class="pill">${f.time_signature}</span>` : '' }
        ${ f.tempo_bpm ? `<span class="pill">${f.tempo_bpm} bpm</span>` : '' }
        <span class="pill">${(f.note_count||0)} notes</span>
        <span class="pill">${(f.unique_pitches||0)} uniq</span>
        ${ classTxt ? `<span class="pill">${classTxt}</span>` : '' }
        ${ keytxt ? `<span class="pill">${keytxt}</span>` : '' }
      </div>
      <div class="meta"></div>
      <div class="controls">
        <button class="play" data-rel="${encodeURIComponent(f.relpath)}">Play</button>
        <button class="stop" data-rel="${encodeURIComponent(f.relpath)}">Stop</button>
      </div>
    `;
    box.appendChild(div);
  });
  box.querySelectorAll('button.play').forEach(b=> b.addEventListener('click', e => {
    const rel = decodeURIComponent(e.currentTarget.dataset.rel);
    const id = rel; showDetails(rel); playLoop(rel, id);
  }));
  box.querySelectorAll('button.stop').forEach(b=> b.addEventListener('click', stopAll));
  box.querySelectorAll('.row .name').forEach(n=> n.addEventListener('click', e => {
    const row = e.currentTarget.closest('.row');
    const rel = decodeURIComponent(row.querySelector('.play').dataset.rel);
    const id = rel; showDetails(rel); playLoop(rel, id);
  }));
}

// Filter
document.getElementById('filterBox').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  const filtered = fileData.filter(f => {
    const fields = [f.filename, f.root, f.mode, f.time_signature, String(f.tempo_bpm), f.classification];
    return fields.filter(Boolean).some(s => String(s).toLowerCase().includes(q));
  });
  renderList(filtered);
});

// Copy selected
document.getElementById('copySelected').addEventListener('click', async () => {
  const picks = [...document.querySelectorAll('.pick:checked')].map(cb => decodeURIComponent(cb.dataset.rel));
  if(!picks.length){ alert('No files selected'); return; }
  const normalizeOnCopy = document.getElementById('normalizeOnCopy').checked;
  const maxBarsCopy = parseFloat(document.getElementById('maxBarsCopy').value);
  const force480 = document.getElementById('force480').checked;
  const velScaleExport = document.getElementById('velScaleExport').checked;
  const velTargetExport = parseInt(document.getElementById('velTargetExport').value);
  const res = await fetch('/api/copy', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ files: picks, normalize: normalizeOnCopy, max_bars: isNaN(maxBarsCopy)?null:maxBarsCopy, force480,
                           vel_scale: velScaleExport, vel_target: isNaN(velTargetExport)?null:velTargetExport }) });
  const data = await res.json();
  alert(`Copied ${data.copied.length} file(s) to /${data.dest}${data.normalized? ' (normalized)': ''}${data.max_bars? ' (truncated to '+data.max_bars+' bars)':''}${data.force480? ' (480 PPQN)': ''}${data.vel_scaled? ' (vel max '+data.vel_target+')':''}${data.errors.length? '\nErrors: '+JSON.stringify(data.errors):''}`);
});

// ZIP selected
document.getElementById('zipSelected').addEventListener('click', async () => {
  const picks = [...document.querySelectorAll('.pick:checked')].map(cb => decodeURIComponent(cb.dataset.rel));
  if(!picks.length){ alert('No files selected'); return; }
  const normalizeOnCopy = document.getElementById('normalizeOnCopy').checked;
  const maxBarsCopy = parseFloat(document.getElementById('maxBarsCopy').value);
  const force480 = document.getElementById('force480').checked;
  const velScaleExport = document.getElementById('velScaleExport').checked;
  const velTargetExport = parseInt(document.getElementById('velTargetExport').value);

  const res = await fetch('/api/export_zip', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ files: picks, normalize: normalizeOnCopy, max_bars: isNaN(maxBarsCopy)?null:maxBarsCopy, force480,
                           vel_scale: velScaleExport, vel_target: isNaN(velTargetExport)?null:velTargetExport }) });
  if(!res.ok){ alert('ZIP export failed'); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'exported_clips.zip';
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
});

// Pack 4
document.getElementById('pack4').addEventListener('click', async () => {
  const picks = [...document.querySelectorAll('.pick:checked')].map(cb => decodeURIComponent(cb.dataset.rel));
  if(picks.length===0){ alert('Select 1–4 files to pack'); return; }
  if(picks.length>4){ alert('Pick at most 4'); return; }
  const normalizeOnCopy = document.getElementById('normalizeOnCopy').checked;
  const maxBarsCopy = parseFloat(document.getElementById('maxBarsCopy').value);
  const res = await fetch('/api/pack4', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ files: picks, normalize: normalizeOnCopy, max_bars: isNaN(maxBarsCopy)?null:maxBarsCopy }) });
  const data = await res.json();
  if(data.error){ alert('Error: '+data.error); return; }
  alert(`Wrote ${data.packed} in /${data.dest}`);
});

function showDetails(rel){
  const f = fileData.find(x => x.relpath===rel);
  if(!f){ document.getElementById('details').innerHTML=''; return; }
  const semis = Number.isFinite(f.transpose_to_C_same_mode) ? f.transpose_to_C_same_mode : 'n/a';
  const classMap = {rhythmic_single_note:'Rhythmic (single-note)', monophonic_melodic:'Monophonic melodic', polyphonic_chordal:'Polyphonic/chordal'};
  document.getElementById('details').innerHTML = `
    <div><b>${f.filename}</b></div>
    <div>Key/Mode: <b>${f.root? f.root : 'n/a'} ${f.mode? labelMode(f.mode): ''}</b> <i>(${f.key_source || 'unknown'})</i></div>
    <div>Tempo (file): <b>${f.tempo_bpm || '—'} bpm</b></div>
    <div>Time Signature: <b>${f.time_signature}</b> | PPQ: <b>${f.ppq}</b></div>
    <div>Length (est): <b>${f.bars_estimate || 0}</b> bars</div>
    <div>Notes: <b>${f.note_count}</b> | Unique pitches: <b>${f.unique_pitches}</b> ${f.over16_unique?'<span class="pill warn">>16 (Yamaha limit)</span>':''}</div>
    <div>Max polyphony: <b>${f.max_polyphony}</b> | Class: <b>${classMap[f.classification]||''}</b></div>
    <div>Channels: <b>${(f.channels||[]).join(', ')||'n/a'}</b> ${f.uses_ch10?'<span class="pill">Suggest: Fixed</span>':''}</div>
    <div>Transpose to C (same mode): <b>${semis}</b> semitones</div>
    <div>Path: <code>${f.relpath}</code></div>
  `;
}

initMIDI();
loadFiles();
</script>
</body>
</html>
"""

def pack4_build(fpaths: List[Path], normalize: bool, max_bars: Optional[float], force_ppq:int=480) -> Path:
    tmp_tracks = []
    descriptors = []
    for src in fpaths[:4]:
        info = analyze_midi(src)
        semis = int(info['transpose_to_C_same_mode']) % 12 if (normalize and info.get('transpose_to_C_same_mode') is not None) else None
        mid = mido.MidiFile(src)
        numer, denom = 4,4
        for tr in mid.tracks:
            for msg in tr:
                if msg.type=='time_signature':
                    numer, denom = msg.numerator, msg.denominator
                    break
            else:
                continue
            break
        ppq = mid.ticks_per_beat
        limit_ticks = int(max_bars * ticks_per_bar(ppq, numer, denom)) if (max_bars and max_bars>0) else None
        factor = (force_ppq / ppq) if force_ppq != ppq else 1.0
        track_out = []
        active = {}
        for tr in mid.tracks:
            abs_events = rescaled_abs_events(tr, factor)
            for t_abs, msg in abs_events:
                m = msg.copy()
                if m.type in ('note_on','note_off') and semis is not None:
                    m.note = (m.note + semis) % 128
                if limit_ticks is not None and t_abs > int(limit_ticks * (force_ppq/ppq)):
                    continue
                track_out.append((t_abs, m))
                if m.type=='note_on' and getattr(m,'velocity',0)>0:
                    active[(getattr(m,'channel',0), m.note)] = True
                elif m.type in ('note_off','note_on') and (m.type=='note_off' or getattr(m,'velocity',0)==0):
                    active.pop((getattr(m,'channel',0), m.note), None)
        if limit_ticks is not None:
            t_limit = int(limit_ticks * (force_ppq/ppq))
            for (ch, note) in list(active.keys()):
                track_out.append((t_limit, mido.Message('note_off', note=note, velocity=0, channel=ch, time=0)))
        tmp_tracks.append(track_out)

        class_map = {'rhythmic_single_note':'Rhythmic','monophonic_melodic':'Mono','polyphonic_chordal':'Poly'}
        tag = class_map.get(info.get('classification',''), '')
        orgroot = 'C' if (normalize and info.get('transpose_to_C_same_mode') is not None) else (info.get('root') or '')
        parts = [src.stem]
        if tag: parts.append(tag)
        if orgroot: parts.append(f"OrgRoot={orgroot}")
        descriptors.append('_'.join(parts))

    out = mido.MidiFile(type=1, ticks_per_beat=force_ppq)
    for track_events in tmp_tracks:
        newt = rebuild_track_from_abs(track_events)
        out.tracks.append(newt)
    dest_dir = ROOT / 'selected'
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = "PACK4_" + ('+'.join(descriptors) if descriptors else 'untitled') + ".mid"
    if len(name) > 180: name = name[:176] + ".mid"
    dst = dest_dir / name
    out.save(dst)
    return dst

@app.route('/api/pack4', methods=['POST'])
def api_pack4():
    data = request.get_json(silent=True) or {}
    files = data.get('files', [])[:4]
    normalize = bool(data.get('normalize', False))
    max_bars = data.get('max_bars', None)
    try: max_bars = float(max_bars) if max_bars not in (None, '') else None
    except Exception: max_bars = None
    try:
        paths = [(ROOT/rel).resolve() for rel in files]
        for p in paths:
            if not str(p).startswith(str(ROOT.resolve())): return jsonify({"error":"Outside root"}), 400
            if not p.exists(): return jsonify({"error":f"Missing: {p.name}"}), 404
        dst = pack4_build(paths, normalize, max_bars, force_ppq=480)
        return jsonify({"packed": str(dst.relative_to(ROOT)), "dest":"selected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, help='Root folder containing .mid files')
    parser.add_argument('--port', type=int, default=8765, help='Port to run on (default 8765)')
    args = parser.parse_args()
    ROOT = Path(args.root).expanduser().resolve()
    if not ROOT.exists():
        raise SystemExit(f"Root folder not found: {ROOT}")
    app.run(host='127.0.0.1', port=args.port, debug=False)

if __name__ == '__main__':
    main()
