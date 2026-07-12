#!/usr/bin/env python3
"""
bench_carousel.py — benchmark carousel album rendering approaches.

Usage:  python3 bench_carousel.py [REPS=20]

Run on the Pi to measure actual timings for each method.
4 side albums rendered per frame (typical animation scenario).
"""

import os, sys, time, math
os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
import pygame
pygame.init()

W, H           = 720, 720
CAR_SIZE       = 350
CAR_COMP       = 0.6
CAR_SIDE_SCALE = 0.85
CAR_CAM_D      = 5.0
N_STRIPS       = 32
CELL_W         = 229
COL_BG         = (10, 10, 14)

screen = pygame.display.set_mode((W, H))

# ── geometry (mirrors display.py _slot) ───────────────────────────────────────
def slot(d):
    a        = abs(d)
    compress = max(CAR_COMP, 1.0 - (1.0 - CAR_COMP) * min(a, 1.0))
    w        = max(4, int(CAR_SIZE * compress))
    sc       = 1.0 if a < 0.001 else max(CAR_SIDE_SCALE, 1.0 - (1.0 - CAR_SIDE_SCALE) * min(a, 1.0))
    st       = math.sqrt(max(0.0, 1.0 - compress * compress))
    hz       = st * CAR_SIZE * 0.5
    if hz > 1.0:
        near_h = int(sc * CAR_SIZE * CAR_CAM_D / max(1.0, CAR_CAM_D - hz))
        far_h  = int(sc * CAR_SIZE * CAR_CAM_D / (CAR_CAM_D + hz))
    else:
        near_h = far_h = CAR_SIZE
    return w, near_h, far_h, compress

