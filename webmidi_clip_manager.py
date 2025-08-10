#!/usr/bin/env python3
# webmidi_clip_manager.py
#
# Local web app to preview MIDI files in your browser via WebMIDI or a built-in WebAudio synth,
# select a subset, and copy to a "selected" subfolder.
#
# Features:
#  - Enumerates .mid files recursively under --root
#  - Shows tempo/time signature/notes/key+mode (from filename when present, else analysis)
#  - Preview: play/loop, Stop, choose MIDI output or Built-in Synth, pick channel, set tempo
#  - Normalize-to-C (same mode) preview toggle + Undo
#  - Velocity scaling preview (toggle + target-loudest velocity)
#  - Max bars control (applies to playback AND copy)
#  - Power-of-two loop rounding (preview) to avoid premature looping
#  - Basic piano roll display of the current file
#  - 2x2 layout: controls (TL), analysis (TR), list (BL), piano roll (BR)
#
# Requirements:
#   pip install flask mido
#
# Usage:
#   python webmidi_clip_manager.py --root "/path/to/your/midis" --port 8765
#   Then open http://localhost:8765 in Chrome/Edge (WebMIDI required for external MIDI).
#
import argparse
import re
import json
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Dict

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
    return s  # dorian, phrygian, lydian, mixolydian, ionian, aeolian

