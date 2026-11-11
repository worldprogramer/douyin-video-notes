"""Prepare evidence for Codex, then publish a reviewed illustrated Markdown note."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def stamp(seconds):
    ms = round(float(seconds) * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}.{ms:03d}'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(cmd):
    result = subprocess.run([str(x) for x in cmd], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-4000:])
    return result.stdout


def runtime(args):
    from runtime_support import discover_runtime
    return discover_runtime(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, model=args.model)


def require_path(value, label):
    if not value or not Path(value).exists():
        raise ValueError(f'缺少 {label}，请先执行 doctor 并配置本地路径。')
    return str(value)


def transcribe(audio, model_path, language, prompt, device):
    from faster_whisper import WhisperModel
    devices = ['cuda', 'cpu'] if device == 'auto' else [device]
    failures = []
    for chosen in devices:
        model = None
        try:
            model = WhisperModel(str(model_path), device=chosen,
                                 compute_type='int8_float16' if chosen == 'cuda' else 'int8',
                                 cpu_threads=max(1, min(8, (os.cpu_count() or 4) // 2)),
                                 num_workers=1, local_files_only=True)
            generator, info = model.transcribe(str(audio), language=None if language == 'auto' else language,
                                              beam_size=5, word_timestamps=True, vad_filter=True,
                                              vad_parameters={'min_silence_duration_ms': 500, 'speech_pad_ms': 250},
                                              condition_on_previous_text=False, initial_prompt=prompt or None)
            rows = []
            for seg in generator:  # Consume inside try: CUDA libraries may fail only on inference.
                words = [{'start': w.start, 'end': w.end, 'word': w.word, 'probability': w.probability}
                         for w in (seg.words or [])]
                row = {'id': seg.id, 'start': seg.start, 'end': seg.end, 'text': seg.text.strip(),
                       'avg_logprob': seg.avg_logprob, 'no_speech_prob': seg.no_speech_prob, 'words': words}
                row['needs_review'] = seg.avg_logprob < -0.8 or seg.no_speech_prob > 0.5 or any(w['probability'] < 0.4 for w in words)
                rows.append(row)
                print(f'已转录至 {stamp(seg.end)}', flush=True)
            return {'engine': 'faster-whisper', 'model': str(model_path), 'device': chosen,
                    'compute_type': 'int8_float16' if chosen == 'cuda' else 'int8',
                    'language': info.language, 'duration': info.duration,
                    'segments': rows, 'warnings': failures + ([] if rows else ['未识别出可靠语音，需检查画面与音轨。'])}
        except (RuntimeError, OSError) as exc:
            if chosen != 'cuda' or device != 'auto':
                raise
            failures.append('显卡推理不可用，已切换到本地 CPU。')
            print(failures[-1], flush=True)
        finally:
            del model


def transcript_files(folder, transcript):
    lines = ['# 完整转录', '', '> 以下为本地语音识别原稿，疑似错误需结合原声与画面复核。', '']
    subtitles = []
    for i, seg in enumerate(transcript['segments'], 1):
        label = ' · 待复核' if seg.get('needs_review') else ''
        lines += [f"## {stamp(seg['start'])}—{stamp(seg['end'])}{label}", '', seg['text'], '']
        subtitles += [str(i), f"{stamp(seg['start']).replace('.', ',')} --> {stamp(seg['end']).replace('.', ',')}", seg['text'], '']
    if not transcript['segments']:
        lines += [str(item) for item in transcript.get('warnings', ['未识别出可靠语音。'])] + ['']
    (folder / 'transcript.md').write_text('\n'.join(lines), encoding='utf-8')
    (folder / 'transcript.srt').write_text('\n'.join(subtitles), encoding='utf-8')


def prepare(args):
    from visual_frames import probe_video, prepare_frames
    job = Path(args.job).resolve()
    job.mkdir(parents=True, exist_ok=True)
    rt = runtime(args)
    ffmpeg = require_path(rt.get('ffmpeg'), 'FFmpeg')
    ffprobe = require_path(rt.get('ffprobe'), 'ffprobe')
    raw = args.input
    local = Path(raw).expanduser()
    if local.is_file():
        source = local.resolve()
        meta = {'path': str(source), 'title': source.stem, 'method': 'local', 'source_url': args.source_url or ''}
    else:
        from douyin_download import fetch_video
        meta = fetch_video(raw, job)
        source = Path(meta['path']).resolve()
    analysis = job / 'analysis'
    analysis.mkdir(exist_ok=True)
    video = probe_video(source, ffprobe)
    model = require_path(rt.get('model'), '本地转录模型') if video['has_audio'] else None
    identity = {'sha256': digest(source), 'model': model, 'language': args.language,
                'prompt': args.prompt, 'device': args.device, 'asr_settings_version': 1}
    state_file = analysis / 'state.json'
    previous = read(state_file) if state_file.exists() else None
    if previous and previous != identity and not args.force:
        raise ValueError('该工作目录已有不同视频或转录设置。请用新目录，或确认重算后加 --force。')
    dump(analysis / 'source.json', {**meta, 'path': str(source), 'sha256': identity['sha256'],
                                   'video': video, 'prepared_at': datetime.now(timezone.utc).isoformat()})
    transcript_path = analysis / 'transcript.json'
    if transcript_path.exists() and previous == identity and not args.force:
        transcript = read(transcript_path)
        print('复用已完成的转录。', flush=True)
    else:
        if video['has_audio']:
            audio = analysis / 'audio.wav'
            run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', source, '-map', '0:a:0',
                 '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', audio])
            transcript = transcribe(audio, model, args.language, args.prompt, args.device)
        else:
            transcript = {'engine': None, 'duration': video['duration'], 'segments': [], 'warnings': ['源视频没有音轨，仅根据画面整理。']}
        transcript['source_sha256'] = identity['sha256']
        dump(transcript_path, transcript)
        dump(state_file, identity)
    transcript_files(analysis, transcript)
    print('提取画面并制作时间戳拼图。', flush=True)
    frames = prepare_frames(source, analysis, ffmpeg, ffprobe, transcript_segments=transcript['segments'],
                            interval=args.interval, max_frames=args.max_frames, scene_threshold=args.scene_threshold)
    frames['source_sha256'] = identity['sha256']
    dump(analysis / 'frames.json', frames)
    dump(analysis / 'evidence.json', {'source': 'source.json', 'transcript': 'transcript.json',
                                     'frames': 'frames.json', 'status': 'awaiting_agent_review',
                                     'warnings': transcript.get('warnings', []) + frames.get('warnings', [])})
    print(json.dumps({'job': str(job), 'transcript': str(analysis / 'transcript.md'),
                      'frames': str(analysis / 'frames.json'), 'status': '请阅读全文和全部拼图，复核关键原图，再写笔记计划。'}, ensure_ascii=False))


def capture(args):
    from visual_frames import extract_frame
    job = Path(args.job).resolve()
    src = read(job / 'analysis/source.json')
    if digest(src['path']) != src['sha256']:
        raise ValueError('源视频已改变，请重新 prepare。')
    rt = runtime(args)
    output = job / 'analysis/details' / f'frame-{round(args.time * 1000):010d}{"-crop" if args.crop else ""}.png'
    output.parent.mkdir(parents=True, exist_ok=True)
    extract_frame(src['path'], output, args.time, rt['ffmpeg'], rt['ffprobe'], crop=args.crop)
    # Explicit provenance prevents substituting unrelated images in the final note.
    records_file = job / 'analysis/details.json'
    old_details = read(records_file) if records_file.exists() else {}
    records = old_details.get('frames', []) if isinstance(old_details, dict) and old_details.get('source_sha256') == src['sha256'] else []
    record = {'time': args.time, 'image': str(output.relative_to(job / 'analysis')).replace('\\', '/'), 'crop': args.crop}
    records = [r for r in records if r['image'] != record['image']] + [record]
    dump(records_file, {'source_sha256': src['sha256'], 'frames': records})
    print(str(output))


def finite_time(value, duration):
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > duration + 0.05:
        raise ValueError(f'时间戳超出视频范围：{value}')
    return value


def clean_label(text):
    return str(text).replace('\n', ' ').replace('[', '（').replace(']', '）').strip()


def publish(args):
    job = Path(args.job).resolve()
    analysis = job / 'analysis'
    src = read(analysis / 'source.json')
    if digest(src['path']) != src['sha256']:
        raise ValueError('源视频已改变，请重新 prepare。')
    plan = read(args.plan)
    duration = float(src['video']['duration'])
    if not plan.get('title') or not plan.get('summary') or not plan.get('sections'):
        raise ValueError('笔记计划必须有 title、summary、sections。')
    reader = plan.get('reader')
    if not isinstance(reader, dict) or not reader.get('title') or not reader.get('summary') or not reader.get('sections'):
        raise ValueError('笔记计划缺少完整的 reader。请补充提炼后的标题、摘要和主题小节；详细时间线保留在原 sections 中。')
    if not isinstance(reader['sections'], list):
        raise ValueError('reader.sections 必须是主题小节列表。')
    if not plan.get('review', {}).get('full_transcript_read') or not plan.get('review', {}).get('all_contact_sheets_viewed'):
        raise ValueError('请先阅读完整转录、看完全部拼图，再记录 review。')
    frame_data = read(analysis / 'frames.json')
    if not isinstance(frame_data, dict) or frame_data.get('source_sha256') != src['sha256']:
        raise ValueError('截图索引不属于当前源视频，请重新 prepare。')
    transcript = read(analysis / 'transcript.json')
    if transcript.get('source_sha256') != src['sha256']:
        raise ValueError('转录不属于当前源视频，请重新 prepare。')
    frames = list(frame_data.get('frames', []))
    detail_file = analysis / 'details.json'
    details = read(detail_file) if detail_file.exists() else {}
    if isinstance(details, dict) and details.get('source_sha256') == src['sha256']:
        frames += details.get('frames', [])
    known = {}
    for frame in frames:
        p = Path(frame['image'])
        p = p.resolve() if p.is_absolute() else (analysis / p).resolve()
        known[p] = frame
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / '笔记.md'
    detail_output = destination / '详细资料.md'
    if (output.exists() or detail_output.exists()) and not args.overwrite:
        raise ValueError('目标笔记或详细资料已存在；确认替换时加 --overwrite，或选新目录。')
    selected = []
    image_registry = {}

    def register_image(spec, start=None, end=None):
        if not isinstance(spec, dict) or not spec.get('file'):
            raise ValueError('配图需指定已登记截图的 file。')
        p = (analysis / spec['file']).resolve()
        if not p.is_relative_to(analysis) or p not in known or not p.is_file():
            raise ValueError(f'图片不是本视频的已登记截图：{spec["file"]}')
        timestamp = finite_time(known[p]['time'], duration)
        if start is not None and (timestamp < start - 1 or timestamp > end + 1):
            raise ValueError('截图时间不在所属章节，请调整章节范围或截图。')
        if not str(spec.get('caption') or '').strip() or not str(spec.get('reason') or '').strip():
            raise ValueError('每张配图需有 caption 和说明选图价值的 reason。')
        if p not in image_registry:
            name = f'画面-{len(selected) + 1:02d}{p.suffix.lower()}'
            image_registry[p] = name
            selected.append((p, name, spec, timestamp))
        return image_registry[p], timestamp, clean_label(spec['caption'])

    # Preserve the complete chronological evidence in its own document. Register
    # these images first so existing evidence keeps the same asset ordering.
    body = [f"# {clean_label(plan['title'])}", '']
    source_url = src.get('source_url', '')
    if source_url.startswith(('https://', 'http://')):
        body += [f'来源：[原视频]({source_url})', '']
    if src.get('author'):
        body += [f"作者：{clean_label(src['author'])}", '']
    body += [str(plan['summary']).strip(), '']
    for section in plan['sections']:
        start = finite_time(section['start'], duration)
        end = finite_time(section['end'], duration)
        if end < start or not section.get('body'):
            raise ValueError('章节时间顺序或正文无效。')
        body += [f"## {clean_label(section['heading'])}", '', f"*{stamp(start)}—{stamp(end)}*", '', str(section['body']).strip(), '']
        for spec in section.get('images', []):
            name, timestamp, caption = register_image(spec, start, end)
            body += [f'![{caption}](assets/{name})', '', f'*{stamp(timestamp)} · {caption}*', '']
    uncertainties = plan.get('uncertainties', [])
    if uncertainties:
        body += ['## 待核实', ''] + [f'- {item}' for item in uncertainties] + ['']
    body += ['[查看完整转录](完整转录.md)', '']
    # The primary note is an authored synthesis. Do not infer its organization
    # from timestamps or automatically prepend evidence metadata.
    reader_body = [f"# {clean_label(reader['title'])}", '', str(reader['summary']).strip(), '']
    for section in reader['sections']:
        if not isinstance(section, dict) or not str(section.get('heading') or '').strip() or not str(section.get('body') or '').strip():
            raise ValueError('提炼笔记的每个主题小节都需有 heading 和 body。')
        reader_body += [f"## {clean_label(section['heading'])}", '', str(section['body']).strip(), '']
        for spec in section.get('images', []):
            name, _, caption = register_image(spec)
            reader_body += [f'![{caption}](assets/{name})', '', f'*{caption}*', '']
    footer = '[查看详细资料](详细资料.md)'
    if source_url.startswith(('https://', 'http://')):
        footer += f' · [原视频]({source_url})'
    reader_body += [footer, '']
    assets = destination / 'assets'
    if selected:
        assets.mkdir(exist_ok=True)
    for p, name, _, _ in selected:
        shutil.copy2(p, assets / name)
    shutil.copy2(analysis / 'transcript.md', destination / '完整转录.md')
    shutil.copy2(analysis / 'transcript.srt', destination / '字幕.srt')
    detail_output.write_text('\n'.join(body), encoding='utf-8')
    output.write_text('\n'.join(reader_body), encoding='utf-8')
    dump(destination / '来源.json', {'title': plan['title'], 'source_url': source_url,
         'author': src.get('author'), 'sha256': src['sha256'], 'duration': duration,
         'images': [{'file': f'assets/{name}', 'time': t, 'caption': spec['caption']} for _, name, spec, t in selected]})
    dump(analysis / 'publication.json', {'output': str(output), 'details': str(detail_output), 'plan_sha256': digest(args.plan), 'image_count': len(selected)})
    print(json.dumps({'note': str(output), 'details': str(detail_output), 'images': len(selected)}, ensure_ascii=False))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='抖音视频图文整理')
    sub = parser.add_subparsers(dest='command', required=True)
    def env_args(p):
        p.add_argument('--ffmpeg'); p.add_argument('--ffprobe'); p.add_argument('--model')
    doctor = sub.add_parser('doctor'); env_args(doctor)
    prep = sub.add_parser('prepare'); env_args(prep)
    prep.add_argument('--input', required=True); prep.add_argument('--job', required=True)
    prep.add_argument('--source-url'); prep.add_argument('--language', default='zh')
    prep.add_argument('--prompt', default=''); prep.add_argument('--device', choices=['cpu', 'cuda', 'auto'], default='cpu')
    prep.add_argument('--interval', type=float, default=5); prep.add_argument('--max-frames', type=int, default=180)
    prep.add_argument('--scene-threshold', type=float, default=.25); prep.add_argument('--force', action='store_true')
    cap = sub.add_parser('capture'); env_args(cap)
    cap.add_argument('--job', required=True); cap.add_argument('--time', type=float, required=True)
    cap.add_argument('--crop', type=float, nargs=4, metavar=('X', 'Y', 'W', 'H'))
    pub = sub.add_parser('publish')
    pub.add_argument('--job', required=True); pub.add_argument('--plan', required=True); pub.add_argument('--output', required=True)
    pub.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    if args.command == 'doctor':
        print(json.dumps(runtime(args), ensure_ascii=False, indent=2))
    elif args.command == 'prepare': prepare(args)
    elif args.command == 'capture': capture(args)
    else: publish(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        print(f'处理未完成：{exc}', file=sys.stderr)
        sys.exit(1)
