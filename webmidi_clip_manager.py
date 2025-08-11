#!/usr/bin/env python3
# webmidi_clip_manager.py — revert Files pane layout to the stable version (no header row),
# keep scrolling grid, short-loop behavior, star (multitrack) selection, and aligned Track Map.
import argparse, re, io, zipfile
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set
from flask import Flask, send_file, request, jsonify, Response
import mido

app = Flask(__name__)
ROOT = Path.cwd()

NOTE_NAME = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
PC = {'c':0,'c#':1,'db':1,'d':2,'d#':3,'eb':3,'e':4,'f':5,'f#':6,'gb':6,'g':7,'g#':8,'ab':8,'a':9,'a#':10,'bb':10,'b':11}
MAJOR_PROFILE = [6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88]
MINOR_PROFILE = [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]

def norm_mode(s: str) -> str:
    s = s.lower()
    if s in ('maj','major'): return 'ionian'
    if s in ('min','minor','m'): return 'aeolian'
    return s

def parse_key_from_name(name: str):
    base = re.sub(r'\(.*?\)', '', name).replace('.mid','').strip()
    pats = [
        r'(?P<root>[A-Ga-g](?:#|b)?)\s*(?P<mode>lydian|mixolydian|dorian|phrygian|ionian|major|minor)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)(?P<mode>lydian|mixolydian|dorian|phrygian|ionian|major|minor)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)\s*(?P<mode>maj|min|m)\b',
        r'(?P<root>[A-Ga-g](?:#|b)?)(?P<mode>maj|min|m)\b',
        r'\b(?P<root>[A-Ga-g](?:#|b)?)\s*$',
    ]
    for pat in pats:
        m = re.search(pat, base, flags=re.IGNORECASE)
        if m:
            root = m.group('root'); mode = m.groupdict().get('mode','ionian')
            root_pc = PC.get(root.lower(), None)
            return root, root_pc, norm_mode(mode) if mode else 'ionian'
    return None, None, None

def guess_major_minor(pitches_dur: List[Tuple[int,int]]):
    if not pitches_dur: return None
    pc_hist = [0.0]*12
    for p,d in pitches_dur: pc_hist[p%12] += d
    s = sum(pc_hist) or 1.0; pc_hist = [x/s for x in pc_hist]
    def corr(profile):
        best=(-1.0,0)
        for r in range(12):
            rot = profile[-r:]+profile[:-r]
            score = sum(a*b for a,b in zip(pc_hist, rot))
            if score>best[0]: best=(score,r)
        return best
    ms, mr = corr(MAJOR_PROFILE); ns, nr = corr(MINOR_PROFILE)
    return (mr,'ionian',ms) if ms>=ns else (nr,'aeolian',ns)

def ticks_per_bar(ppq: int, numer: int, denom: int) -> int:
    return int(round(numer * (ppq * 4 / denom)))

def _track_stats(abs_msgs: List[mido.Message], ppq:int, numer:int, denom:int) -> Dict:
    note_on_map: Dict[Tuple[int,int], List[Tuple[int,int,int]]] = {}
    unique_pitches: Set[int] = set(); channels_used: Set[int] = set()
    events=[]; notes=[]
    for m in abs_msgs:
        if m.type=='note_on' and m.velocity>0:
            ch=getattr(m,'channel',0); k=(ch,m.note)
            note_on_map.setdefault(k,[]).append((m.time,m.velocity,ch))
            unique_pitches.add(m.note); channels_used.add(ch); events.append((m.time,+1))
        elif m.type in ('note_off','note_on') and (m.type=='note_off' or m.velocity==0):
            ch=getattr(m,'channel',0); k=(ch,m.note)
            if note_on_map.get(k):
                st, vel, ch = note_on_map[k].pop(0)
                if m.time>st: notes.append((st,m.time,m.note,vel,ch)); events.append((m.time,-1))
    notes.sort(key=lambda x:(x[0],x[2]))
    note_count=len(notes); bar_ticks=ticks_per_bar(ppq,numer,denom)
    end_ticks=max((n[1] for n in notes), default=0)
    est_bars=(end_ticks/bar_ticks) if bar_ticks>0 else 0
    max_poly=0; cur=0
    for t,delta in sorted(events,key=lambda x:(x[0],-x[1])):
        cur+=delta; max_poly=max(max_poly,cur)
    if len(unique_pitches)<=1: classification='rhythmic_single_note'
    elif max_poly<=1: classification='monophonic_melodic'
    else: classification='polyphonic_chordal'
    pd=[(p,max(1,e-s)) for (s,e,p,vel,ch) in notes]
    gm=guess_major_minor(pd) if pd else None
    if gm: r_pc,mode,score=gm; r_name=NOTE_NAME[r_pc]
    else: r_pc=mode=r_name=score=None
    return {
        "note_count": note_count, "unique_pitches": len(unique_pitches),
        "over16_unique": len(unique_pitches)>16, "channels": sorted(list(channels_used)),
        "uses_ch10": (9 in channels_used), "max_polyphony": max_poly,
        "bars_estimate": round(est_bars,3), "notes_raw": notes,
        "root_pc": r_pc, "root": r_name, "mode": mode, "key_score": score
    }

def analyze_midi(path: Path) -> Dict:
    try: mid=mido.MidiFile(path)
    except Exception as e: return {"filename": path.name, "relpath": str(path.relative_to(ROOT)), "error": f"open failed: {e}"}
    numer,denom=4,4; tempo_bpm=None; per_track_abs=[]
    for tr in mid.tracks:
        t=0; abs_msgs=[]
        for m in tr:
            t+=m.time; abs_msgs.append(m.copy(time=t))
            if tempo_bpm is None and getattr(m,'type','')=='set_tempo':
                try: tempo_bpm=round(mido.tempo2bpm(m.tempo),2)
                except: pass
            if getattr(m,'type','')=='time_signature' and (numer,denom)==(4,4):
                numer,denom=m.numerator,m.denominator
        per_track_abs.append(abs_msgs)
    if tempo_bpm is None: tempo_bpm=120.0
    ppq=mid.ticks_per_beat
    tracks_summary=[]
    for idx,abs_msgs in enumerate(per_track_abs):
        stats=_track_stats([m for m in abs_msgs if hasattr(m,'type')], ppq, numer, denom)
        tr_name=next((m.name for m in abs_msgs if getattr(m,'type','')=='track_name'), f"Track {idx+1}")
        tracks_summary.append({"index":idx,"name":tr_name, **{k:stats[k] for k in ("note_count","unique_pitches","over16_unique","channels","uses_ch10","max_polyphony","bars_estimate","root_pc","root","mode")}})
    all_msgs=[m for abs_msgs in per_track_abs for m in abs_msgs if hasattr(m,'type')]
    file_stats=_track_stats(all_msgs, ppq, numer, denom)
    root_name,root_pc,mode=parse_key_from_name(path.name); key_source=None
    if root_pc is not None and mode: key_source='filename'
    else:
        if file_stats["root_pc"] is not None:
            root_pc=file_stats["root_pc"]; mode=file_stats["mode"]; root_name=NOTE_NAME[root_pc]; key_source='analysis'
    transpose_to_c=(0-root_pc)%12 if root_pc is not None else None
    bars_estimate=file_stats["bars_estimate"]; max_poly=file_stats["max_polyphony"]
    classification=('rhythmic_single_note' if file_stats["unique_pitches"]<=1 else ('monophonic_melodic' if max_poly<=1 else 'polyphonic_chordal'))
    return {
        "filename": path.name, "relpath": str(path.relative_to(ROOT)), "tempo_bpm": tempo_bpm,
        "time_signature": f"{numer}/{denom}", "ppq": ppq, "bars_estimate": bars_estimate,
        "note_count": file_stats["note_count"], "root": root_name, "root_pc": root_pc,
        "mode": mode, "key_source": key_source, "transpose_to_C_same_mode": transpose_to_c,
        "unique_pitches": file_stats["unique_pitches"], "over16_unique": file_stats["over16_unique"],
        "channels": sorted(list({ch for ts in tracks_summary for ch in ts["channels"]})),
        "uses_ch10": any(ts["uses_ch10"] for ts in tracks_summary),
        "max_polyphony": max_poly, "classification": classification,
        "track_count": len(tracks_summary), "tracks": tracks_summary,
    }

