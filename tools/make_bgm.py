# 生成 4 首无版权背景轻音乐（本地合成，可自由分发）→ web/bgm/bgm1~4.mp3
# 风格：1 晨光钢琴 pad / 2 云端长音 / 3 风铃八音盒 / 4 深夜低音氛围
import numpy as np
import lameenc
import pathlib

SR = 44100
CHORD_LEN = 12.0
OUT = pathlib.Path('web/bgm')
OUT.mkdir(parents=True, exist_ok=True)

CHORDS = [
    [130.81, 196.00, 246.94, 293.66, 329.63],   # C add9
    [110.00, 164.81, 261.63, 329.63, 392.00],   # Am7
    [ 87.31, 174.61, 220.00, 261.63, 349.23],   # F maj7
    [ 98.00, 196.00, 246.94, 293.66, 392.00],   # G6
]
PENTA = [523.25, 587.33, 659.26, 783.99, 880.00, 1046.50]
LEN = CHORD_LEN * 4
total = int(SR * LEN)


def render(seed):
    buf = np.zeros(total)
    rng = np.random.default_rng(seed)

    def add_tone(start, dur, freq, amp, attack, release, detune=0.0):
        n0 = int(start * SR)
        n = int(dur * SR)
        if n0 + n > total:
            return
        t = np.arange(n) / SR
        env = np.ones(n)
        na, nr = min(int(attack * SR), n), min(int(release * SR), n)
        if na > 0:
            env[:na] = np.linspace(0, 1, na) ** 2
        if nr > 0:
            env[-nr:] *= np.linspace(1, 0, nr) ** 2
        f = freq * (1 + detune)
        wave = (np.sin(2 * np.pi * f * t)
                + 0.28 * np.sin(2 * np.pi * 2 * f * t)
                + 0.10 * np.sin(2 * np.pi * 3 * f * t))
        buf[n0:n0 + n] += amp * env * wave

    return buf, rng, add_tone


def encode(buf, name):
    buf = buf / max(1e-9, np.abs(buf).max()) * 0.55
    fade = int(0.8 * SR)
    buf[:fade] *= np.linspace(0, 1, fade)
    buf[-fade:] *= np.linspace(1, 0, fade)
    pcm = (buf * 32767).astype(np.int16)
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(SR)
    enc.set_channels(1)
    enc.set_quality(2)
    data = enc.encode(pcm.tobytes()) + enc.flush()
    (OUT / name).write_bytes(data)
    print(name, len(data), 'bytes')


# --- bgm1 晨光钢琴：pad + 五声音阶点缀（原版风格） ---
buf, rng, tone = render(42)
for ci, ch in enumerate(CHORDS):
    base = ci * CHORD_LEN
    for k, f in enumerate(ch):
        amp = 0.16 / (k + 1.4)
        tone(base, CHORD_LEN + 2.5, f, amp, 3.5, 3.5, detune=0.0012)
        tone(base + 0.4, CHORD_LEN + 2.0, f, amp * 0.7, 4.0, 4.0, detune=-0.0012)
t = 1.0
while t < LEN - 2:
    tone(t, 3.2, PENTA[rng.integers(0, len(PENTA))], rng.uniform(0.05, 0.09), 0.012, 2.8)
    if rng.random() < 0.22:
        tone(t + 0.18, 2.6, PENTA[rng.integers(0, len(PENTA))], 0.045, 0.012, 2.2)
    t += rng.uniform(1.6, 3.2)
encode(buf, 'bgm1.mp3')

# --- bgm2 云端长音：纯 pad，微失谐叠厚，无点缀，更空灵 ---
buf, _, tone = render(7)
for ci, ch in enumerate(CHORDS):
    base = ci * CHORD_LEN
    for k, f in enumerate(ch):
        for d in (0.0021, -0.0021, 0.0):
            tone(base, CHORD_LEN + 3.5, f, 0.10 / (k + 1.3), 4.5, 4.5, detune=d)
encode(buf, 'bgm2.mp3')

# --- bgm3 风铃八音盒：极轻 pad 底 + 稀疏高音短音（快速衰减） ---
buf, rng, tone = render(99)
for ci, ch in enumerate(CHORDS):
    base = ci * CHORD_LEN
    for k, f in enumerate(ch[:3]):
        tone(base, CHORD_LEN + 2.0, f, 0.07 / (k + 1.2), 4.0, 4.0)
t = 0.6
while t < LEN - 1.5:
    f = PENTA[rng.integers(0, len(PENTA))] * (2 if rng.random() < 0.35 else 1)
    tone(t, 1.8, f, rng.uniform(0.06, 0.10), 0.004, 1.5)
    if rng.random() < 0.30:
        tone(t + rng.uniform(0.1, 0.4), 1.4, PENTA[rng.integers(0, len(PENTA))], 0.05, 0.004, 1.2)
    t += rng.uniform(0.9, 2.2)
encode(buf, 'bgm3.mp3')

# --- bgm4 深夜氛围：低八度 pad + 每 4 秒一次的柔和低音脉冲 ---
buf, _, tone = render(2024)
for ci, ch in enumerate(CHORDS):
    base = ci * CHORD_LEN
    for k, f in enumerate(ch):
        tone(base, CHORD_LEN + 3.0, f / 2, 0.15 / (k + 1.6), 3.8, 3.8, detune=0.0008)
    for p in range(3):
        tone(base + p * 4.0, 2.4, ch[0] / 2, 0.10, 0.6, 1.6)
encode(buf, 'bgm4.mp3')

print('all bgm done')
