from PIL import Image, ImageDraw


FRAME_COUNT = 12
FRAME_MS = 80
VALUE_FRAME_MS = 120
VALUE_SAMPLE_COUNT = 4
VALUE_SAMPLE_SECONDS = 0.5


def cyberpunk(draw, phase, accents):
    for index, accent in enumerate(accents):
        left = index * 32
        position = (phase * 30 // FRAME_COUNT + index * 7) % 30
        for offset, color in enumerate((accent, accent, "#fff6b8")):
            draw.point((left + 1 + (position + offset) % 30, 5), fill=color)
        # A matching signal runs below the numbers, leaving the meter intact.
        for offset in range(3):
            draw.point((left + 30 - (position + offset) % 30, 12), fill=accent)


def gta_vi(draw, phase, accents):
    horizon = ("#b23c82", "#d95392", "#f47c9e", "#ffac92", "#ffc68d", "#f47c9e")
    for index, accent in enumerate(accents):
        left = index * 32
        for segment in range(3):
            color = horizon[(phase // 2 + index + segment) % len(horizon)]
            draw.line((left + 1 + segment * 10, 5, left + 10 + segment * 10, 5), fill=color)
        # Short wave reflections leave the figures and bar readings unchanged.
        for wave in (phase, (phase + 6) % FRAME_COUNT):
            x = left + 2 + wave * 2
            draw.line((x, 12, x + 2, 12), fill=accent if wave < 6 else "#803e83")


def sweep(draw, phase, accents):
    for index, accent in enumerate(accents):
        left = index * 32
        marker = left + 2 + ((phase + index * 2) % FRAME_COUNT) * 2
        draw.line((marker, 5, marker + 2, 5), fill=accent)


EFFECTS = {"sweep": sweep, "cyberpunk": cyberpunk, "gta-vi": gta_vi}


def save_animation(images, target, theme, phase=0, scale=1, frame_ms=FRAME_MS):
    effect = EFFECTS[theme.animation]
    frames = []
    for index in range(FRAME_COUNT):
        current = images[index % len(images)].copy()
        effect(ImageDraw.Draw(current), (phase + index) % FRAME_COUNT, theme.accents)
        if scale != 1:
            current = current.resize((current.width * scale, current.height * scale), Image.Resampling.NEAREST)
        frames.append(current)
    frames[0].save(target, format="GIF", save_all=True, append_images=frames[1:], duration=frame_ms, loop=0)