# ── synthetic thumbnail ───────────────────────────────────────────────────────
def make_thumb(size):
    s = pygame.Surface((size, size))
    try:
        import numpy as np
        arr = pygame.surfarray.pixels3d(s)
        xs  = np.linspace(55, 200, size, dtype=np.uint8)
        ys  = np.linspace(30, 180, size, dtype=np.uint8)
        xx, yy = np.meshgrid(xs, ys, indexing='ij')
        arr[:, :, 0] = yy
        arr[:, :, 1] = xx
        arr[:, :, 2] = 100
        del arr
    except ImportError:
        for y in range(0, size, 3):
            pygame.draw.line(s, (55 + y*145//size, 80, 120), (0, y), (size-1, y))
    return s

# ── rendering methods ─────────────────────────────────────────────────────────

def m_strips(thumb, d, N=N_STRIPS, smooth=True):
    """N independent strips, each smoothscale'd (current production path)."""
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    tw, th = thumb.get_size()
    surf   = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    sfn    = pygame.transform.smoothscale if smooth else pygame.transform.scale
    shmax  = int(130 * (1.0 - compress))
    for col in range(N):
        t  = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        sx = int(col * tw / N);  sw = max(1, int((col+1) * tw / N) - sx)
        dx = int(col * w  / N);  dw = max(1, int((col+1) * w  / N) - dx)
        st = sfn(thumb.subsurface((sx, 0, sw, th)), (dw, ch))
        sa = int(shmax * t)
        if sa > 0:
            st.fill((max(0, 255 - sa),) * 3, special_flags=pygame.BLEND_MULT)
        surf.blit(st, (dx, dy))
    return surf

def m_single_smooth(thumb, d, N=N_STRIPS):
    """1 smoothscale to full size, then blit clips per strip — no per-strip scale.
    Perspective is approximate: correct width but uniform vertical scale."""
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    big   = pygame.transform.smoothscale(thumb, (w, max_h))
    surf  = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    shmax = int(130 * (1.0 - compress))
    for col in range(N):
        t  = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        dx = int(col * w / N);  dw = max(1, int((col+1) * w / N) - dx)
        sa = int(shmax * t)
        if sa > 0:
            st = big.subsurface((dx, dy, dw, ch)).copy()
            st.fill((max(0, 255 - sa),) * 3, special_flags=pygame.BLEND_MULT)
            surf.blit(st, (dx, dy))
        else:
            surf.blit(big, (dx, dy), area=(dx, dy, dw, ch))
    return surf

def m_single_smooth_noshadow(thumb, d):
    """1 smoothscale + clip, no shadow — isolates blit overhead."""
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    big   = pygame.transform.smoothscale(thumb, (w, max_h))
    surf  = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    for col in range(N_STRIPS):
        t  = (1.0 - col / max(1, N_STRIPS-1)) if d > 0 else (col / max(1, N_STRIPS-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        dx = int(col * w / N_STRIPS);  dw = max(1, int((col+1) * w / N_STRIPS) - dx)
        surf.blit(big, (dx, dy), area=(dx, dy, dw, ch))
    return surf

def m_numpy_loop(thumb, d, N=N_STRIPS):
    """Numpy per-column resampling — no Surface allocation per strip."""
    import numpy as np
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    tw, th = thumb.get_size()
    src    = pygame.surfarray.array3d(thumb)   # (tw, th, 3) uint8
    out    = np.empty((w, max_h, 3), dtype=np.uint8)
    out[:] = COL_BG
    shmax  = int(130 * (1.0 - compress))
    for col in range(N):
        t    = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch   = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy   = (max_h - ch) // 2
        sx   = int(col * tw / N);  sx_e = max(sx + 1, int((col+1) * tw / N))
        dx   = int(col * w  / N);  dx_e = max(dx + 1, int((col+1) * w  / N))
        sc   = (src[sx:sx_e].mean(axis=0) if sx_e > sx + 1
                else src[sx].astype(np.float32))                   # (th, 3)
        yi   = np.round(np.linspace(0, th - 1, ch)).astype(np.int32)
        data = sc[yi]                                              # (ch, 3)
        sa   = int(shmax * t)
        if sa > 0:
            data = data * ((255 - sa) / 255.0)
        out[dx:dx_e, dy:dy + ch] = data.astype(np.uint8)
    return pygame.surfarray.make_surface(out)

def m_numpy_vectorized(thumb, d, N=N_STRIPS):
    """Fully vectorized numpy — no Python loop over strips or pixels.
    Builds the entire album surf in one pass using index arrays."""
    import numpy as np
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    tw, th = thumb.get_size()
    src    = pygame.surfarray.array3d(thumb)   # (tw, th, 3) uint8
    shmax  = int(130 * (1.0 - compress))

    # Per-destination-x column metadata  shape: (w,)
    dx_all = np.arange(w, dtype=np.float32)
    c_all  = np.clip((dx_all * N / w).astype(np.int32), 0, N - 1)
    if d > 0:
        t_all = 1.0 - c_all / max(1, N - 1)
    else:
        t_all = c_all.astype(np.float32) / max(1, N - 1)
    ch_all = np.maximum(1, (far_h + (near_h - far_h) * (1.0 - t_all)).astype(np.int32))
    dy_all = (max_h - ch_all) // 2
    sx_all = np.clip((dx_all * tw / w).astype(np.int32), 0, tw - 1)
    sh_all = (255 - np.minimum(shmax, (shmax * t_all).astype(np.int32))
              ).astype(np.float32) / 255.0                         # (w,)

    # 2-D grids: (w, max_h)
    DY    = np.arange(max_h, dtype=np.int32)[np.newaxis, :]       # (1, max_h)
    valid = (DY >= dy_all[:, np.newaxis]) & (DY < (dy_all + ch_all)[:, np.newaxis])

    ch_f  = np.maximum(ch_all, 2).astype(np.float32)
    frac  = np.clip((DY - dy_all[:, np.newaxis]).astype(np.float32)
                    / (ch_f[:, np.newaxis] - 1.0), 0.0, 1.0)
    sy_all = (frac * (th - 1)).astype(np.int32)                   # (w, max_h)

    gathered = src[sx_all[:, np.newaxis], sy_all]                  # (w, max_h, 3)
    shaded   = (gathered * sh_all[:, np.newaxis, np.newaxis]).astype(np.uint8)

    out    = np.empty((w, max_h, 3), dtype=np.uint8)
    out[:] = COL_BG
    out[valid] = shaded[valid]
    return pygame.surfarray.make_surface(out)

def m_strips_noshadow(thumb, d, N=N_STRIPS, smooth=True):
    """N strips, no shadow — isolates strip-smoothscale cost without BLEND_MULT."""
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    tw, th = thumb.get_size()
    surf   = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    sfn    = pygame.transform.smoothscale if smooth else pygame.transform.scale
    for col in range(N):
        t  = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        sx = int(col * tw / N);  sw = max(1, int((col+1) * tw / N) - sx)
        dx = int(col * w  / N);  dw = max(1, int((col+1) * w  / N) - dx)
        surf.blit(sfn(thumb.subsurface((sx, 0, sw, th)), (dw, ch)), (dx, dy))
    return surf

def m_single_smooth_noshadow_n(thumb, d, N=N_STRIPS):
    """Single smoothscale + N clips, no shadow — variable N."""
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    big   = pygame.transform.smoothscale(thumb, (w, max_h))
    surf  = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    for col in range(N):
        t  = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        dx = int(col * w / N);  dw = max(1, int((col+1) * w / N) - dx)
        surf.blit(big, (dx, dy), area=(dx, dy, dw, ch))
    return surf

def m_gradient_shadow(thumb, d, N=N_STRIPS):
    """Single smoothscale + numpy gradient shadow (1 BLEND_MULT blit) + N clips.
    Replaces 128 copy()+fill(BLEND_MULT) with 1 numpy op + 1 blit.
    Visual quality: correct shadow gradient, approximate perspective (uniform V scale)."""
    import numpy as np
    w, near_h, far_h, compress = slot(d)
    max_h = near_h
    big   = pygame.transform.smoothscale(thumb, (w, max_h))
    shmax = int(130 * (1.0 - compress))
    if shmax > 0:
        # Build a (w,1) multiply-mask and scale it to (w, max_h)
        col_idx = np.clip((np.arange(w) * N / w).astype(np.int32), 0, N - 1)
        t_arr   = (1.0 - col_idx / max(1, N - 1)) if d > 0 \
                  else col_idx.astype(np.float32) / max(1, N - 1)
        df_arr  = np.maximum(0, 255 - (shmax * t_arr).astype(np.int32)).astype(np.uint8)
        mask_col = np.empty((w, 1, 3), dtype=np.uint8)
        mask_col[:, 0, :] = df_arr[:, np.newaxis]
        smask   = pygame.transform.scale(pygame.surfarray.make_surface(mask_col), (w, max_h))
        big.blit(smask, (0, 0), special_flags=pygame.BLEND_MULT)
    surf  = pygame.Surface((w, max_h))
    surf.fill(COL_BG)
    for col in range(N):
        t  = (1.0 - col / max(1, N-1)) if d > 0 else (col / max(1, N-1))
        ch = max(1, int(far_h + (near_h - far_h) * (1.0 - t)))
        dy = (max_h - ch) // 2
        dx = int(col * w / N);  dw = max(1, int((col+1) * w / N) - dx)
        surf.blit(big, (dx, dy), area=(dx, dy, dw, ch))
    return surf

# ── full-frame helper ─────────────────────────────────────────────────────────
def full_frame(side_fn, ds, thumb):
    """Render one complete carousel frame: fill + all albums blitted to screen."""
    screen.fill(COL_BG)
    for d in ds:
        if abs(d) < 0.01:
            # Centre album: single smoothscale (not benchmarked)
            w, nh, _, _ = slot(d)
            surf = pygame.Surface((w, nh))
            surf.fill(COL_BG)
            surf.blit(pygame.transform.smoothscale(thumb, (w, nh)), (0, 0))
        else:
            surf = side_fn(thumb, d)
        screen.blit(surf, (W // 2 - surf.get_width() // 2, 50))

# ── benchmark runner ──────────────────────────────────────────────────────────
def bench(label, fn, reps):
    fn()   # warm-up
    t0 = time.perf_counter()
    for _ in range(reps):
        fn()
    ms  = (time.perf_counter() - t0) / reps * 1000
    fps = 1000.0 / ms if ms > 0 else 9999
    print(f"  {label:<46}  {ms:6.1f} ms  (~{fps:4.0f} fps equiv)")

def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    print("Preparing thumbnails...")
    t_small = make_thumb(CELL_W)     # 229×229  (current grid/thumb size)
    t_big   = make_thumb(CAR_SIZE)   # 350×350  (pre-scaled to carousel size)

    try:
        import numpy as np
        HAS_NP = True
        print(f"numpy {np.__version__} available")
    except ImportError:
        HAS_NP = False
        print("numpy not available — numpy methods skipped")

    print(f"Benchmarking {reps} frames each.  "
          f"Screen: {W}×{H}.  Thumb small: {CELL_W}px  big: {CAR_SIZE}px\n")

    # Two scenarios: integer d (what settled state sees) and fractional d (animation)
    for label, ds in [
        ("SETTLED   — d = 0, ±1, ±2  (integer positions)",   [-2.0, -1.0, 0.0,  1.0,  2.0]),
        ("ANIMATING — d = ±0.5, ±1.5  (mid-swipe positions)", [-1.5, -0.5, 0.5,  1.5]),
    ]:
        print(f"── {label}")

        # helper: pre-bind thumb so the lambda only takes (thumb, d)
        def B(fn, *args, **kw):
            return lambda t, d: fn(t, d, *args, **kw)

        # strips × N × smoothscale/scale
        for N in (32, 16, 8, 4):
            bench(f"strips N={N:<2} smoothscale",
                  lambda ds=ds, N=N: full_frame(B(m_strips, N, True),   ds, t_small), reps)
        for N in (8, 4):
            bench(f"strips N={N:<2} scale (nearest-neighbor)",
                  lambda ds=ds, N=N: full_frame(B(m_strips, N, False),  ds, t_small), reps)

        # single smoothscale + clip
        bench("single smooth + N=32 clip (approx perspective)",
              lambda ds=ds: full_frame(B(m_single_smooth, 32),          ds, t_small), reps)
        bench("single smooth + N=8  clip (approx perspective)",
              lambda ds=ds: full_frame(B(m_single_smooth, 8),           ds, t_small), reps)
        bench("single smooth + N=32 clip NO shadow",
              lambda ds=ds: full_frame(lambda t, d: m_single_smooth_noshadow(t, d),
                                       ds, t_small), reps)

        # strips without shadow — isolates smoothscale cost vs BLEND_MULT cost
        if HAS_NP:
            bench("strips N=32 smooth  NO shadow",
                  lambda ds=ds: full_frame(B(m_strips_noshadow, 32, True),   ds, t_small), reps)
            bench("strips N=8  smooth  NO shadow",
                  lambda ds=ds: full_frame(B(m_strips_noshadow, 8,  True),   ds, t_small), reps)
            bench("strips N=4  smooth  NO shadow",
                  lambda ds=ds: full_frame(B(m_strips_noshadow, 4,  True),   ds, t_small), reps)
            bench("strips N=4  scale   NO shadow",
                  lambda ds=ds: full_frame(B(m_strips_noshadow, 4,  False),  ds, t_small), reps)

            # single smooth + no shadow, variable N
            bench("single smooth + N=8  clip NO shadow",
                  lambda ds=ds: full_frame(B(m_single_smooth_noshadow_n, 8),  ds, t_small), reps)
            bench("single smooth + N=4  clip NO shadow",
                  lambda ds=ds: full_frame(B(m_single_smooth_noshadow_n, 4),  ds, t_small), reps)

            # gradient shadow (1 numpy op + 1 BLEND_MULT blit) + clip strips
            bench("gradient shadow + N=32 clips",
                  lambda ds=ds: full_frame(B(m_gradient_shadow, 32),          ds, t_small), reps)
            bench("gradient shadow + N=8  clips",
                  lambda ds=ds: full_frame(B(m_gradient_shadow, 8),           ds, t_small), reps)
            bench("gradient shadow + N=4  clips",
                  lambda ds=ds: full_frame(B(m_gradient_shadow, 4),           ds, t_small), reps)

            # 3 side albums instead of 4: reduce visibility threshold
            ds3 = [d for d in ds if abs(d) <= 1.6]
            bench(f"gradient shadow + N=8  — 3 albums (±1 only, d≤1.6)",
                  lambda ds3=ds3: full_frame(B(m_gradient_shadow, 8),         ds3, t_small), reps)
            bench(f"single smooth N=8 noshadow — 3 albums (±1 only)",
                  lambda ds3=ds3: full_frame(B(m_single_smooth_noshadow_n, 8), ds3, t_small), reps)

        print()

    print("Notes:")
    print("  'single smooth' approximates perspective: correct width, uniform vertical scale")
    print("  'gradient shadow' = 1 numpy mask + 1 BLEND_MULT blit vs N copy()+fill() per album")
    print("  '3 albums' = visibility threshold 1.6 instead of 2.4 (only ±1 side album)")
    print("  numpy methods may allocate large temporary arrays")

if __name__ == "__main__":
    main()