def iter_midis(root: Path):
    for p in sorted(root.rglob('*.mid')): yield p

@app.route('/')
def index(): return Response(INDEX_HTML, mimetype='text/html')

@app.route('/api/files')
def api_files():
    files=[analyze_midi(p) for p in iter_midis(ROOT)]
    return jsonify({"root": str(ROOT), "count": len(files), "files": files})

@app.route('/api/raw')
def api_raw():
    rel=request.args.get('file'); 
    if not rel: return "Missing ?file=...", 400
    p=(ROOT/rel).resolve()
    if not str(p).startswith(str(ROOT.resolve())): return "Forbidden", 403
    if not p.exists() or p.suffix.lower()!='.mid': return "Not found", 404
    return send_file(p, mimetype='audio/midi', as_attachment=False, download_name=p.name)

# -------- export helpers --------
def rescaled_abs_events(track: mido.MidiTrack, factor: float):
    events=[]; t=0
    for msg in track:
        t+=msg.time; tt=int(round(t*factor)) if factor!=1.0 else t
        events.append((tt, msg.copy(time=0)))
    return events

def rebuild_track_from_abs(events):
    events.sort(key=lambda x:(x[0], 0 if getattr(x[1],'type','')=='note_on' else 1))
    newt=mido.MidiTrack(); last=0
    for t_abs,m in events:
        dt=max(0,t_abs-last); last=t_abs; newt.append(m.copy(time=dt))
    newt.append(mido.MetaMessage('end_of_track', time=0)); return newt

def ticks_per_bar_ppq(ppq, numer, denom): return int(round(numer*(ppq*4/denom)))

def write_processed(src:Path, dst:Path, normalization_mode:str, per_track=None, force_ppq=None, max_bars:Optional[float]=None):
    mid=mido.MidiFile(src); numer,denom=4,4
    for tr in mid.tracks:
        for msg in tr:
            if msg.type=='time_signature': numer,denom=msg.numerator,msg.denominator; break
        else: continue
        break
    ppq=mid.ticks_per_beat
    limit_ticks=int(max_bars*ticks_per_bar_ppq(ppq,numer,denom)) if (max_bars and max_bars>0) else None
    info=analyze_midi(src); global_semis=info.get('transpose_to_C_same_mode',None) if normalization_mode=='global' else None
    target_ppq=force_ppq or ppq; factor=(target_ppq/ppq) if target_ppq!=ppq else 1.0
    max_vel_per_track={}
    for ti,tr in enumerate(mid.tracks):
        mv=0
        for msg in tr:
            if msg.type=='note_on' and msg.velocity>0: mv=max(mv,msg.velocity)
        max_vel_per_track[ti]=mv
    out=mido.MidiFile(type=1, ticks_per_beat=target_ppq)
    for ti,tr in enumerate(mid.tracks):
        abs_events=rescaled_abs_events(tr, factor); processed=[]; active={}
        pmap=(per_track or {}).get(ti,{})
        out_ch=pmap.get('out_ch',None); tr_semitones=pmap.get('transpose',0) or 0
        vel_target=pmap.get('vel_target',None); drums=bool(pmap.get('drums',False))
        if normalization_mode=='off': semis=0
        elif normalization_mode=='global': semis=(global_semis or 0)
        else:
            tr_info=next((t for t in info.get('tracks',[]) if t['index']==ti), None)
            semis=(0 - tr_info['root_pc'])%12 if (tr_info and tr_info.get('root_pc') is not None) else (global_semis or 0)
        if drums: semis=0
        semis=(semis + tr_semitones)%12
        mv=max_vel_per_track.get(ti,0)
        for t_abs,msg in abs_events:
            m=msg.copy()
            if out_ch is not None and hasattr(m,'channel'): m.channel=max(0,min(15,out_ch-1))
            if m.type in ('note_on','note_off'):
                if m.type=='note_on' and m.velocity>0 and not drums: m.note=(m.note+semis)%128
                elif m.type=='note_off' and not drums: m.note=(m.note+semis)%128
                if vel_target is not None and m.type=='note_on' and m.velocity>0 and mv>0 and not drums:
                    scaled=int(round(m.velocity*(vel_target/mv))); m.velocity=max(1,min(127,scaled))
            if limit_ticks is not None and t_abs>int(limit_ticks*(target_ppq/ppq)): continue
            processed.append((t_abs,m))
            if m.type=='note_on' and getattr(m,'velocity',0)>0: active[(getattr(m,'channel',0),m.note)]=True
            elif m.type in ('note_off','note_on') and (getattr(m,'velocity',0)==0 or m.type=='note_off'): active.pop((getattr(m,'channel',0),m.note),None)
        if limit_ticks is not None:
            t_limit=int(limit_ticks*(target_ppq/ppq))
            for (ch,note) in list(active.keys()): processed.append((t_limit, mido.Message('note_off', note=note, velocity=0, channel=ch, time=0)))
        out.tracks.append(rebuild_track_from_abs(processed))
    out.save(dst)

@app.route('/api/copy', methods=['POST'])
def api_copy():
    data=request.get_json(silent=True) or {}
    files=data.get('files',[]); force480=bool(data.get('force480',True))
    normalization_mode=data.get('normalization_mode','global')
    max_bars=data.get('max_bars',None); track_map=data.get('track_map',None); track_map_rel=data.get('track_map_rel',None)
    try: max_bars=float(max_bars) if max_bars not in (None,'') else None
    except: max_bars=None
    dest_dir=ROOT/'selected'; dest_dir.mkdir(parents=True, exist_ok=True)
    copied=[]; errors=[]
    for rel in files:
        try:
            src=(ROOT/rel).resolve()
            if not str(src).startswith(str(ROOT.resolve())): raise RuntimeError("Outside root")
            if not src.exists() or src.suffix.lower()!='.mid': raise RuntimeError("not a .mid or missing")
            suffix=[]; 
            if normalization_mode!='off': suffix.append('C')
            if max_bars and max_bars>0: suffix.append(f"max{int(max_bars)}bar" if float(max_bars).is_integer() else f"max{max_bars}bar")
            dstname=src.stem + (' - ' + ' '.join(suffix) if suffix else '') + src.suffix
            dst=dest_dir/dstname
            per_track=None
            if track_map and track_map_rel and Path(rel).resolve()==(ROOT/track_map_rel).resolve():
                per_track={ int(t['index']): {
                    'out_ch': (int(t['out_ch']) if str(t.get('out_ch','')).isdigit() else None),
                    'transpose': int(t.get('transpose',0) or 0),
                    'vel_target': (int(t['vel_target']) if str(t.get('vel_target','')).isdigit() else None),
                    'drums': bool(t.get('drums', False))
                } for t in track_map }
            write_processed(src, dst, normalization_mode=normalization_mode, per_track=per_track, force_ppq=480 if force480 else None, max_bars=max_bars)
            copied.append(str(dst.relative_to(ROOT)))
        except Exception as e: errors.append({"file": rel, "error": str(e)})
    return jsonify({"copied":copied, "errors":errors, "dest":str(dest_dir.relative_to(ROOT)), "force480":force480, "normalization_mode":normalization_mode})

@app.route('/api/export_zip', methods=['POST'])
def api_export_zip():
    data=request.get_json(silent=True) or {}
    files=data.get('files',[]); force480=bool(data.get('force480',True))
    normalization_mode=data.get('normalization_mode','global'); max_bars=data.get('max_bars',None)
    track_map=data.get('track_map',None); track_map_rel=data.get('track_map_rel',None)
    try: max_bars=float(max_bars) if max_bars not in (None,'') else None
    except: max_bars=None
    mem=io.BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            try:
                src=(ROOT/rel).resolve()
                if not str(src).startswith(str(ROOT.resolve())): raise RuntimeError("Outside root")
                if not src.exists() or src.suffix.lower()!='.mid': raise RuntimeError("not a .mid or missing")
                suffix=[]; 
                if normalization_mode!='off': suffix.append('C')
                if max_bars and max_bars>0: suffix.append(f"max{int(max_bars)}bar" if float(max_bars).is_integer() else f"max{max_bars}bar")
                dstname=src.stem + (' - ' + ' '.join(suffix) if suffix else '') + src.suffix
                per_track=None
                if track_map and track_map_rel and Path(rel).resolve()==(ROOT/track_map_rel).resolve():
                    per_track={ int(t['index']): {
                        'out_ch': (int(t['out_ch']) if str(t.get('out_ch','')).isdigit() else None),
                        'transpose': int(t.get('transpose',0) or 0),
                        'vel_target': (int(t['vel_target']) if str(t.get('vel_target','')).isdigit() else None),
                        'drums': bool(t.get('drums', False))
                    } for t in track_map }
                tmp=ROOT/('._tmp_'+dstname)
                write_processed(src, tmp, normalization_mode=normalization_mode, per_track=per_track, force_ppq=480 if force480 else None, max_bars=max_bars)
                with open(tmp,'rb') as fh: zf.writestr(dstname, fh.read())
                try: tmp.unlink()
                except: pass
            except Exception as e:
                zf.writestr(f"ERROR_{Path(rel).name}.txt", str(e))
    mem.seek(0)
    return send_file(mem, mimetype='application/zip', as_attachment=True, download_name='exported_clips.zip')