def parse_key_from_name(name: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    base = re.sub(r'\(.*?\)', '', name)  # strip parentheses
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
    # Fallback: first key-like token anywhere
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
        for m in tr:
            msgs.append(m.copy())
    t = 0
    for m in msgs:
        t += m.time
        m.time = t

    tempo_msg = next((m for m in msgs if m.type=='set_tempo'), None)
    tempo_bpm = round(mido.tempo2bpm(tempo_msg.tempo),2) if tempo_msg else 120.0
    ts_msg = next((m for m in msgs if m.type=='time_signature'), None)
    ts=(ts_msg.numerator, ts_msg.denominator) if ts_msg else (4,4)
    ppq = mid.ticks_per_beat
    bar_ticks = ticks_per_bar(ppq, *ts)

    note_on = {}
    notes = []
    for m in msgs:
        if m.type == 'note_on' and m.velocity>0:
            k=(m.channel, m.note)
            note_on.setdefault(k, []).append((m.time, m.velocity))
        elif m.type == 'note_off' or (m.type=='note_on' and m.velocity==0):
            k=(m.channel, m.note)
            if note_on.get(k):
                st, vel = note_on[k].pop(0)
                if m.time>st:
                    notes.append((st, m.time, m.note, vel, k[0]))
    notes.sort(key=lambda x:(x[0], x[2]))
    note_count = len(notes)
    end_ticks = max((n[1] for n in notes), default=0)
    est_bars = (end_ticks / bar_ticks) if bar_ticks>0 else 0

    # filename-derived key/mode
    root_name, root_pc, mode = parse_key_from_name(path.name)
    key_source = None
    if root_pc is not None and mode:
        key_source = 'filename'
    else:
        # duration-weighted pitch classes -> major/minor only
        pd = [(p, max(1, e - s)) for (s, e, p, vel, ch) in notes]
        gm = guess_major_minor(pd)
        if gm:
            root_pc, mode, _ = gm
            root_name = NOTE_NAME[root_pc]
            key_source = 'analysis'

    transpose_to_c = (0 - root_pc) % 12 if root_pc is not None else None

    return {
        "filename": path.name,
        "relpath": str(path.relative_to(ROOT)),
        "tempo_bpm": tempo_bpm,
        "time_signature": f"{ts[0]}/{ts[1]}",
        "ppq": ppq,
        "bars_estimate": round(est_bars, 3),
        "note_count": note_count,
        "root": root_name,
        "root_pc": root_pc,
        "mode": mode,  # ionian/aeolian/lydian/mixolydian/dorian/phrygian
        "key_source": key_source,
        "transpose_to_C_same_mode": transpose_to_c
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
    # Serve raw MIDI bytes
    rel = request.args.get('file')
    if not rel:
        return "Missing ?file=...", 400
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        return "Forbidden", 403
    if not p.exists() or p.suffix.lower()!='.mid':
        return "Not found", 404
    return send_file(p, mimetype='audio/midi', as_attachment=False, download_name=p.name)

def write_transposed_and_truncated(src: Path, dst: Path, semitones: Optional[int], max_bars: Optional[float]):
    """Write a (optionally) transposed + (optionally) truncated copy of src to dst."""
    mid = mido.MidiFile(src)
    # compute bar limit
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

    out = mido.MidiFile(type=mid.type, ticks_per_beat=ppq)
    for tr in mid.tracks:
        abs_t = 0
        events: List[Tuple[int, mido.Message]] = []
        active = {}  # (channel, note) -> True (after transpose)
        # collect events up to limit
        for msg in tr:
            abs_t += msg.time
            m = msg.copy()
            # transpose notes if requested
            if m.type in ('note_on','note_off') and semitones is not None:
                m.note = (m.note + semitones) % 128
            # handle limit
            if limit_ticks is not None and abs_t > limit_ticks:
                # ignore beyond limit; will close actives at limit
                continue
            # within limit: append
            if m.type == 'note_on' and m.velocity > 0:
                events.append((abs_t, m))
                active[(getattr(m, 'channel', 0), m.note)] = True
            elif m.type in ('note_off','note_on') and (m.type=='note_off' or m.velocity==0):
                events.append((abs_t, m))
                active.pop((getattr(m, 'channel', 0), m.note), None)
            else:
                events.append((abs_t, m))
        # close any active notes at limit
        if limit_ticks is not None:
            for (ch, note) in list(active.keys()):
                events.append((limit_ticks, mido.Message('note_off', note=note, velocity=0, channel=ch, time=0)))
        # sort and rebuild delta times
        events.sort(key=lambda x: (x[0], 0 if (getattr(x[1], 'type', '')=='note_on') else 1))
        newt = mido.MidiTrack()
        last = 0
        for t_abs, m in events:
            dt = t_abs - last
            last = t_abs
            m = m.copy(time=dt)
            newt.append(m)
        # ensure EOT
        newt.append(mido.MetaMessage('end_of_track', time=0))
        out.tracks.append(newt)
    out.save(dst)

@app.route('/api/copy', methods=['POST'])
def api_copy():
    data = request.get_json(silent=True) or {}
    files = data.get('files', [])
    normalize = bool(data.get('normalize', False))
    max_bars = data.get('max_bars', None)
    try:
        max_bars = float(max_bars) if max_bars not in (None, '') else None
    except Exception:
        max_bars = None

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
            if normalize and info.get('transpose_to_C_same_mode') is not None:
                semis = int(info['transpose_to_C_same_mode']) % 12
            mode = info.get('mode') or ''
            suffix = ""
            if semis is not None:
                suffix += f" - C {mode.capitalize()}" if mode else " - C"
            if max_bars and max_bars > 0:
                suffix += f" - max{int(max_bars)}bar" if float(max_bars).is_integer() else f" - max{max_bars}bar"
            dstname = src.stem + suffix + src.suffix
            dst = dest_dir / dstname
            write_transposed_and_truncated(src, dst, semis, max_bars)
            copied.append(str(dst.relative_to(ROOT)))
        except Exception as e:
            errors.append({"file": rel, "error": str(e)})
    return jsonify({"copied": copied, "errors": errors, "dest": str(dest_dir.relative_to(ROOT)), "normalized": normalize, "max_bars": max_bars})

INDEX_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>WebMIDI Clip Manager</title>
  <style>
    :root { --bg:#0b0d10; --fg:#e6edf3; --muted:#a9b1ba; --card:#12161a; --accent:#6bb3ff; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
           background:var(--bg); color:var(--fg); }
    h1 { margin:0; font-size:18px; letter-spacing:.3px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; grid-template-rows: auto 1fr; grid-template-areas:
            "controls details" "list roll"; gap:16px; padding:16px 20px; }
    .panel { background:var(--card); border:1px solid #1c2228; border-radius:14px; overflow:hidden; display:flex; flex-direction:column; }
    .panel header { padding:12px 14px; background:#11151a; border-bottom:1px solid #1c2228; }
    .panel header h2 { margin:0; font-size:14px; color:#cdd6df; }
    .body { padding:12px; display:flex; gap:12px; flex-wrap:wrap; align-items:flex-start; }
    .tool { background:var(--card); padding:10px 12px; border-radius:12px; border:1px solid #1c2228; }
    .tool label { font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }
    .tool select, .tool .opts { font-size:14px; }
    .tool .opts label { margin-right:10px; color:var(--fg); }
    .btn { background:#1a73e8; color:#fff; border:none; border-radius:10px; padding:10px 14px; cursor:pointer; font-weight:600; }
    .btn.secondary { background:#1f2937; color:#e6edf3; border:1px solid #2b3542; }
    .btn:disabled { opacity:.6; cursor:not-allowed; }
    .status { font-size:12px; color:#a9b1ba; }
    input[type="text"], input[type="number"] { background:#0d131a; color:#e6edf3; border:1px solid #202833; border-radius:8px; padding:8px 10px; }
    /* 2 rows of 8 for channel selector */
    #chanOpts { display:grid; grid-template-columns: repeat(8, auto); column-gap:8px; row-gap:4px; }
    /* list */
    #fileList { overflow-y:auto; }
    .row { display:grid; grid-template-columns: 28px 1fr auto auto; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid #151b21; }
    .row:hover { background:#0f1419; }
    .name { cursor:pointer; font-weight:600; }
    .pill { font-size:11px; padding:4px 8px; border-radius:20px; background:#0e1620; border:1px solid #1c2228; color:#cfd6dd; margin-left:6px; }
    .controls button { margin-left:8px; background:#1f2937; color:#e6edf3; border:1px solid #2b3542; border-radius:8px; padding:6px 10px; cursor:pointer; }
    .controls button:hover { border-color:#3c4858; }
    .playing { color:#7ee787; }
    /* piano roll */
    #rollCanvas { width:100%; height:100%; background:#0a0f14; display:block; }
    #rollWrap { overflow:auto; height:100%; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/@tonejs/midi@2.0.28/build/Midi.min.js"></script>
</head>
<body>
  <div class="grid">
    <!-- Controls TL -->
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
            <label>Target loudest: <input type="number" id="velTarget" min="1" max="127" value="100" style="width:80px"></label>
          </div>
        </div>
        <div class="tool">
          <label>Max bars</label>
          <div><input type="number" id="maxBars" min="1" step="0.5" value="4" style="width:80px"></div>
        </div>
        <div class="tool">
          <label>Round loop to power-of-two bars</label>
          <div><input type="checkbox" id="roundP2"></div>
        </div>
        <div class="tool">
          <label>Playback</label>
          <button class="btn" id="stopAll">Stop</button>
        </div>
        <div class="tool">
          <label>Filter</label>
          <input type="text" id="filterBox" placeholder="Search name/key/mode"/>
        </div>
        <div class="tool">
          <label>Copy selected</label>
          <div class="opts">
            <label><input type="checkbox" id="normalizeOnCopy"/> Apply normalization</label>
            <label>Max bars: <input type="number" id="maxBarsCopy" min="1" step="0.5" value="4" style="width:80px"></label>
          </div>
          <button id="copySelected">Copy → <b>/selected</b></button>
        </div>
        <div class="status" id="status">Loading…</div>
      </div>
    </section>

    <!-- Analysis TR -->
    <section class="panel" style="grid-area:details;">
      <header><h2>Analysis</h2></header>
      <div class="body" id="details" style="min-height:120px"></div>
    </section>

    <!-- List BL -->
    <section class="panel" style="grid-area:list; min-height:300px;">
      <header><h2>Files</h2></header>
      <div class="body" id="fileList" style="height: calc(65vh - 60px);"></div>
    </section>

    <!-- Piano Roll BR -->
    <section class="panel" style="grid-area:roll; min-height:300px;">
      <header><h2>Piano Roll</h2></header>
      <div id="rollWrap">
        <canvas id="rollCanvas"></canvas>
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
let playing = null; // { id, timers:[], loopLen }
let fileData = [];  // from API
let normalizePreview = false;
let normalizeHistory = []; // stack of booleans
let activeVoices = new Map(); // note -> {osc,gain}
let velScaleEnabled = false;
let velTarget = 100;
let roundP2 = false;

// Build channel radios (2 x 8 grid via CSS)
(function buildChanRadios(){
  const cont = document.getElementById('chanOpts');
  for(let i=1;i<=16;i++){
    const lab=document.createElement('label');
    lab.innerHTML = '<input type="radio" name="chan" value="'+i+'" '+(i===1?'checked':'')+'>'+i;
    cont.appendChild(lab);
  }
  cont.addEventListener('change', e => {
    if(e.target.name==='chan'){ currentChannel = parseInt(e.target.value); }
  });
})();

// Tempo radios
document.querySelectorAll('input[name="tempo"]').forEach(r => {
  r.addEventListener('change', e => {
    currentTempo = parseInt(e.target.value);
    if(playing){
      const row = document.getElementById('row-'+cssEscape(playing.id));
      if(row){
        const rel = decodeURIComponent(row.querySelector('.play').dataset.rel);
        playLoop(rel, playing.id);
      }
    }
  });
});

// Velocity scaling controls
document.getElementById('velScaleToggle').addEventListener('change', e => {
  velScaleEnabled = !!e.target.checked;
  if(playing){ restartCurrent(); }
});
document.getElementById('velTarget').addEventListener('change', e => {
  const v = parseInt(e.target.value);
  velTarget = Math.max(1, Math.min(127, isNaN(v)?100:v));
  e.target.value = velTarget;
  if(playing){ restartCurrent(); }
});

// Normalize toggle + undo
const toggleBtn = document.getElementById('toggleNormalize');
const undoBtn = document.getElementById('undoNormalize');
function updateNormalizeUI(){
  toggleBtn.textContent = normalizePreview ? 'On' : 'Off';
  toggleBtn.classList.toggle('secondary', true);
}
toggleBtn.addEventListener('click', () => {
  normalizeHistory.push(normalizePreview);
  normalizePreview = !normalizePreview;
  updateNormalizeUI();
  if(playing){ restartCurrent(); }
});
undoBtn.addEventListener('click', () => {
  if(normalizeHistory.length){
    normalizePreview = normalizeHistory.pop();
    updateNormalizeUI();
    if(playing){ restartCurrent(); }
  }
});
updateNormalizeUI();

// Round to power-of-two bars
document.getElementById('roundP2').addEventListener('change', e => {
  roundP2 = !!e.target.checked;
  if(playing){ restartCurrent(); }
});

function restartCurrent(){
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
  const v = Math.max(0.04, Math.pow((vel/127), 1.5) * 0.35);
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(v, t + 0.01); // quick attack
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
  v.gain.gain.setTargetAtTime(0.0001, t, 0.03); // short release
  v.osc.stop(t + 0.1);
  activeVoices.delete(note);
}
function synthAllNotesOff(){
  if(!ac) return;
  for(const note of Array.from(activeVoices.keys())){
    synthNoteOff(note);
  }
}

// WebMIDI setup
async function initMIDI(){
  try{
    midiAccess = await navigator.requestMIDIAccess({ sysex:false });
  }catch(e){
    midiAccess = null;
  }
  refreshOutputs();
  if(midiAccess){
    midiAccess.onstatechange = refreshOutputs;
  }
}
function refreshOutputs(){
  const sel = document.getElementById('midiOut');
  const was = sel.value;
  sel.innerHTML = '';
  // Always offer built-in synth
  const optSynth = document.createElement('option');
  optSynth.value = 'builtin';
  optSynth.text = 'Built-in Synth';
  sel.appendChild(optSynth);
  if(midiAccess){
    const outs=[...midiAccess.outputs.values()];
    outs.forEach((o)=>{
      const opt=document.createElement('option');
      opt.value=o.id; opt.text=o.name;
      sel.appendChild(opt);
    });
  }
  // pick previous or default to synth
  let pickVal = 'builtin';
  if(was && [...sel.options].some(o=>o.value===was)){ pickVal = was; }
  sel.value = pickVal;
  setOutput(pickVal);
}
function setOutput(id){
  useSynth = (id === 'builtin');
  if(!useSynth && midiAccess){
    currentOut = [...midiAccess.outputs.values()].find(o=>o.id===id) || null;
  }else{
    currentOut = null;
  }
}
document.getElementById('midiOut').addEventListener('change', e => setOutput(e.target.value));

// MIDI helpers
function noteOn(note, vel=100){
  if(useSynth){
    synthNoteOn(note, vel);
    return;
  }
  if(!currentOut) return;
  const ch = (currentChannel-1) & 0x0F;
  currentOut.send([0x90 | ch, note & 0x7F, vel & 0x7F]);
}
function noteOff(note){
  if(useSynth){
    synthNoteOff(note);
    return;
  }
  if(!currentOut) return;
  const ch = (currentChannel-1) & 0x0F;
  currentOut.send([0x80 | ch, note & 0x7F, 0]);
}

// Helpers
function cssEscape(s){ return s.replace(/[^a-zA-Z0-9_-]/g, '_'); }
function pill(text){ return `<span class="pill">${text}</span>`; }
function labelMode(m){
  const map={ionian:'Maj', aeolian:'min', lydian:'Lyd', mixolydian:'Mix', dorian:'Dor', phrygian:'Phr'};
  return map[m] || m;
}
function barDurationSeconds(ts, bpm){
  const [num, den] = ts.split('/').map(x=>parseInt(x,10));
  const q = 60 / bpm; // quarter note seconds
  return num * q * (4/den);
}
function nextPowerOfTwoBars(bars){
  let p = 1;
  while(p < bars) p <<= 1;
  return p;
}

// Playback via @tonejs/midi; supports normalize-to-C, velocity scaling, max bars, and p2 rounding
async function playLoop(relpath, rowId){
  stopAll();
  const url = '/api/raw?file='+encodeURIComponent(relpath);
  const midi = await Midi.fromUrl(url);

  // tempo scaling: slower tempo => larger time scaling. Scale = orig / target.
  const origBpm = (midi.header.tempos && midi.header.tempos.length) ? midi.header.tempos[0].bpm : 120;
  const scale = origBpm / currentTempo;

  // Fetch metadata for ts and transpose semitones
  const meta = fileData.find(f => f.relpath===relpath) || {};
  const normSemis = (normalizePreview && Number.isFinite(meta.transpose_to_C_same_mode)) ? (meta.transpose_to_C_same_mode % 12) : 0;

  // Determine bar-based limits
  const maxBarsInput = parseFloat(document.getElementById('maxBars').value);
  const ts = meta.time_signature || '4/4';
  const barSec = barDurationSeconds(ts, currentTempo);
  let maxPlaySecs = Infinity;
  if(!isNaN(maxBarsInput) && maxBarsInput>0){
    maxPlaySecs = maxBarsInput * barSec;
  }

  // Build combined notes with optional transpose and velocity scaling
  const notes=[];
  let maxVelSeen = 1;
  midi.tracks.forEach(tr => {
    tr.notes.forEach(n => {
      let pitch = n.midi + normSemis;
      while(pitch<0) pitch+=12;
      while(pitch>127) pitch-=12;
      const vel127 = Math.round((n.velocity || 0.8) * 127);
      if(vel127 > maxVelSeen) maxVelSeen = vel127;
      notes.push({
        time: n.time * scale,
        duration: n.duration * scale,
        midi: pitch,
        velocity: vel127
      });
    });
  });
  notes.sort((a,b)=>a.time - b.time);

  // Velocity scaling (loudest => velTarget, others proportional)
  let velFactor = 1;
  if(velScaleEnabled && maxVelSeen>0){
    velFactor = velTarget / maxVelSeen;
  }
  // schedule with truncation
  const timers=[];
  const endNatural = midi.duration * scale;
  const endByBars = isFinite(maxPlaySecs) ? maxPlaySecs : endNatural;
  const loopLen = Math.min(endNatural, endByBars);
  const loopBars = loopLen / barSec;
  const loopLenFinal = (roundP2 ? nextPowerOfTwoBars(Math.max(1, loopBars)) * barSec : loopLen);

  // Draw piano roll
  drawRoll(midi, scale, normSemis, velFactor, loopLenFinal, barSec);

  // schedule notes (respect truncation at loopLen)
  for(const n of notes){
    if(n.time >= loopLen) continue; // don't start beyond loop length
    const noteOffTime = Math.min(n.time + n.duration, loopLen);
    const v = Math.max(1, Math.min(127, Math.round(n.velocity * velFactor)));
    timers.push(setTimeout(()=>noteOn(n.midi, v), Math.max(0, n.time*1000)));
    timers.push(setTimeout(()=>noteOff(n.midi), Math.max(0, noteOffTime*1000)));
  }
  // loop
  timers.push(setTimeout(()=>{
    if(playing && playing.id===rowId){
      playLoop(relpath, rowId);
    }
  }, Math.max(0, loopLenFinal*1000)));
  playing = { id: rowId, timers, loopLen: loopLenFinal };

  // UI
  document.querySelectorAll('.row .name').forEach(el=>el.classList.remove('playing'));
  const nameEl = document.querySelector(`#row-${cssEscape(rowId)} .name`);
  if(nameEl){ nameEl.classList.add('playing'); }
}

function stopAll(){
  if(playing){
    playing.timers.forEach(t=>clearTimeout(t));
    playing=null;
  }
  if(useSynth){
    synthAllNotesOff();
  } else if(currentOut){
    for(let ch=0; ch<16; ch++){
      currentOut.send([0xB0 | ch, 123, 0]); // All Notes Off
      currentOut.send([0xB0 | ch, 120, 0]); // All Sound Off
    }
  }
  document.querySelectorAll('.row .name').forEach(el=>el.classList.remove('playing'));
}

// Piano roll drawing
function drawRoll(midi, scale, normSemis, velFactor, loopLenSec, barSec){
  const canvas = document.getElementById('rollCanvas');
  const wrap = document.getElementById('rollWrap');
  // compute note extents
  let minPitch = 127, maxPitch = 0;
  const notes=[];
  midi.tracks.forEach(tr => {
    tr.notes.forEach(n => {
      let pitch = n.midi + normSemis;
      while(pitch<0) pitch+=12;
      while(pitch>127) pitch-=12;
      minPitch = Math.min(minPitch, pitch);
      maxPitch = Math.max(maxPitch, pitch);
      notes.push({
        time: n.time * scale,
        dur: n.duration * scale,
        midi: pitch,
        vel: Math.max(1, Math.min(127, Math.round((n.velocity||0.8)*127 * velFactor)))
      });
    });
  });
  if(minPitch>maxPitch){ minPitch=60; maxPitch=72; }
  const pitchRange = Math.max(12, maxPitch - minPitch + 1);
  // layout sizes
  const pxPerBar = 180; // width scale
  const totalBars = Math.max(1, Math.ceil(loopLenSec / barSec));
  const W = totalBars * pxPerBar + 60;
  const rowH = 8; // pixels per semitone
  const H = pitchRange * rowH + 20;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  // bg
  ctx.fillStyle = '#0a0f14';
  ctx.fillRect(0,0,W,H);
  // grid
  for(let b=0;b<=totalBars;b++){
    const x = b*pxPerBar;
    ctx.strokeStyle = b%4===0 ? '#203040' : '#15202a';
    ctx.beginPath(); ctx.moveTo(x+0.5,0); ctx.lineTo(x+0.5,H); ctx.stroke();
    // bar labels
    ctx.fillStyle = '#7a8696';
    ctx.font = '10px system-ui';
    ctx.fillText(String(b), x+4, 10);
  }
  // key rows
  for(let p=0;p<pitchRange;p++){
    const y = (pitchRange - 1 - p) * rowH + 20;
    if(((minPitch+p)%12)===1 || ((minPitch+p)%12)===3 || ((minPitch+p)%12)===6 || ((minPitch+p)%12)===8 || ((minPitch+p)%12)===10){
      ctx.fillStyle = 'rgba(255,255,255,0.02)';
      ctx.fillRect(0,y-rowH,W,rowH);
    }
  }
  // notes
  function velColor(v){
    const t = v/127;
    const r = Math.round(80 + t*110);
    const g = Math.round(120 + t*80);
    const b = Math.round(200 - t*120);
    return 'rgb('+r+','+g+','+b+')';
  }
  ctx.lineWidth = 1;
  notes.forEach(n => {
    if(n.time >= loopLenSec) return;
    const x = (n.time / barSec) * pxPerBar;
    const w = Math.max(2, Math.min(((Math.min(n.dur, loopLenSec - n.time)) / barSec) * pxPerBar, pxPerBar*4));
    const y = (maxPitch - n.midi) * rowH + 20;
    ctx.fillStyle = velColor(n.vel);
    ctx.fillRect(x, y-6, w, 6);
    ctx.strokeStyle = 'rgba(0,0,0,0.35)';
    ctx.strokeRect(x+0.5, y-6+0.5, w-1, 6-1);
  });
  // loop end line
  const loopX = (loopLenSec / barSec) * pxPerBar;
  ctx.strokeStyle = '#ff5555';
  ctx.beginPath(); ctx.moveTo(loopX+0.5,0); ctx.lineTo(loopX+0.5,H); ctx.stroke();

  // ensure visible
  wrap.scrollLeft = 0;
}

// Fetch file list + render
async function loadFiles(){
  const res = await fetch('/api/files');
  const data = await res.json();
  fileData = data.files;
  renderList(fileData);
  document.getElementById('status').textContent = `${data.count} file(s) in ${data.root}`;
}

function renderList(files){
  const box = document.getElementById('fileList');
  box.innerHTML='';
  files.forEach((f) => {
    const rowId = f.relpath;
    const div = document.createElement('div');
    div.className='row';
    div.id = 'row-'+cssEscape(rowId);
    const keytxt = f.root && f.mode ? (f.root + ' ' + labelMode(f.mode)) : 'key: n/a';
    div.innerHTML = `
      <div><input type="checkbox" class="pick" data-rel="${encodeURIComponent(f.relpath)}"></div>
      <div class="name" title="Click to play">${f.filename}
        ${ pill(keytxt) }
        ${ pill(f.time_signature || '') }
        ${ pill(f.tempo_bpm ? (f.tempo_bpm + ' bpm') : '—') }
        ${ pill((f.note_count||0) + ' notes') }
      </div>
      <div class="meta">${ f.key_source ? ('key via ' + f.key_source) : '' }</div>
      <div class="controls">
        <button class="play" data-rel="${encodeURIComponent(f.relpath)}">Play</button>
        <button class="stop" data-rel="${encodeURIComponent(f.relpath)}">Stop</button>
      </div>
    `;
    box.appendChild(div);
  });
  // wire
  box.querySelectorAll('button.play').forEach(b=>{
    b.addEventListener('click', e => {
      const rel = decodeURIComponent(e.currentTarget.dataset.rel);
      const id = rel;
      showDetails(rel);
      playLoop(rel, id);
    });
  });
  box.querySelectorAll('button.stop').forEach(b=>{
    b.addEventListener('click', e => {
      stopAll();
    });
  });
  box.querySelectorAll('.row .name').forEach(n=>{
    n.addEventListener('click', e => {
      const row = e.currentTarget.closest('.row');
      const rel = decodeURIComponent(row.querySelector('.play').dataset.rel);
      const id = rel;
      showDetails(rel);
      playLoop(rel, id);
    });
  });
}

// filter
document.getElementById('filterBox').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  const filtered = fileData.filter(f => {
    const fields = [f.filename, f.root, f.mode, f.time_signature, String(f.tempo_bpm)];
    return fields.filter(Boolean).some(s => String(s).toLowerCase().includes(q));
  });
  renderList(filtered);
});

// copy selected (with optional normalization + max bars)
document.getElementById('copySelected').addEventListener('click', async () => {
  const picks = [...document.querySelectorAll('.pick:checked')].map(cb => decodeURIComponent(cb.dataset.rel));
  if(!picks.length){ alert('No files selected'); return; }
  const normalizeOnCopy = document.getElementById('normalizeOnCopy').checked;
  const maxBarsCopy = parseFloat(document.getElementById('maxBarsCopy').value);
  const res = await fetch('/api/copy', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ files: picks, normalize: normalizeOnCopy, max_bars: isNaN(maxBarsCopy)?null:maxBarsCopy }) });
  const data = await res.json();
  alert(`Copied ${data.copied.length} file(s) to /${data.dest}${data.normalized? ' (normalized)': ''}${data.max_bars? ' (truncated to '+data.max_bars+' bars)':''}${data.errors.length? '\nErrors: '+JSON.stringify(data.errors):''}`);
});

function showDetails(rel){
  const f = fileData.find(x => x.relpath===rel);
  if(!f){ document.getElementById('details').innerHTML=''; return; }
  const semis = Number.isFinite(f.transpose_to_C_same_mode) ? f.transpose_to_C_same_mode : 'n/a';
  document.getElementById('details').innerHTML = `
    <div><b>${f.filename}</b></div><br/>
    <div>Key/Mode: <b>${f.root? f.root : 'n/a'} ${f.mode? labelMode(f.mode): ''}</b> <i>(${f.key_source || 'unknown'})</i></div>
    <div>Tempo (file): <b>${f.tempo_bpm || '—'} bpm</b></div>
    <div>Time Signature: <b>${f.time_signature}</b> | PPQ: <b>${f.ppq}</b></div>
    <div>Length (est): <b>${f.bars_estimate || 0}</b> bars</div>
    <div>Notes: <b>${f.note_count}</b></div>
    <div>Transpose to C (same mode): <b>${semis}</b> semitones</div>
    <div>Path: <code>${f.relpath}</code></div>
  `;
}

// Safety
document.getElementById('stopAll').addEventListener('click', stopAll);
window.addEventListener('beforeunload', stopAll);

// Start
initMIDI();
loadFiles();
</script>
</body>
</html>
"""

def main():
    global ROOT
    import argparse
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
