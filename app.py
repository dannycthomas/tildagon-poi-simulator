"""Faithful badge port of Danny Thomas's Automatic Poi Simulator.

The original Processing sketch advances integer angles by two degrees per
simulation frame and lets each poi independently choose a new motion only at
clean cardinal alignments.  This port keeps that state machine and its exact
speed tables, while adapting rendering and controls for Tildagon OS.

Controls:
  LEFT     show/hide trails
  RIGHT    force a new legal motion without resetting position
  UP/DOWN  raise/lower simulation rate by 5 fps
  CONFIRM  pause/resume
  CANCEL   leave the app
"""

import math
import random

try:
    import time
except ImportError:
    time = None

import app
from app_components import clear_background
from events.input import Buttons, BUTTON_TYPES


TWO_PI = 2.0 * math.pi
DEG_TO_RAD = math.pi / 180.0

# Original sketch proportions for a square viewport:
# armLength = min(width, height) / 5
# poiLength = min(width, height) / 6
ARM_LENGTH = 48.0
POI_LENGTH = 40.0
SHOULDER_Y = -5.0
POI_RADIUS = 4.0

TRAIL_POINTS = 48
TRAIL_BANDS = 6
MIN_SIM_FPS = 5
MAX_SIM_FPS = 60
FPS_STEP = 5
DEFAULT_SIM_FPS = 60
INPUT_COOLDOWN_MS = 170

# Exact speed choices from poi_auto.pde.  The outer index corresponds to
# ARM_SPEEDS (-2, -1, 0, +1, +2).
ARM_SPEEDS = (-2, -1, 0, 1, 2)
POI_SPEEDS = (
    (-6, -2, 2, 6),
    (-3, -2, -1, 2, 3),
    (-2, 2),
    (-3, -2, 1, 2, 3),
    (-6, -2, 2, 6),
)
CARDINAL_ANGLES = (0, 90, 180, 270)

# Colours from the original sketch, normalised for ctx.rgb/rgba.
POI_COLORS = (
    (0.0, 1.0, 1.0),
    (128.0 / 255.0, 160.0 / 255.0, 30.0 / 255.0),
)


def _pick(values):
    """MicroPython-friendly random.choice replacement."""
    return values[random.randrange(len(values))]


def _sign(value):
    """Match the original sign(): zero counts as positive."""
    return 1 if value >= 0 else -1


def _wrap_degrees(value):
    return value % 360