def build_multitrack(files: List[Path], normalization_mode:str, force_ppq:int=480, max_bars:Optional[float]=None) -> Path:
    out=mido.MidiFile(type=1, ticks_per_beat=force_ppq)
    for src in files:
        info=analyze_midi(src)
        if normalization_mode=='off': semis=0
        elif normalization_mode=='global': semis=(info.get('transpose_to_C_same_mode',0) or 0)
        else: semis=(info.get('transpose_to_C_same_mode',0) or 0)
        mid=mido.MidiFile(src); numer,denom=4,4
        for tr in mid.tracks:
            for msg in tr:
                if msg.type=='time_signature': numer,denom=msg.numerator,msg.denominator; break
            else: continue
            break
        ppq=mid.ticks_per_beat
        limit_ticks=int(max_bars*ticks_per_bar_ppq(ppq,numer,denom)) if (max_bars and max_bars>0) else None
        factor=(force_ppq/ppq) if force_ppq!=ppq else 1.0
        merged=[]; active={}
        for tr in mid.tracks:
            for t_abs,msg in rescaled_abs_events(tr,factor):
                m=msg.copy()
                if m.type in ('note_on','note_off'): m.note=(m.note+semis)%128
                if limit_ticks is not None and t_abs>int(limit_ticks*(force_ppq/ppq)): continue
                merged.append((t_abs,m))
                if m.type=='note_on' and getattr(m,'velocity',0)>0: active[(getattr(m,'channel',0),m.note)]=True
                elif m.type in ('note_off','note_on') and (m.type=='note_off' or getattr(m,'velocity',0)==0): active.pop((getattr(m,'channel',0),m.note),None)
        if limit_ticks is not None:
            t_limit=int(limit_ticks*(force_ppq/ppq))
            for (ch,note) in list(active.keys()): merged.append((t_limit,mido.Message('note_off',note=note,velocity=0,channel=ch,time=0)))
        tr_out=rebuild_track_from_abs(merged); tr_out.insert(0, mido.MetaMessage('track_name', name=src.stem, time=0)); out.tracks.append(tr_out)
    dest=ROOT/'selected'; dest.mkdir(parents=True, exist_ok=True); dst=dest/("Combined_%d_tracks.mid"%len(files)); out.save(dst); return dst

@app.route('/api/build_multitrack', methods=['POST'])
def api_build_multitrack():
    data=request.get_json(silent=True) or {}
    files=data.get('files',[])[:16]; normalization_mode=data.get('normalization_mode','off')
    max_bars=data.get('max_bars',None)
    try: max_bars=float(max_bars) if max_bars not in (None,'') else None
    except: max_bars=None
    try:
        paths=[(ROOT/rel).resolve() for rel in files]
        for p in paths:
            if not str(p).startswith(str(ROOT.resolve())): return jsonify({"error":"Outside root"}),400
            if not p.exists(): return jsonify({"error":f"Missing: {p.name}"}),404
        dst=build_multitrack(paths, normalization_mode, force_ppq=480, max_bars=max_bars)
        return jsonify({"built": str(dst.relative_to(ROOT)), "dest":"selected"})
    except Exception as e:
        return jsonify({"error":str(e)}),500

