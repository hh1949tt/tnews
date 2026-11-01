"""语音合成：edge-tts 分段合成 + MP3 帧级拼接，无需 ffmpeg。

每段音频带 word boundary 时间戳，据此累计出每段的真实起始时间，
前端就能做“点击某条新闻跳到对应位置”的章节跳转。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import edge_tts

from .config import Config

_log = logging.getLogger(__name__)

# ---- MP3 帧参数表（Layer III）----
# 关键点：静音帧必须和真实音频的版本/采样率/声道完全一致，
# 否则解码器遇到参数突变的帧会直接抛错（表现为播到第一个停顿处就失败）。
_BITRATES = {
    3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],  # MPEG-1
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],  # MPEG-2
    0: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],  # MPEG-2.5
}
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}

# edge-tts 的默认输出：MPEG-2 Layer III / 24kHz / 48kbps / 单声道
_FALLBACK_FMT = {
    "ver_bits": 2,
    "bitrate_idx": 6,
    "sr_idx": 1,
    "ch_mode": 3,
    "sample_rate": 24000,
    "frame_len": 144,
    "samples": 576,
}


def _detect_mp3_format(data: bytes) -> dict | None:
    """从真实音频里嗅探出帧格式，用于生成同规格的静音帧。"""
    n = len(data)
    for i in range(min(n - 4, 8192)):
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            continue
        ver_bits = (data[i + 1] >> 3) & 0x03
        layer_bits = (data[i + 1] >> 1) & 0x03
        if ver_bits == 1 or layer_bits != 1:  # 只处理 Layer III
            continue
        br_idx = (data[i + 2] >> 4) & 0x0F
        sr_idx = (data[i + 2] >> 2) & 0x03
        if br_idx in (0, 15) or sr_idx == 3:
            continue
        bitrate = _BITRATES[ver_bits][br_idx]
        sample_rate = _SAMPLE_RATES[ver_bits][sr_idx]
        padding = (data[i + 2] >> 1) & 0x01
        ch_mode = (data[i + 3] >> 6) & 0x03
        frame_len = int((144 if ver_bits == 3 else 72) * bitrate * 1000 / sample_rate) + padding
        if frame_len < 24 or i + frame_len + 2 > n:
            continue
        # 校验下一帧也能同步，避免把数据噪声误判成帧头
        nxt = i + frame_len
        if data[nxt] != 0xFF or (data[nxt + 1] & 0xE0) != 0xE0:
            continue
        return {
            "ver_bits": ver_bits,
            "bitrate_idx": br_idx,
            "sr_idx": sr_idx,
            "ch_mode": ch_mode,
            "sample_rate": sample_rate,
            "frame_len": frame_len,
            "samples": 1152 if ver_bits == 3 else 576,
        }
    return None


def verify_audio(path: Path, max_scan: int = 4_000_000) -> dict:
    """成品自检：帧序列是否连续、参数是否统一、真实时长多少。

    帧参数混用会让浏览器在遇到突变帧时直接报错停播，这里主动发现比等用户反馈好。
    """
    data = path.read_bytes()[:max_scan]
    n = len(data)
    i = 0
    sigs: dict[tuple, int] = {}
    broken = 0
    duration = 0.0
    while i < n - 4:
        fmt = None
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            head = _detect_at(data, i)
            if head:
                fmt = head
        if not fmt:
            broken += 1
            i += 1
            continue
        key = (fmt["ver_bits"], _BITRATES[fmt["ver_bits"]][fmt["bitrate_idx"]],
               fmt["sample_rate"], fmt["ch_mode"])
        sigs[key] = sigs.get(key, 0) + 1
        duration += fmt["samples"] / fmt["sample_rate"]
        i += fmt["frame_len"]
    return {
        "ok": len(sigs) == 1 and broken == 0,
        "signatures": sigs,
        "broken_bytes": broken,
        "duration": round(duration, 2),
    }


def _detect_at(data: bytes, i: int) -> dict | None:
    """解析位置 i 处的单帧头，不校验后续帧。"""
    if i + 4 > len(data):
        return None
    ver_bits = (data[i + 1] >> 3) & 0x03
    layer_bits = (data[i + 1] >> 1) & 0x03
    if ver_bits == 1 or layer_bits != 1:
        return None
    br_idx = (data[i + 2] >> 4) & 0x0F
    sr_idx = (data[i + 2] >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = _BITRATES[ver_bits][br_idx]
    sample_rate = _SAMPLE_RATES[ver_bits][sr_idx]
    padding = (data[i + 2] >> 1) & 0x01
    frame_len = int((144 if ver_bits == 3 else 72) * bitrate * 1000 / sample_rate) + padding
    if frame_len < 24:
        return None
    return {
        "ver_bits": ver_bits,
        "bitrate_idx": br_idx,
        "sr_idx": sr_idx,
        "ch_mode": (data[i + 3] >> 6) & 0x03,
        "sample_rate": sample_rate,
        "frame_len": frame_len,
        "samples": 1152 if ver_bits == 3 else 576,
    }


def _silent_frame(fmt: dict) -> bytes:
    b1 = 0xE0 | (fmt["ver_bits"] << 3) | (1 << 1) | 0x01
    b2 = (fmt["bitrate_idx"] << 4) | (fmt["sr_idx"] << 2)
    b3 = fmt["ch_mode"] << 6
    return bytes([0xFF, b1, b2, b3]) + b"\x00" * (fmt["frame_len"] - 4)


def make_silence(seconds: float, fmt: dict | None = None) -> tuple[bytes, float]:
    """生成与源音频同规格的静音帧，返回 (字节, 实际时长)。"""
    fmt = fmt or _FALLBACK_FMT
    if seconds <= 0:
        return b"", 0.0
    frame_dur = fmt["samples"] / fmt["sample_rate"]
    frames = max(1, int(round(seconds / frame_dur)))
    return _silent_frame(fmt) * frames, round(frames * frame_dur, 4)


def _sanitize_for_tts(text: str) -> str:
    # 去掉 TTS 容易念错的符号，保留中文标点维持停顿
    import re

    out = text.replace("·", " ").replace("|", "，").replace("—", "，")
    out = out.replace("「", "").replace("」", "").replace("《", "").replace("》", "")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([，。、：；！？])", r"\1", out)
    while "，，" in out:
        out = out.replace("，，", "，")
    return out.strip()


async def _synth_once(text: str, voice: str, rate: str, volume: str, pitch: str) -> tuple[bytes, float]:
    # 显式要求词级边界，才能拿到精确的段落起始时间
    comm = edge_tts.Communicate(
        text, voice, rate=rate, volume=volume, pitch=pitch, boundary="WordBoundary"
    )
    audio = bytearray()
    last_end = 0.0
    async for chunk in comm.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            audio.extend(chunk["data"])
        elif ctype in ("WordBoundary", "SentenceBoundary"):
            end = (chunk.get("offset", 0) + chunk.get("duration", 0)) / 10_000_000
            if end > last_end:
                last_end = end
    data = bytes(audio)
    if data:
        # 用实际字节数按 48kbps 估算，比 word boundary 更接近真实时长
        # （word boundary 不含每段结尾的静音帧）
        last_end = max(last_end, len(data) / 6000.0)
    return data, last_end


async def _synth_with_retry(
    text: str, cfg: Config, retries: int = 2, rate: str | None = None, voice: str | None = None
) -> tuple[bytes, float]:
    tts_cfg = cfg.tts
    voice = voice or tts_cfg.get("voice", "zh-CN-YunxiNeural")
    rate = rate or tts_cfg.get("rate", "+0%")
    volume = tts_cfg.get("volume", "+0%")
    pitch = tts_cfg.get("pitch", "+0Hz")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _synth_once(text, voice, rate, volume, pitch)
        except Exception as e:  # 网络抖动 / 限流
            last_err = e
            await asyncio.sleep(1.2 * (attempt + 1))
    raise last_err  # type: ignore[misc]


async def synthesize(
    script: dict, cfg: Config, out_path: Path, rate: str | None = None, voice: str | None = None
) -> dict:
    """合成整期节目，返回时长与章节时间轴。"""
    segments = script["segments"]
    gap = float(cfg.tts.get("gap_seconds", 0.4))
    max_seg_chars = int(cfg.tts.get("max_chars_per_segment", 300))

    # 过长的段落按句切分，避免单次请求过大
    jobs: list[tuple[int, str]] = []
    for idx, seg in enumerate(segments):
        text = _sanitize_for_tts(seg["text"])
        if not text:
            jobs.append((idx, ""))
            continue
        if len(text) <= max_seg_chars:
            jobs.append((idx, text))
        else:
            parts, buf = [], ""
            for sent in text.replace("。", "。|").split("|"):
                if not sent:
                    continue
                if len(buf) + len(sent) > max_seg_chars and buf:
                    parts.append(buf)
                    buf = sent
                else:
                    buf += sent
            if buf:
                parts.append(buf)
            # 同一段的多个分片用极小停顿连接，时间戳继续累加
            jobs.append((idx, "\u0000".join(parts)))

    sem = asyncio.Semaphore(3)

    async def run(idx: int, text: str):
        if not text:
            return idx, b"", 0.0
        async with sem:
            total_audio = bytearray()
            total_dur = 0.0
            for piece in text.split("\u0000"):
                audio, dur = await _synth_with_retry(piece, cfg, rate=rate, voice=voice)
                total_audio.extend(audio)
                total_dur += dur
            return idx, bytes(total_audio), total_dur

    results = await asyncio.gather(*(run(i, t) for i, t in jobs))
    ordered = sorted(results, key=lambda r: r[0])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".part")

    chapters: list[dict] = []
    cursor = 0.0

    # 用真实音频的帧格式生成同规格静音，避免解码器遇到参数突变的帧
    fmt = None
    for _idx, _audio, _dur in ordered:
        if _audio:
            fmt = _detect_mp3_format(_audio)
            if fmt:
                break
    if not fmt:
        fmt = _FALLBACK_FMT
        if any(a for _i, a, _d in ordered):
            _log.warning("未能嗅探音频帧格式，静音段使用默认 24kHz/48kbps 参数")

    with open(tmp_path, "wb") as f:
        for idx, audio, dur in ordered:
            seg = segments[idx]
            if not audio:
                continue
            start = cursor
            f.write(audio)
            cursor += dur
            if gap > 0:
                silence, real_gap = make_silence(gap, fmt)
                f.write(silence)
                cursor += real_gap
            chapters.append(
                {
                    "index": idx,
                    "start": round(start, 2),
                    "duration": round(dur, 2),
                    "type": seg.get("type"),
                    "title": seg.get("title") or seg.get("label") or seg.get("text", "")[:24],
                    "category": seg.get("category"),
                }
            )
    os.replace(tmp_path, out_path)

    # 成品自检：帧参数混用会导致浏览器播到该处直接报“音频加载失败”
    check = verify_audio(out_path)
    if not check["ok"]:
        _log.warning(
            "音频自检未通过 %s：帧参数=%s 异常字节=%d。可能出现播放中断",
            out_path.name,
            list(check["signatures"].items()),
            check["broken_bytes"],
        )
    elif abs(check["duration"] - cursor) > max(2.0, cursor * 0.02):
        _log.warning(
            "音频自检时长偏差 %s：文件解析 %.1fs，时间轴记录 %.1fs",
            out_path.name,
            check["duration"],
            cursor,
        )

    return {
        "audio_file": out_path.name,
        "duration": round(cursor, 1),
        "chapters": chapters,
    }
