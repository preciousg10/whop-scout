"""Human-pacing engine.

Everything here exists to make the browsing rhythm look like a person rather than
a loop: jittered per-page delays, irregular AFK breaks, incremental wheel
scrolling, the occasional hover, and the rare "wait, let me re-check that" scroll
back up. Nothing here is periodic on purpose — every interval is re-randomized.
"""
import random
import time


class Pacer:
    def __init__(self, *, base_delay, fast_delay, fast_chance,
                 afk_every, afk_break, long_afk_chance, long_afk_break,
                 revisit_chance, scroll_step, scroll_pause):
        self.base_delay = base_delay          # (min, max) normal per-page delay, s
        self.fast_delay = fast_delay          # (min, max) fast click-through, s
        self.fast_chance = fast_chance        # prob. a given delay is a fast one
        self.afk_every = afk_every            # (min, max) campaigns between breaks
        self.afk_break = afk_break            # (min, max) break length, s
        self.long_afk_chance = long_afk_chance
        self.long_afk_break = long_afk_break  # (min, max) long break length, s
        self.revisit_chance = revisit_chance
        self.scroll_step = scroll_step        # (min, max) px per wheel tick
        self.scroll_pause = scroll_pause      # (min, max) s between wheel ticks
        self._since_break = 0
        self._next_break_at = random.randint(*afk_every)

    @property
    def campaigns_until_break(self):
        return max(self._next_break_at - self._since_break, 0)

    def page_delay(self):
        """Jittered think-time; ~20% of the time it's a fast click-through."""
        if random.random() < self.fast_chance:
            time.sleep(random.uniform(*self.fast_delay))
        else:
            time.sleep(random.uniform(*self.base_delay))

    def tick(self):
        """Call once per campaign handled. May trigger an AFK break."""
        self._since_break += 1
        if self._since_break >= self._next_break_at:
            self._afk_break()
            self._since_break = 0
            self._next_break_at = random.randint(*self.afk_every)

    def _afk_break(self):
        if random.random() < self.long_afk_chance:
            secs = random.uniform(*self.long_afk_break)
        else:
            secs = random.uniform(*self.afk_break)
        print(f"    ...afk break ~{int(secs)}s")
        time.sleep(secs)

    def human_scroll(self, page, steps=None):
        """Incremental wheel scrolls. Never jumps to bottom."""
        steps = steps if steps is not None else random.randint(2, 5)
        for _ in range(steps):
            page.mouse.wheel(0, random.randint(*self.scroll_step))
            time.sleep(random.uniform(*self.scroll_pause))

    def maybe_hover(self, locator):
        """Occasionally hover an element before acting on it."""
        if random.random() < 0.3:
            try:
                locator.hover(timeout=2000)
                time.sleep(random.uniform(0.2, 0.7))
            except Exception:
                pass

    def should_revisit(self):
        """5% (default): double back and re-look at the previous campaign."""
        return random.random() < self.revisit_chance