INDEX_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>WebMIDI Clip Manager</title>
<style>
:root { --bg:#0b0d10; --fg:#e6edf3; --muted:#a9b1ba; --card:#12161a; --accent:#6bb3ff; --grid:#1a2530; }
html,body{height:100%} body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
.grid{display:grid;height:98vh;grid-template-columns:1fr 1fr;grid-template-rows:0.42fr 0.58fr;grid-template-areas:"controls details" "list roll";gap:10px;padding:10px 12px}
.panel{background:var(--card);border:1px solid #182028;border-radius:12px;overflow:hidden;display:flex;flex-direction:column;min-height:0}
.panel header{padding:8px 10px;background:#0f141a;border-bottom:1px solid #182028}
.panel header.hdrflex{display:flex;align-items:center;justify-content:space-between;gap:8px}
.panel header input[type="text"]{background:#0d131a;color:#e6edf3;border:1px solid #202833;border-radius:8px;padding:6px 8px;font-size:12px;min-width:180px}
.panel header h2{margin:0;font-size:13px;color:#cdd6df}
.body{padding:8px;display:flex;gap:8px;align-items:flex-start;overflow:auto}
.col{display:flex;flex-direction:column;gap:8px}
.tool{background:#0f151b;padding:8px 10px;border-radius:10px;border:1px solid #1a222c}
.tool label{font-size:11px;color:#a9b1ba;display:block;margin-bottom:4px}
.tool select,.tool .opts,.tool input{font-size:13px}
.btn{background:#1a73e8;color:#fff;border:none;border-radius:8px;padding:8px 10px;cursor:pointer;font-weight:600}
.btn.secondary{background:#1f2937;color:#e6edf3;border:1px solid #2b3542}
.btn.destructive{background:#d14343}
.floating{position:fixed;bottom:12px;right:12px;z-index:1000;box-shadow:0 2px 10px rgba(0,0,0,.4)}
.status{font-size:12px;color:#a9b1ba}
input[type="text"],input[type="number"]{background:#0d131a;color:#e6edf3;border:1px solid #202833;border-radius:8px;padding:6px 8px}
#fileList{overflow:auto;display:flex;flex-direction:column}
.fileHeader{display:grid;grid-template-columns:24px 60px 52px 52px 64px 52px 68px 80px 36px 56px 56px 1fr;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid #151b21;background:#0f1419;position:relative;z-index:1}
.row{display:grid;grid-template-columns:24px 60px 52px 52px 64px 52px 68px 80px 36px 56px 56px 1fr;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid #151b21}
.row:hover{background:#0f1419}
.fname{cursor:pointer;font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{font-size:10px;padding:2px 6px;border-radius:12px;background:#0e1620;border:1px solid #1c2228;color:#cfd6dd;margin-left:4px;white-space:nowrap}
.pill.warn{border-color:#e55353;color:#ff9a9a}
.controls button{margin-left:6px;background:#1f2937;color:#e6edf3;border:1px solid #2b3542;border-radius:6px;padding:4px 8px;cursor:pointer}
.controls button:hover{border-color:#3c4858}
.playing{color:#7ee787}
#details{display:block;width:100%}
.kv{display:grid;grid-template-columns:180px 1fr;gap:6px 12px;align-items:baseline;margin:6px 0}
.kv .k{color:#a9b1ba;font-size:12px}.kv .v{font-size:13px;font-weight:600}
.legend{margin-top:6px;border-top:1px solid #1b2330;padding-top:6px}
.legendHead{font-size:12px;color:#a9b1ba;margin:6px 0 6px}
/* 10 columns: sw | Track | Out Ch | Transpose | Vel Max | Drums | Chord | Timbre | Mute | Solo */
.mapHead{display:grid;grid-template-columns:16px 1.5fr 70px 80px 80px 70px 70px 80px 60px 60px;gap:8px;padding:4px 0;color:#8ea0b3;font-size:11px}
.mapRow{display:grid;grid-template-columns:16px 1.5fr 70px 80px 80px 70px 70px 80px 60px 60px;gap:8px;align-items:center;padding:4px 0}
.sw{width:14px;height:14px;border-radius:3px}
.btnxs{font-size:11px;padding:2px 6px;border-radius:6px;background:#1f2937;color:#cfe3ff;border:1px solid #2b3542;cursor:pointer}
.btnxs.on{background:#2b3a55}
.btnstar{font-size:12px;padding:2px 6px;border-radius:6px;background:#253041;color:#ffd166;border:1px solid #2b3542;cursor:pointer}
.btnstar.on{background:#3a475c;color:#ffe08a}
#rollWrap{overflow:hidden;height:100%;background:#0b1016}
#rollSvg{height:100%;width:100%}
.gridline{stroke:#1a2530;stroke-width:1}
.barline{stroke:#233141;stroke-width:1.2}
.loopline{stroke:#ff5555;stroke-width:1.5}
.note{stroke:rgba(0,0,0,.4);stroke-width:.5}
.note.active{stroke:#ffffff;stroke-width:1}
.marker{fill:#d1e4ff}
.playhead{stroke:#88c0ff;stroke-width:1.5}
.gutter{fill:#0c1118}
.gutterText{fill:#9fb3c8;font-size:10px}
.chordText{fill:#e8f0ff;font-size:12px;font-weight:700}
</style>
<script src="https://cdn.jsdelivr.net/npm/@tonejs/midi@2.0.28/build/Midi.min.js"></script>
</head>
<body>
<button id="stopAllFloat" class="btn destructive floating" title="Panic / All Notes Off">Stop All</button>
<div class="grid">
  <section class="panel" style="grid-area:controls;">
    <header><h2>Controls</h2></header>
    <div class="body" style="gap:16px;">
      <div class="col" style="flex:1 1 50%; min-width:320px;">
        <div class="tool"><label>Output</label><select id="midiOut"></select></div>
        <div class="tool"><label>MIDI Channel (single-track)</label><select id="chanSelect"></select><div class="status" id="mtHint" style="margin-top:4px; display:none;">Multitrack: respecting per-track mapping</div></div>
        <div class="tool"><label>Tempo (BPM)</label><input type="number" id="tempoBox" min="20" max="300" step="1" value="120" style="width:90px"></div>
        <div class="tool">
          <label>Export selected</label>
          <div class="opts" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <label><input type="checkbox" id="applyPreview" checked> Normalize using preview settings</label>
            <label><input type="checkbox" id="force480" checked> Force 480 PPQN (Yamaha)</label>
            <button id="copySelected">Copy → <b>/selected</b></button>
            <button id="zipSelected">Download ZIP</button>
            <button id="buildMulti">Build Multitrack</button><span id="multiCount" class="status" style="margin-left:6px;">(0)</span>
          </div>
        </div>
        <div class="status" id="status">Loading…</div>
      </div>
      <div class="col" style="flex:1 1 40%; min-width:260px;">
        <div class="tool">
          <label>Preview settings</label>
          <div class="opts" style="display:flex;flex-direction:column;gap:6px;">
            <div>
              <div style="font-size:11px;color:#a9b1ba;margin-bottom:4px;">Normalization</div>
              <label><input type="radio" name="normMode" value="off"> Off</label>
              <label style="margin-left:10px;"><input type="radio" name="normMode" value="global" checked> Global → C</label>
              <label style="margin-left:10px;"><input type="radio" name="normMode" value="per"> Per-track → C</label>
            </div>
            <label><input type="checkbox" id="roundP2" checked> Round loop to power-of-two bars</label>
            <label>Max bars: <input type="number" id="maxBars" min="1" step="0.5" value="" placeholder="(no limit)" style="width:90px"></label>
            <div><label><input type="checkbox" id="velScaleToggle" checked> Velocity scaling</label>
              <label>Target loudest: <input type="number" id="velTarget" min="1" max="127" value="100" style="width:70px"></label></div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="panel" style="grid-area:details;">
    <header><h2>Analysis</h2></header>
    <div class="body" id="details" style="min-height:110px; display:block; width:100%;"></div>
  </section>

  <section class="panel" style="grid-area:list;">
    <header class="hdrflex"><h2>Files</h2><input type="text" id="filterBox" placeholder="Filter…"></header>
    <div id="fileHeader" class="fileHeader">
      <div></div><div>Tracks</div><div>Tsig</div><div>BPM</div><div>Notes</div><div>Uniq</div><div>Type</div><div>Scale</div><div></div><div></div><div></div><div>File</div>
    </div>
    <div class="body" id="fileList"></div>
  </section>

  <section class="panel" style="grid-area:roll;">
    <header><h2>Piano Roll</h2></header>
    <div id="rollWrap"><svg id="rollSvg" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet"></svg></div>
  </section>
</div>

<script>
let midiAccess=null, currentOut=null, useSynth=false, ac=null, master=null;
let currentChannel=1, currentTempo=120;
let playing=null, playheadTimer=null;
let fileData=[], currentRenderNotes=[], currentRectMap=new Map();
let velScaleEnabled=true, velTarget=100, roundP2=true, normMode='global';
let lastChordSegments=[];
let trackState=null; // {rel,count,outCh[],transpose[],velTarget[],drums[],chordSrc[],timbre[],mute[],solo[]}
let rollMetrics=null; // {gutterW, pxPerBar, barSec, chordH, W, H}
let notesGroup=null, chordGroup=null;
let multiSel=new Set();

const TRACK_COLORS=['#7ee787','#9cdcfe','#f28b82','#fbbc04','#34d399','#a78bfa','#f472b6','#60a5fa','#ffa8a8','#ffd166','#06d6a0','#bdb2ff','#ffadad','#90caf9','#b2f7ef','#f4a261'];

(function buildChanSelect(){
  const s=document.getElementById('chanSelect'); for(let i=1;i<=16;i++){ const o=document.createElement('option'); o.value=String(i); o.text=String(i); s.appendChild(o); }
  s.value='1'; s.addEventListener('change', e=> currentChannel=parseInt(e.target.value));
})();
document.getElementById('tempoBox').addEventListener('change', e=>{ const v=parseFloat(e.target.value); currentTempo=isNaN(v)?120:Math.max(20,Math.min(300,v)); e.target.value=currentTempo; if(playing) restartCurrent(); });
document.querySelectorAll('input[name="normMode"]').forEach(r=> r.addEventListener('change', e=>{ normMode=e.target.value; if(playing) restartCurrent(); }));
document.getElementById('velScaleToggle').addEventListener('change', e=>{ velScaleEnabled=!!e.target.checked; if(playing) restartCurrent(); });
document.getElementById('velTarget').addEventListener('change', e=>{ const v=parseInt(e.target.value); velTarget=Math.max(1,Math.min(127,isNaN(v)?100:v)); e.target.value=velTarget; if(playing) restartCurrent(); });
document.getElementById('roundP2').addEventListener('change', e=>{ roundP2=!!e.target.checked; if(playing) restartCurrent(); });
document.getElementById('maxBars').addEventListener('change', e=>{ if(playing) restartCurrent(); });

function restartCurrent(){
  if(!playing) return;
  const row=document.getElementById('row-'+cssEscape(playing.id));
  if(row){ const rel=decodeURIComponent(row.querySelector('.play').dataset.rel); playLoop(rel, playing.id); }
}

// Built-in synth
function ensureAC(){ if(!ac){ const Ctx=window.AudioContext||window.webkitAudioContext; ac=new Ctx(); master=ac.createGain(); master.gain.value=.8; master.connect(ac.destination);} }
function hzFromMidi(n){ return 440*Math.pow(2,(n-69)/12); }
function timbreForTrack(ti){ return ['sawtooth','square','triangle','sine'][ti%4]; }
let activeVoices=new Map();
function synthNoteOn(note, vel=100, ti=0){ ensureAC(); const t=ac.currentTime; const osc=ac.createOscillator(), g=ac.createGain(); osc.type=timbreForTrack(ti); osc.frequency.setValueAtTime(hzFromMidi(note),t); const v=Math.max(0.03, Math.pow((vel/127),1.3)*0.35); g.gain.setValueAtTime(0,t); g.gain.linearRampToValueAtTime(v,t+0.01); osc.connect(g).connect(master); osc.start(t); activeVoices.set(note+(ti*1000),{osc:osc,gain:g}); }
function synthNoteOff(note, ti=0){ if(!ac) return; const v=activeVoices.get(note+(ti*1000)); if(!v) return; const t=ac.currentTime; v.gain.gain.cancelScheduledValues(t); v.gain.gain.setTargetAtTime(0.0001,t,0.03); v.osc.stop(t+0.1); activeVoices.delete(note+(ti*1000)); }
function synthAllNotesOff(){ if(!ac) return; for(const k of Array.from(activeVoices.keys())){ const ti=Math.floor(k/1000); const note=k-(ti*1000); synthNoteOff(note,ti); } }

// WebMIDI
async function initMIDI(){ try{ midiAccess=await navigator.requestMIDIAccess({sysex:false}); }catch(e){ midiAccess=null; } refreshOutputs(); if(midiAccess){ midiAccess.onstatechange=refreshOutputs; } }
function refreshOutputs(){
  const sel=document.getElementById('midiOut'); const was=sel.value; sel.innerHTML='';
  const optSynth=document.createElement('option'); optSynth.value='builtin'; optSynth.text='Built-in Synth'; sel.appendChild(optSynth);
  if(midiAccess){ [...midiAccess.outputs.values()].forEach(o=>{ const opt=document.createElement('option'); opt.value=o.id; opt.text=o.name; sel.appendChild(opt); }); }
  const pickVal=(was && [...sel.options].some(o=>o.value===was))?was:'builtin'; sel.value=pickVal; setOutput(pickVal);
}
function setOutput(id){ useSynth=(id==='builtin'); if(!useSynth && midiAccess){ currentOut=[...midiAccess.outputs.values()].find(o=>o.id===id)||null; } else { currentOut=null; } }
document.getElementById('midiOut').addEventListener('change', e=> setOutput(e.target.value));

// MIDI send
function noteOn(note, vel=100, ch=1, ti=0){ if(useSynth){ synthNoteOn(note,vel,ti); return; } if(!currentOut) return; const c=(ch-1)&0x0F; currentOut.send([0x90|c, note&0x7F, vel&0x7F]); }
function noteOff(note, ch=1, ti=0){ if(useSynth){ synthNoteOff(note,ti); return; } if(!currentOut) return; const c=(ch-1)&0x0F; currentOut.send([0x80|c, note&0x7F, 0]); }

// Helpers
function cssEscape(s){ return s.replace(/[^a-zA-Z0-9_-]/g,'_'); }
function labelMode(m){ const map={ionian:'Maj',aeolian:'min',lydian:'Lyd',mixolydian:'Mix',dorian:'Dor',phrygian:'Phr'}; return map[m]||m; }
function barDurationSeconds(ts,bpm){ const [num,den]=ts.split('/').map(x=>parseInt(x,10)); const q=60/bpm; return num*q*(4/den); }
function beatDurationSeconds(ts,bpm){ return 60/bpm; }
function nextP2(bars){ let p=1; while(p<bars) p<<=1; return p; }
function noteName(n){ const nn=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']; const o=Math.floor(n/12)-1; return nn[n%12]+o; }
function rootName(pc){ const nn=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']; return nn[((pc%12)+12)%12]; }

// Chords
const CHORD_TEMPLATES=[
  {name:'maj7',ints:[0,4,7,11]},{name:'7',ints:[0,4,7,10]},{name:'m7',ints:[0,3,7,10]},{name:'mMaj7',ints:[0,3,7,11]},
  {name:'dim7',ints:[0,3,6,9]},{name:'m7b5',ints:[0,3,6,10]},{name:'aug7',ints:[0,4,8,10]},
  {name:'maj',ints:[0,4,7]},{name:'min',ints:[0,3,7]},{name:'dim',ints:[0,3,6]},{name:'aug',ints:[0,4,8]},{name:'sus2',ints:[0,2,7]},{name:'sus4',ints:[0,5,7]},
];
function detectChord(pcs){
  if(!pcs||pcs.size===0) return null; let best=null;
  for(let r of pcs){
    const trans=new Set([...pcs].map(p=>((p-r)%12+12)%12));
    for(const tpl of CHORD_TEMPLATES){
      const hit=tpl.ints.filter(i=>trans.has(i)).length; const extra=[...trans].filter(i=>!tpl.ints.includes(i)).length; const score=hit*10 - extra;
      if(hit>=3 || (hit>=2 && tpl.ints.length===3)){ if(!best || score>best.score || (score===best.score && tpl.ints.length>best.len)){ best={score,root:r,name:tpl.name,len:tpl.ints.length}; } }
    }
  }
  if(!best){
    if(pcs.size>=3){ const arr=[...pcs]; for(let r of arr){ const tr=new Set(arr.map(p=>((p-r)%12+12)%12)); if(tr.has(4)&&tr.has(7)) return rootName(r); if(tr.has(3)&&tr.has(7)) return rootName(r)+'m'; } }
    return null;
  }
  const root=rootName(best.root); if(best.name==='maj') return root; if(best.name==='min') return root+'m'; return root+best.name;
}
function inferChords(renderNotes, ts, loopLen){
  const beatSec=beatDurationSeconds(ts,currentTempo); const steps=Math.max(1,Math.floor(loopLen/beatSec)); const eps=0.001; const segs=[]; let last=null, segStart=0;
  for(let i=0;i<=steps;i++){
    const t=Math.min(loopLen, i*beatSec+eps); const pcs=new Set();
    for(const n of renderNotes){ if(n.muted) continue; if(!trackState || !trackState.chordSrc[n.ti]) continue; if(n.t<=t && (n.t+n.d)>t) pcs.add(((n.p%12)+12)%12); }
    const label=detectChord(pcs);
    if(i===0){ last=label; segStart=0; continue; }
    if(label!==last || i===steps){ const t1=Math.min(loopLen, i*beatSec); if(last){ segs.push({t0:segStart, t1, label:last}); } segStart=t1; last=label; }
  }
  return segs;
}

// Track Map
function initTrackState(rel,count,tracksMeta){
  trackState={ rel, count, outCh:new Array(count).fill(null), transpose:new Array(count).fill(0),
               velTarget:new Array(count).fill(null), drums:new Array(count).fill(false),
               chordSrc:new Array(count).fill(true), timbre:new Array(count).fill(0),
               mute:new Array(count).fill(false), solo:new Array(count).fill(false) };
  tracksMeta.forEach((t,i)=>{
    if(t.channels && t.channels.length){ trackState.outCh[i]=t.channels[0]+1; if(trackState.outCh[i]<1||trackState.outCh[i]>16) trackState.outCh[i]=null; }
    if(t.uses_ch10){ trackState.drums[i]=true; trackState.chordSrc[i]=false; if(trackState.outCh[i]==null) trackState.outCh[i]=10; }
  });
}

function anySolo(){ return trackState && trackState.solo.some(Boolean); }
function isAudible(ti){
  if(!trackState) return true;
  if(anySolo()) return !!trackState.solo[ti];
  return !trackState.mute[ti];
}

// Render Track Map (aligned headers)
function renderTrackMap(meta){
  const det=document.getElementById('details'); const old=det.querySelector('.legendTbl'); if(old) old.remove();
  const wrapper=document.createElement('div'); wrapper.className='legend legendTbl';
  const head=document.createElement('div'); head.className='legendHead'; head.textContent='Track Map (per-track preview & export):'; wrapper.appendChild(head);

  const hdr=document.createElement('div'); hdr.className='mapHead';
  hdr.innerHTML='<div></div><div>Track</div><div>Out Ch</div><div>Transpose</div><div>Vel Max</div><div>Drums</div><div>Chord</div><div>Timbre</div><div>Mute</div><div>Solo</div>';
  wrapper.appendChild(hdr);

  meta.tracks.forEach((tr,i)=>{
    const row=document.createElement('div'); row.className='mapRow';
    const sw=document.createElement('div'); sw.className='sw'; sw.style.background=TRACK_COLORS[i%TRACK_COLORS.length];
    const name=document.createElement('div'); name.textContent=`${tr.name} ${tr.channels && tr.channels.length ? `(ch ${tr.channels.join(',')})` : ''}`;

    const outSel=document.createElement('select'); const opt0=document.createElement('option'); opt0.value=''; opt0.text='(orig)'; outSel.appendChild(opt0);
    for(let c=1;c<=16;c++){ const o=document.createElement('option'); o.value=String(c); o.text=String(c); outSel.appendChild(o); }
    if(trackState.outCh[i]) outSel.value=String(trackState.outCh[i]);
    outSel.addEventListener('change', e=>{ trackState.outCh[i]=e.target.value?parseInt(e.target.value):null; if(playing) restartCurrent(); });

    const trIn=document.createElement('input'); trIn.type='number'; trIn.min='-24'; trIn.max='24'; trIn.step='1'; trIn.value=trackState.transpose[i];
    trIn.addEventListener('change', e=>{ const v=parseInt(e.target.value)||0; trackState.transpose[i]=Math.max(-24,Math.min(24,v)); e.target.value=trackState.transpose[i]; if(playing) restartCurrent(); });

    const velIn=document.createElement('input'); velIn.type='number'; velIn.min='1'; velIn.max='127'; velIn.placeholder='(—)'; velIn.value=trackState.velTarget[i]??'';
    velIn.addEventListener('change', e=>{ const v=parseInt(e.target.value); trackState.velTarget[i]=isNaN(v)?null:Math.max(1,Math.min(127,v)); if(playing) restartCurrent(); });

    const dChk=document.createElement('input'); dChk.type='checkbox'; dChk.checked=!!trackState.drums[i];
    dChk.addEventListener('change', e=>{ trackState.drums[i]=!!e.target.checked; if(trackState.drums[i]) trackState.chordSrc[i]=false; if(playing) restartCurrent(); });

    const cChk=document.createElement('input'); cChk.type='checkbox'; cChk.checked=!!trackState.chordSrc[i];
    cChk.addEventListener('change', e=>{ trackState.chordSrc[i]=!!e.target.checked; if(playing) restartCurrent(); });

    const timSel=document.createElement('select'); ['saw','square','triangle','sine'].forEach((nm,ix)=>{ const o=document.createElement('option'); o.value=String(ix); o.text=nm; timSel.appendChild(o); });
    timSel.value=String(trackState.timbre[i]||0); timSel.addEventListener('change', e=>{ trackState.timbre[i]=parseInt(e.target.value)||0; if(playing) restartCurrent(); });

    const mbtn=document.createElement('button'); mbtn.className='btnxs'+(trackState.mute[i]?' on':''); mbtn.textContent='Mute';
    mbtn.addEventListener('click', ()=>{ trackState.mute[i]=!trackState.mute[i]; mbtn.classList.toggle('on'); if(playing) restartCurrent(); });

    const sbtn=document.createElement('button'); sbtn.className='btnxs'+(trackState.solo[i]?' on':''); sbtn.textContent='Solo';
    sbtn.addEventListener('click', ()=>{ trackState.solo[i]=!trackState.solo[i]; sbtn.classList.toggle('on'); if(playing) restartCurrent(); });

    row.appendChild(sw); row.appendChild(name); row.appendChild(outSel); row.appendChild(trIn); row.appendChild(velIn);
    const dLbl=document.createElement('label'); dLbl.appendChild(dChk); row.appendChild(dLbl);
    const cLbl=document.createElement('label'); cLbl.appendChild(cChk); row.appendChild(cLbl);
    row.appendChild(timSel); row.appendChild(mbtn); row.appendChild(sbtn);
    wrapper.appendChild(row);
  });
  det.appendChild(wrapper);
}

// SVG Piano Roll with scrolling grid for >4 bars
function drawRollSVG(renderNotes, loopLenSec, barSec, chordSegs){
  const svg=document.getElementById('rollSvg');
  while(svg.firstChild) svg.removeChild(svg.firstChild);
  if(!renderNotes.length){ svg.setAttribute('viewBox','0 0 100 60'); return; }
  let minPitch=Math.min(...renderNotes.map(n=>n.p)), maxPitch=Math.max(...renderNotes.map(n=>n.p));
  const prange=Math.max(12, maxPitch-minPitch+1);
  let rowH=8; if(prange<=12) rowH=16; else if(prange<=24) rowH=12; else if(prange<=36) rowH=10; else rowH=8;
  const pxPerBar=220; const gutterW=(rowH>=12)?60:36; const chordH=18;
  const W=Math.ceil(4*pxPerBar + gutterW + 20); const H=prange*rowH + 24 + chordH;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.style.width='100%'; svg.style.height='100%';
  // background & gutter
  const bg=docRect(0,0,W,H,'#0a0f14'); svg.appendChild(bg);
  const gut=docRect(0,chordH,gutterW,H-chordH); gut.setAttribute('class','gutter'); svg.appendChild(gut);

  // scrolling container: chords, grid, notes
  const scrollGroup=document.createElementNS('http://www.w3.org/2000/svg','g'); scrollGroup.setAttribute('id','scrollGroup'); svg.appendChild(scrollGroup);

  // chord annotations (scrolling)
  chordGroup=document.createElementNS('http://www.w3.org/2000/svg','g'); chordGroup.setAttribute('id','chordGroup'); scrollGroup.appendChild(chordGroup);
  if(chordSegs && chordSegs.length){
    for(const s of chordSegs){
      const x0=gutterW + (s.t0 / barSec) * pxPerBar; const x1=gutterW + (s.t1 / barSec) * pxPerBar; const xm=(x0+x1)/2;
      const t=docText(xm,12,s.label,'chordText'); chordGroup.appendChild(t);
    }
  }

  // grid lines + absolute bar labels (scrolling)
  const gridGroup=document.createElementNS('http://www.w3.org/2000/svg','g'); gridGroup.setAttribute('id','gridGroup'); scrollGroup.appendChild(gridGroup);
  const totalBars=Math.max(4, Math.ceil(loopLenSec / barSec)+1);
  for(let b=0;b<=totalBars;b++){
    const x=gutterW + b*pxPerBar;
    const gl=docLine(x+0.5,chordH,x+0.5,H, b%4===0?'barline':'gridline'); gridGroup.appendChild(gl);
    if(b>0){ const tt=docText(x+4,chordH+10,String(b),''); tt.setAttribute('fill','#7a8696'); tt.setAttribute('font-size','10'); gridGroup.appendChild(tt); }
  }

  // horizontal rows & note names (fixed)
  const showAllNames=(rowH>=12); const maxPitchAll=Math.max(...renderNotes.map(rn=>rn.p));
  for(let i=0;i<prange;i++){
    const midiN=minPitch+i; const pc=midiN%12; const y=chordH + (prange-1-i)*rowH + 18;
    if(pc===1||pc===3||pc===6||pc===8||pc===10){ const r=docRect(gutterW,y-rowH,W-gutterW,rowH,'rgba(255,255,255,0.02)'); svg.appendChild(r); }
    if(showAllNames || pc===0){ const text=docText(gutterW-6,y-2,noteName(midiN),'gutterText'); text.setAttribute('text-anchor','end'); svg.appendChild(text); }
  }

  // notes (scrolling)
  notesGroup=document.createElementNS('http://www.w3.org/2000/svg','g'); notesGroup.setAttribute('id','notesGroup'); scrollGroup.appendChild(notesGroup);
  function velColor(v){ const t=v/127; const r=Math.round(80+t*110), g=Math.round(120+t*80), b=Math.round(200-t*120); return `rgb(${r},${g},${b})`; }
  currentRectMap.clear();
  for(const n of renderNotes){
    const x=gutterW + (n.t / barSec) * pxPerBar; const widthSec=Math.min(n.d, Math.max(0, loopLenSec - n.t)); if(widthSec<=0) continue;
    const w=Math.max(2, Math.min((widthSec / barSec) * pxPerBar, pxPerBar*32));
    const y=chordH + (maxPitchAll - n.p) * (H - chordH - 24) / (maxPitchAll - minPitch + 1) + 18;
    const rect=docRect(x,y-6,w,6,velColor(n.v)); rect.setAttribute('class','note'); rect.setAttribute('opacity', (n.muted?'0.2':'1'));
    rect.setAttribute('stroke', n.color); rect.setAttribute('stroke-width','0.8'); notesGroup.appendChild(rect);
    currentRectMap.set(n.idx+'-'+n.ti, rect);
  }

  // viewport end marker (4 bars) & playhead
  const loopX=gutterW + (4 * pxPerBar); const ll=docLine(loopX+0.5,chordH,loopX+0.5,H,'loopline'); svg.appendChild(ll);
  const ph=docLine(gutterW+0.5, chordH, gutterW+0.5, H, 'playhead'); ph.setAttribute('id','playhead'); svg.appendChild(ph);

  rollMetrics={gutterW, pxPerBar, barSec, chordH, W, H};
}
function docRect(x,y,w,h,fill){ const r=document.createElementNS('http://www.w3.org/2000/svg','rect'); r.setAttribute('x',x); r.setAttribute('y',y); r.setAttribute('width',w); r.setAttribute('height',h); if(fill) r.setAttribute('fill',fill); return r; }
function docLine(x1,y1,x2,y2,cls){ const l=document.createElementNS('http://www.w3.org/2000/svg','line'); l.setAttribute('x1',x1); l.setAttribute('y1',y1); l.setAttribute('x2',x2); l.setAttribute('y2',y2); if(cls) l.setAttribute('class',cls); return l; }
function docText(x,y,txt,cls){ const t=document.createElementNS('http://www.w3.org/2000/svg','text'); t.setAttribute('x',x); t.setAttribute('y',y); if(cls) t.setAttribute('class',cls); t.textContent=txt; return t; }

// Playback
async function playLoop(relpath, rowId){
  stopAll();
  const midi=await Midi.fromUrl('/api/raw?file='+encodeURIComponent(relpath));
  const origBpm=(midi.header.tempos && midi.header.tempos.length)? midi.header.tempos[0].bpm : 120;
  const scale=origBpm/currentTempo;
  const meta=fileData.find(f=>f.relpath===relpath) || {};
  const isMultitrack=(meta.track_count||0)>1; document.getElementById('mtHint').style.display=isMultitrack?'block':'none';
  if(!trackState || trackState.rel!==relpath){ initTrackState(relpath, midi.tracks.length, meta.tracks||[]); }
  const ts=meta.time_signature||'4/4'; const barSec=barDurationSeconds(ts,currentTempo);
  const maxBarsInput=parseFloat(document.getElementById('maxBars').value); const limitSecs=(!isNaN(maxBarsInput)&&maxBarsInput>0)? maxBarsInput*barSec : Infinity;
  // Build notes
  const notes=[]; let maxVelSeenGlobal=1; const maxVelPerTrack=new Array(midi.tracks.length).fill(1);
  midi.tracks.forEach((tr,ti)=>{
    const color=TRACK_COLORS[ti%TRACK_COLORS.length];
    const outCh= trackState.outCh[ti] || (meta.tracks && meta.tracks[ti] && meta.tracks[ti].channels && meta.tracks[ti].channels.length ? (meta.tracks[ti].channels[0]+1) : currentChannel);
    let semis=0;
    if(normMode==='global'){ semis=Number.isFinite(meta.transpose_to_C_same_mode)? (meta.transpose_to_C_same_mode%12) : 0; }
    else if(normMode==='per'){ const tmeta=(meta.tracks||[])[ti]; if(tmeta && Number.isFinite(tmeta.root_pc)) semis=(0 - tmeta.root_pc)%12; else semis=Number.isFinite(meta.transpose_to_C_same_mode)? (meta.transpose_to_C_same_mode%12) : 0; }
    if(trackState.drums[ti]) semis=0; semis=(semis + (trackState.transpose[ti]||0))%12;
    tr.notes.forEach(n=>{
      let pitch=n.midi + semis; while(pitch<0) pitch+=12; while(pitch>127) pitch-=12;
      const v=Math.round((n.velocity||0.8)*127); if(v>maxVelSeenGlobal) maxVelSeenGlobal=v; if(v>maxVelPerTrack[ti]) maxVelPerTrack[ti]=v;
      const audible=isAudible(ti) && (outCh!=null);
      notes.push({ ti, color, ch: (outCh==null?1:outCh), t:n.time*scale, d:n.duration*scale, p:pitch, v, muted:!audible });
    });
  });
  notes.sort((a,b)=>a.t-b.t);
  const natural=midi.duration*scale; const baseLoop=Math.min(natural, limitSecs); const bars=baseLoop/barSec; const finalLoop=roundP2? Math.max(barSec, nextP2(Math.max(1,bars))*barSec) : baseLoop;
  const globalFactor=(velScaleEnabled && maxVelSeenGlobal>0)? (velTarget/maxVelSeenGlobal) : 1;
  const perTrackFactor=i=>{ const tgt=trackState.velTarget[i]; if(tgt && maxVelPerTrack[i]>0) return tgt/maxVelPerTrack[i]; return globalFactor; };
  currentRenderNotes=notes.map((n,idx)=>({...n, v:Math.max(1,Math.min(127,Math.round(n.v*perTrackFactor(n.ti)))), idx})).filter(n=>n.t<baseLoop);
  lastChordSegments=inferChords(currentRenderNotes, ts, baseLoop);
  renderTrackMap(meta);
  drawRollSVG(currentRenderNotes, finalLoop, barSec, lastChordSegments);

  const timers=[];
  currentRenderNotes.forEach(n=>{
    const onT=n.t, offT=Math.min(n.t+n.d, baseLoop);
    if(!n.muted){
      timers.push(setTimeout(()=>{ noteOn(n.p,n.v,n.ch,n.ti); const r=currentRectMap.get(n.idx+'-'+n.ti); if(r){ r.classList.add('active'); } }, Math.max(0,onT*1000)));
      timers.push(setTimeout(()=>{ noteOff(n.p,n.ch,n.ti); const r=currentRectMap.get(n.idx+'-'+n.ti); if(r){ r.classList.remove('active'); } }, Math.max(0,offT*1000)));
    }
  });
  timers.push(setTimeout(()=>{ if(playing && playing.id===rowId){ playLoop(relpath,rowId); } }, Math.max(0, finalLoop*1000)));

  const svg=document.getElementById('rollSvg'); const phEl=svg.querySelector('#playhead');
  const updatePH=()=>{
    if(!playing || !phEl || !rollMetrics) return;
    const {gutterW, pxPerBar, chordH, H, barSec:bs} = rollMetrics;
    const elapsed=(performance.now()-playing.startMs)/1000; const t=elapsed % finalLoop;
    const mid=gutterW + 2*pxPerBar; // mid at end of bar 2
    const barsInLoop = finalLoop / bs;

    let windowStart = 0;
    let phX = gutterW + (t/bs) * pxPerBar;

    if(barsInLoop > 4){
      if(t >= 2*bs){
        windowStart = t - 2*bs;
        phX = mid;
      }
    } else {
      windowStart = 0;
    }

    const tx = - (windowStart / bs) * pxPerBar;
    const scrollEl=document.getElementById('scrollGroup');
    if(scrollEl){ scrollEl.setAttribute('transform', `translate(${tx},0)`); }

    phEl.setAttribute('x1', phX+0.5); phEl.setAttribute('x2', phX+0.5); phEl.setAttribute('y1', chordH); phEl.setAttribute('y2', H);
  };
  const phIv=setInterval(updatePH, 33);

  playing={id:rowId, timers, loopLen:finalLoop, startMs:performance.now()};
  playheadTimer=phIv;
  document.querySelectorAll('.row .fname').forEach(el=>el.classList.remove('playing'));
  const nameEl=document.querySelector(`#row-${cssEscape(rowId)} .fname`); if(nameEl) nameEl.classList.add('playing');
}

function stopAll(){
  if(playing){ playing.timers.forEach(clearTimeout); playing=null; }
  if(playheadTimer){ clearInterval(playheadTimer); playheadTimer=null; }
  if(useSynth){ synthAllNotesOff(); } else if(currentOut){ for(let ch=0; ch<16; ch++){ currentOut.send([0xB0|ch,123,0]); currentOut.send([0xB0|ch,120,0]); } }
  document.querySelectorAll('.row .fname').forEach(el=>el.classList.remove('playing'));
  currentRectMap.forEach(rect=>rect.classList.remove('active'));
}
document.getElementById('stopAllFloat').addEventListener('click', stopAll);
window.addEventListener('beforeunload', stopAll);

// Files
async function loadFiles(){ const res=await fetch('/api/files'); const data=await res.json(); fileData=data.files; renderList(fileData); document.getElementById('status').textContent=`${data.count} file(s) in ${data.root}`; }
function renderList(files){
  const box=document.getElementById('fileList'); box.innerHTML='';
  files.forEach(f=>{
    const rowId=f.relpath; const div=document.createElement('div'); div.className='row'; div.id='row-'+cssEscape(rowId);
    const typeMap={rhythmic_single_note:'Rhy', monophonic_melodic:'Mono', polyphonic_chordal:'Poly'};
    const typeTxt=typeMap[f.classification]||'';
    const keytxt=(f.root && f.mode)? (f.root+' '+labelMode(f.mode)) : '—';
    const bpmTxt=(f.tempo_bpm? String(Math.round(f.tempo_bpm)) : '—');
    const notesTxt=String(f.note_count||0);
    const uniqTxt=String(f.unique_pitches||0);
    const tsigTxt=(f.time_signature || '—');
    const trkTxt=(f.track_count && f.track_count>1)? String(f.track_count) : (f.track_count? String(f.track_count): '1');
    div.innerHTML=`
      <div><input type="checkbox" class="pick" data-rel="${encodeURIComponent(f.relpath)}" title="Select for copy/zip"></div>
      <div>${trkTxt}</div>
      <div>${tsigTxt}</div>
      <div>${bpmTxt}</div>
      <div>${notesTxt}</div>
      <div>${uniqTxt}</div>
      <div>${typeTxt}</div>
      <div>${keytxt}</div>
      <div class="controls"><button class="btnstar star" data-rel="${encodeURIComponent(f.relpath)}" title="Toggle for multitrack">★</button></div>
      <div class="controls"><button class="play" data-rel="${encodeURIComponent(f.relpath)}">Play</button></div>
      <div class="controls"><button class="stop" data-rel="${encodeURIComponent(f.relpath)}">Stop</button></div>
      <div class="fname" title="Click to play">${f.filename}</div>`;
    box.appendChild(div);
  });
  box.querySelectorAll('button.play').forEach(b=> b.addEventListener('click', e=>{ const rel=decodeURIComponent(e.currentTarget.dataset.rel); const id=rel; showDetails(rel); playLoop(rel,id); }));
  box.querySelectorAll('button.stop').forEach(b=> b.addEventListener('click', stopAll));
  box.querySelectorAll('.row .fname').forEach(n=> n.addEventListener('click', e=>{ const row=e.currentTarget.closest('.row'); const rel=decodeURIComponent(row.querySelector('.play').dataset.rel); const id=rel; showDetails(rel); playLoop(rel,id); }));
  box.querySelectorAll('button.star').forEach(b=> b.addEventListener('click', e=>{
    const rel=decodeURIComponent(e.currentTarget.dataset.rel);
    const btn=e.currentTarget; const key=rel;
    if(multiSel.has(key)){ multiSel.delete(key); btn.classList.remove('on'); }
    else { multiSel.add(key); btn.classList.add('on'); }
    const mc=document.getElementById('multiCount'); if(mc) mc.textContent = `(${multiSel.size})`;
  }));
}

// Export helpers
function getNormalizationMode(){ const r=document.querySelector('input[name="normMode"]:checked'); return r? r.value : 'off'; }
function getCurrentTrackMap(){
  if(!trackState) return null; const arr=[];
  for(let i=0;i<trackState.count;i++){ arr.push({ index:i, out_ch:trackState.outCh[i], transpose:trackState.transpose[i], vel_target:trackState.velTarget[i], drums:!!trackState.drums[i], chord:!!trackState.chordSrc[i], timbre:trackState.timbre[i]||0 }); }
  return arr;
}

// Copy selected / ZIP / Build
document.getElementById('copySelected').addEventListener('click', async ()=>{
  const picks=[...document.querySelectorAll('.pick:checked')].map(cb=>decodeURIComponent(cb.dataset.rel)); if(!picks.length){ alert('No files selected'); return; }
  const res=await fetch('/api/copy',{method:'POST',headers:{'Content-Type':'application/json'}, body:JSON.stringify({ files:picks, force480:document.getElementById('force480').checked, normalization_mode: document.getElementById('applyPreview').checked? getNormalizationMode():'off', max_bars: parseFloat(document.getElementById('maxBars').value), track_map:getCurrentTrackMap(), track_map_rel:(trackState?trackState.rel:null) })});
  const data=await res.json(); if(!res.ok){ alert('Copy failed'); return; }
  alert(`Copied ${data.copied.length} file(s) to /${data.dest}${data.force480?' (480 PPQN)':''}${data.errors.length? '\nErrors: '+JSON.stringify(data.errors):''}`);
});
document.getElementById('zipSelected').addEventListener('click', async ()=>{
  const picks=[...document.querySelectorAll('.pick:checked')].map(cb=>decodeURIComponent(cb.dataset.rel)); if(!picks.length){ alert('No files selected'); return; }
  const res=await fetch('/api/export_zip',{method:'POST',headers:{'Content-Type':'application/json'}, body:JSON.stringify({ files:picks, force480:document.getElementById('force480').checked, normalization_mode: document.getElementById('applyPreview').checked? getNormalizationMode():'off', max_bars: parseFloat(document.getElementById('maxBars').value), track_map:getCurrentTrackMap(), track_map_rel:(trackState?trackState.rel:null) })});
  if(!res.ok){ alert('ZIP export failed'); return; }
  const blob=await res.blob(); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='exported_clips.zip'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
});
document.getElementById('buildMulti').addEventListener('click', async ()=>{
  const picks= (multiSel.size>0) ? Array.from(multiSel) : [...document.querySelectorAll('.pick:checked')].map(cb=>decodeURIComponent(cb.dataset.rel));
  if(picks.length<2){ alert('Select 2–16 items (starred or checked) for multitrack'); return; }
  const res=await fetch('/api/build_multitrack',{method:'POST',headers:{'Content-Type':'application/json'}, body:JSON.stringify({ files:picks, normalization_mode:getNormalizationMode(), max_bars: parseFloat(document.getElementById('maxBars').value) })});
  const data=await res.json(); if(data.error){ alert('Error: '+data.error); return; } alert(`Built ${data.built} in /${data.dest}`);
});

function showDetails(rel){
  const f=fileData.find(x=>x.relpath===rel); if(!f){ document.getElementById('details').innerHTML=''; return; }
  const semis=Number.isFinite(f.transpose_to_C_same_mode)? f.transpose_to_C_same_mode : 'n/a';
  const classMap={rhythmic_single_note:'Rhythmic (single-note)', monophonic_melodic:'Monophonic melodic', polyphonic_chordal:'Polyphonic/chordal'};
  const html=`
    <div class="kv"><span class="k">File</span><span class="v">${f.filename}</span></div>
    <div class="kv"><span class="k">Key / Mode</span><span class="v">${f.root? f.root : 'n/a'} ${f.mode? labelMode(f.mode): ''} <i style="font-weight:400;color:#8b95a3;">(${f.key_source || 'unknown'})</i></span></div>
    <div class="kv"><span class="k">Tempo (file)</span><span class="v">${f.tempo_bpm || '—'} bpm</span></div>
    <div class="kv"><span class="k">Time Sig / PPQ</span><span class="v">${f.time_signature} • ${f.ppq}</span></div>
    <div class="kv"><span class="k">Length (est)</span><span class="v">${f.bars_estimate || 0} bars</span></div>
    <div class="kv"><span class="k">Notes / Unique</span><span class="v">${f.note_count} • ${f.unique_pitches} ${f.over16_unique?'<span class="pill warn">>16 unique (Yamaha limit)</span>':''}</span></div>
    <div class="kv"><span class="k">Max Poly / Class</span><span class="v">${f.max_polyphony} • ${classMap[f.classification]||''}</span></div>
    <div class="kv"><span class="k">Channels</span><span class="v">${(f.channels||[]).join(', ')||'n/a'} ${f.uses_ch10?'<span class="pill">Suggest: Fixed</span>':''}</span></div>
    <div class="kv"><span class="k">Transpose → C</span><span class="v">${semis} semitones</span></div>`;
  const det=document.getElementById('details'); det.innerHTML=html;
  if((f.track_count||0)>1){ if(!trackState || trackState.rel!==rel){ initTrackState(rel, f.track_count, f.tracks||[]); } renderTrackMap(f); } else { trackState=null; }
}

initMIDI(); loadFiles();
</script>
</body></html>
"""

def main():
    global ROOT
    import argparse
    p=argparse.ArgumentParser(description="WebMIDI Clip Manager (reverted file list UI, stable build)")
    p.add_argument('--root', required=True, help='Root folder containing .mid files (scanned recursively)')
    p.add_argument('--port', type=int, default=8765, help='HTTP port (default 8765)')
    args=p.parse_args()
    ROOT=Path(args.root).expanduser().resolve()
    if not ROOT.exists(): raise SystemExit(f"Root folder not found: {ROOT}")
    app.run(host='127.0.0.1', port=args.port, debug=False)

if __name__=="__main__":
    main()