class PoiSimApp(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)

        try:
            if time is not None:
                random.seed(time.ticks_ms())
        except Exception:
            pass

        self.paused = False
        self.show_trails = True
        self.sim_fps = DEFAULT_SIM_FPS
        self.step_accumulator = 0.0
        self.input_cooldown_ms = 0
        self.change_flash_ms = 0
        self.change_source = ""

        # Exact initial state from setup() in the Processing sketch.
        self.arm_angle = [0, 0]
        self.poi_angle = [0, 0]
        self.arm_speed = [-1, 1]
        self.poi_speed = [3, -3]
        self.last_arm_speed = [-1, 1]
        self.last_poi_speed = [3, -3]
        self.elliptic_arm = [0.0, 0.0]

        self.trails = [[], []]
        self._sample_trails()

    def _hand_position(self, poi_id):
        """Port of getHandX/getHandY, including the elliptic parameter."""
        angle = self.arm_angle[poi_id] * DEG_TO_RAD
        elliptic = self.elliptic_arm[poi_id]

        x_scale = 1.0 - abs(min(elliptic, 0.0))
        y_scale = 1.0 - abs(max(elliptic, 0.0))

        hand_x = ARM_LENGTH * math.cos(angle) * x_scale
        hand_y = SHOULDER_Y + ARM_LENGTH * math.sin(angle) * y_scale
        return hand_x, hand_y

    def _poi_position(self, poi_id):
        """Port of getPoiX/getPoiY using the world-reference poi angle."""
        hand_x, hand_y = self._hand_position(poi_id)
        angle = self.poi_angle[poi_id] * DEG_TO_RAD
        return (
            hand_x + POI_LENGTH * math.cos(angle),
            hand_y + POI_LENGTH * math.sin(angle),
        )

    def _sample_trails(self):
        for poi_id in range(2):
            self.trails[poi_id].append(self._poi_position(poi_id))
            if len(self.trails[poi_id]) > TRAIL_POINTS:
                self.trails[poi_id].pop(0)

    def _choose_motion(self, poi_id, can_change_direction, avoid_same=False):
        """Choose speeds using the original arm/poi speed lookup table."""
        old_arm = self.arm_speed[poi_id]
        old_poi = self.poi_speed[poi_id]

        # The original allows a random choice to reproduce the same pair.
        # The manual control avoids that so a button press is visibly useful.
        attempts = 8 if avoid_same else 1
        new_arm = old_arm
        new_poi = old_poi

        for _ in range(attempts):
            speed_index = random.randrange(len(ARM_SPEEDS))
            candidate_arm = ARM_SPEEDS[speed_index]
            choices = POI_SPEEDS[speed_index]
            candidate_poi = _pick(choices)

            if not can_change_direction:
                while _sign(candidate_poi) != _sign(old_poi):
                    candidate_poi = _pick(choices)

            new_arm = candidate_arm
            new_poi = candidate_poi
            if not avoid_same or new_arm != old_arm or new_poi != old_poi:
                break

        self.last_arm_speed[poi_id] = old_arm
        self.last_poi_speed[poi_id] = old_poi
        self.arm_speed[poi_id] = new_arm
        self.poi_speed[poi_id] = new_poi

    def _try_auto_randomise(self, poi_id):
        """Port of randomise(ID), including alignment and direction rules."""
        arm_angle = self.arm_angle[poi_id]
        poi_angle = self.poi_angle[poi_id]

        can_randomise = False
        can_change_direction = False

        if arm_angle in CARDINAL_ANGLES:
            speed_product = self.poi_speed[poi_id] * self.arm_speed[poi_id]

            if poi_angle == arm_angle:
                can_randomise = True
                # Exact branch from the source: at same-direction alignment,
                # matching rotation signs may not reverse poi direction.
                can_change_direction = speed_product < 0
            elif poi_angle == _wrap_degrees(arm_angle + 180):
                can_randomise = True
                # At opposite alignment, the condition is reversed.
                can_change_direction = speed_product >= 0

        # The original disables randomisation when random() > 0.2, leaving a
        # 20 percent chance at each eligible alignment frame.
        if not can_randomise or random.random() > 0.2:
            return False

        self._choose_motion(poi_id, can_change_direction)
        return True

    def _force_pattern_change(self):
        """Badge-only control: change both motions without resetting angles."""
        for poi_id in range(2):
            self._choose_motion(poi_id, True, avoid_same=True)
        self.change_source = "MANUAL"
        self.change_flash_ms = 900

    def _simulate_step(self):
        """One original-style Processing frame."""
        changed = False

        for poi_id in range(2):
            self.arm_angle[poi_id] = _wrap_degrees(
                self.arm_angle[poi_id] + 2 * self.arm_speed[poi_id]
            )
            self.poi_angle[poi_id] = _wrap_degrees(
                self.poi_angle[poi_id] + 2 * self.poi_speed[poi_id]
            )

            if self._try_auto_randomise(poi_id):
                changed = True

        if changed:
            self.change_source = "AUTO"
            self.change_flash_ms = 650

        self._sample_trails()

    def _handle_buttons(self):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()
            return

        if self.input_cooldown_ms > 0:
            return

        handled = False

        if self.button_states.get(BUTTON_TYPES["LEFT"]):
            self.show_trails = not self.show_trails
            handled = True
        elif self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self._force_pattern_change()
            handled = True
        elif self.button_states.get(BUTTON_TYPES["UP"]):
            self.sim_fps = min(MAX_SIM_FPS, self.sim_fps + FPS_STEP)
            handled = True
        elif self.button_states.get(BUTTON_TYPES["DOWN"]):
            self.sim_fps = max(MIN_SIM_FPS, self.sim_fps - FPS_STEP)
            handled = True
        elif self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.paused = not self.paused
            handled = True

        if handled:
            self.button_states.clear()
            self.input_cooldown_ms = INPUT_COOLDOWN_MS

    def update(self, delta):
        # Tildagon OS supplies delta in milliseconds.
        if self.input_cooldown_ms > 0:
            self.input_cooldown_ms = max(0, self.input_cooldown_ms - delta)
        if self.change_flash_ms > 0:
            self.change_flash_ms = max(0, self.change_flash_ms - delta)

        self._handle_buttons()
        if self.paused:
            return

        # Reproduce frameRate-controlled motion: each simulation frame always
        # advances by the original integer two-degree speed unit; changing fps
        # changes the number of those frames performed per real second.
        delta_ms = min(delta, 100)
        self.step_accumulator += delta_ms * self.sim_fps / 1000.0

        # The clamp above normally limits this to six steps at 60 fps.  The hard
        # cap protects the badge if an unusually long frame is reported.
        steps = 0
        while self.step_accumulator >= 1.0 and steps < 8:
            self._simulate_step()
            self.step_accumulator -= 1.0
            steps += 1

        if steps == 8 and self.step_accumulator > 1.0:
            self.step_accumulator = 1.0

    @staticmethod
    def _draw_dot(ctx, x, y, radius, color, alpha=1.0):
        ctx.rgba(color[0], color[1], color[2], alpha)
        ctx.arc(x, y, radius, 0, TWO_PI, True).fill()

    @staticmethod
    def _draw_fading_path(ctx, points, color):
        """Approximate p5's translucent framebuffer with a bounded trail."""
        count = len(points)
        if count < 2:
            return

        for band in range(TRAIL_BANDS):
            start = (count * band) // TRAIL_BANDS
            end = (count * (band + 1)) // TRAIL_BANDS
            if end - start < 1:
                continue

            path_start = max(0, start - 1)
            alpha = 0.05 + 0.10 * (band + 1)

            ctx.save()
            ctx.line_width = 1.8
            ctx.rgba(color[0], color[1], color[2], alpha).begin_path()
            first = points[path_start]
            ctx.move_to(first[0], first[1])
            for point_index in range(path_start + 1, end):
                point = points[point_index]
                ctx.line_to(point[0], point[1])
            ctx.stroke()
            ctx.restore()

    @staticmethod
    def _center_text(ctx, text, y, size=9, color=(0.76, 0.79, 0.84)):
        ctx.font_size = size
        width = ctx.text_width(text)
        ctx.rgb(color[0], color[1], color[2]).move_to(-width / 2.0, y).text(text)

    def _draw_person(self, ctx):
        """Badge-scaled equivalent of drawPerson() from the source."""
        hand_0 = self._hand_position(0)
        hand_1 = self._hand_position(1)
        hip_y = SHOULDER_Y + 0.9 * ARM_LENGTH

        ctx.save()
        ctx.line_width = 7.0
        ctx.rgba(0.78, 0.08, 0.08, 0.20).begin_path()

        # Arms.
        ctx.move_to(0, SHOULDER_Y)
        ctx.line_to(hand_0[0], hand_0[1])
        ctx.move_to(0, SHOULDER_Y)
        ctx.line_to(hand_1[0], hand_1[1])

        # Body and legs.
        ctx.move_to(0, SHOULDER_Y)
        ctx.line_to(0, hip_y)
        ctx.line_to(0.4 * ARM_LENGTH, SHOULDER_Y + 2.0 * ARM_LENGTH)
        ctx.move_to(0, hip_y)
        ctx.line_to(-0.4 * ARM_LENGTH, SHOULDER_Y + 2.0 * ARM_LENGTH)
        ctx.stroke()

        # The original uses a narrow ellipse; a small circular head is cheaper
        # and visually clearer on the 240 px badge display.
        ctx.rgba(0.78, 0.08, 0.08, 0.24)
        ctx.arc(0, SHOULDER_Y - 0.30 * ARM_LENGTH, 6.5, 0, TWO_PI, True).fill()
        ctx.restore()

    def draw(self, ctx):
        clear_background(ctx)
        ctx.save()
        ctx.rgb(0.0, 0.0, 0.0).rectangle(-120, -120, 240, 240).fill()

        if self.show_trails:
            self._draw_fading_path(ctx, self.trails[0], POI_COLORS[0])
            self._draw_fading_path(ctx, self.trails[1], POI_COLORS[1])

        self._draw_person(ctx)

        # Draw each tether and poi after the person, matching the source order.
        for poi_id in range(2):
            hand_x, hand_y = self._hand_position(poi_id)
            poi_x, poi_y = self._poi_position(poi_id)
            color = POI_COLORS[poi_id]

            ctx.save()
            ctx.line_width = 2.0
            ctx.rgba(color[0], color[1], color[2], 0.88).begin_path()
            ctx.move_to(hand_x, hand_y)
            ctx.line_to(poi_x, poi_y)
            ctx.stroke()
            ctx.restore()

            self._draw_dot(ctx, poi_x, poi_y, POI_RADIUS, color, 1.0)

        title = "POI AUTO  {}fps".format(self.sim_fps)
        self._center_text(ctx, title, -108, 10)

        speed_line = "A {:+d}/{:+d}   B {:+d}/{:+d}".format(
            self.arm_speed[0],
            self.poi_speed[0],
            self.arm_speed[1],
            self.poi_speed[1],
        )
        self._center_text(ctx, speed_line, 91, 9)

        if self.paused:
            self._center_text(ctx, "PAUSED", 104, 9, (1.0, 0.72, 0.20))
        elif self.change_flash_ms > 0:
            self._center_text(ctx, self.change_source + " CHANGE", 104, 9)
        else:
            trails = "on" if self.show_trails else "off"
            self._center_text(ctx, "L trail:{}  R change  OK pause".format(trails), 104, 8)

        ctx.restore()


__app_export__ = PoiSimApp
