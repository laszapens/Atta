# -*- coding: utf-8 -*-
"""A single-stroke (Hershey Simplex Roman) glyph table — public-domain data.

Why a stroke font, not an outline font
--------------------------------------
Atta has no font files, no FreeType, and no SDF pipeline, and it must stay
licence-clean and dependency-free.  A **single-stroke** ("engraving") font is
the right primitive for that job: every glyph is a small set of open polylines
on the centre line of the stroke, so text becomes a chain of boxes we can
sweep, union and cut.  Outline fonts would need Bézier flattening plus 2-D
polygon offsetting with holes — far more machinery, and a font-file licence.
This module is therefore a *stroke* font: it gives you polylines, and the
caller decides the stroke width.  Letters are medians, not contours; closed
counters ('O', 'A', '8') are open strokes, not holes.

Provenance and licence
----------------------
* Glyph shapes: the **Hershey Simplex Roman** ("futural") vector font, created
  circa 1967 by Dr. A. V. Hershey while at the U.S. National Bureau of
  Standards.  U.S. Government work — public domain.
* Digitisation: James Hurt's ``.jhf`` conversion, distributed by the Hershey
  Fonts project (Kamal Mostafa), file ``hershey-fonts/futural.jhf``,
  <https://github.com/kamalmostafa/hershey-fonts>, retrieved 2026-11-02,
  blob sha1 ``9bff530527ae69b97984190f01a8271545c9802b`` (3498 bytes).
  The data distribution's use restriction permits any use, commercial or
  otherwise, and conversion to any format except the original NTIS one,
  provided these acknowledgements travel with the data:

      The Hershey Fonts were originally created by Dr. A. V. Hershey while
      working at the U. S. National Bureau of Standards.
      The format of the Font data in this distribution was originally created
      by James Hurt, Cognition, Inc., 900 Technology Park Drive, Billerica,
      MA 01821.

  The GPL-2+ ``libhersheyfont`` C library from the same repository was *not*
  used or copied: only the unencumbered glyph data was parsed, with a parser
  written for this module from the documented format
  (``hershey-fonts.notes``): columns 0:5 glyph number, 5:8 vertex count,
  column 8 left bearing, column 9 right bearing, then coordinate pairs
  offset by ASCII 'R', with the pair ``" R"`` meaning pen-up.

What is embedded
----------------
The table below is the *whole* printable ASCII range (32-126, 95 glyphs)
transcribed from that file into integer thousandths of the cap height, so the
module is self-contained (no file, no network, no font dependency at run
time).  Nothing else from the source is included.

Coordinate system
-----------------
* ``x`` starts at the left bearing of the glyph; ``y`` is up.
* **cap height = 1.0**, **baseline = y 0.0**.  Uppercase letters span 0..1,
  lowercase x-height is 2/3, descenders reach -1/3 (Hershey's 21/14/7 units).
* ``advance`` is the pen advance to the next glyph, in the same units.
* All geometry is 2-D (XY); z belongs to the caller.

Public API
----------
``glyph_polylines(ch)``   strokes of one glyph as float arrays, cap = 1.0
``glyph_advance(ch)``     pen advance of one glyph
``text_width(text)``      advance width of a string
``text_ink_bounds(text)`` ink box, optionally inflated by a stroke width
``layout_strokes(...)``   laid-out, aligned strokes scaled to a cap height
``charset()``             every supported character
"""

from __future__ import annotations

import numpy as np

__all__ = ["EM", "CAP_HEIGHT", "X_HEIGHT", "DESCENDER", "FALLBACK",
           "charset", "has_glyph", "missing", "glyph_polylines", "glyph_advance",
           "text_width", "text_ink_bounds", "layout_strokes", "metrics"]

#: Integer units per cap height in :data:`_GLYPHS`.
EM = 1000

CAP_HEIGHT = 1.0
X_HEIGHT = 2.0 / 3.0
DESCENDER = -1.0 / 3.0

#: Glyph substituted for a character that is not in the table.
FALLBACK = "?"

#: char -> (advance, (stroke, ...)); a stroke is x0,y0,x1,y1,... in 1/EM units.
_GLYPHS = {
    " ": (762,()),
    "!": (476,((238,1000,238,333),(238,95,190,48,238,0,286,48,238,95))),
    "\"": (762,((190,1000,190,667),(571,1000,571,667))),
    "#": (1000,((524,1190,190,-333),(810,1190,476,-333),(190,571,857,571),(143,286,810,286))),
    "$": (
            952,
            (
                (381,1190,381,-190),
                (571,1190,571,-190),
                (
                    810, 857, 714, 952, 571, 1000, 381, 1000, 238, 952, 143, 857, 143, 762, 190,
                    667, 238, 619, 333, 571, 619, 476, 714, 429, 762, 381, 810, 286, 810, 143,
                    714, 48, 571, 0, 381, 0, 238, 48, 143, 143
                ),
            ),
        ),
    "%": (
            1143,
            (
                (1000,1000,143,0),
                (
                    381, 1000, 476, 905, 476, 810, 429, 714, 333, 667, 238, 667, 143, 762, 143,
                    857, 190, 952, 286, 1000, 381, 1000, 476, 952, 619, 905, 762, 905, 905, 952,
                    1000, 1000
                ),
                (
                    810, 333, 714, 286, 667, 190, 667, 95, 762, 0, 857, 0, 952, 48, 1000, 143,
                    1000, 238, 905, 333, 810, 333
                ),
            ),
        ),
    "&": (
            1238,
            (
                (
                    1095, 571, 1095, 619, 1048, 667, 1000, 667, 952, 619, 905, 524, 810, 286,
                    714, 143, 619, 48, 524, 0, 333, 0, 238, 48, 190, 95, 143, 190, 143, 286,
                    190, 381, 238, 429, 571, 619, 619, 667, 667, 762, 667, 857, 619, 952, 524,
                    1000, 429, 952, 381, 857, 381, 762, 429, 619, 524, 476, 762, 143, 857, 48,
                    952, 0, 1048, 0, 1095, 48, 1095, 95
                ),
            ),
        ),
    "'": (476,((238,905,190,952,238,1000,286,952,286,857,238,762,190,714),)),
    "(": (
            667,
            (
                (
                    524, 1190, 429, 1095, 333, 952, 238, 762, 190, 524, 190, 333, 238, 95, 333,
                    -95, 429, -238, 524, -333
                ),
            ),
        ),
    ")": (
            667,
            (
                (
                    143, 1190, 238, 1095, 333, 952, 429, 762, 476, 524, 476, 333, 429, 95, 333,
                    -95, 238, -238, 143, -333
                ),
            ),
        ),
    "*": (762,((381,714,381,143),(143,571,619,286),(619,571,143,286))),
    "+": (1238,((619,857,619,0),(190,429,1048,429))),
    ",": (381,((238,190,190,143,143,190,190,238,238,190,238,95,143,0),)),
    "-": (1238,((190,429,1048,429),)),
    ".": (381,((190,238,143,190,190,143,238,190,190,238),)),
    "/": (1048,((952,1190,95,-333),)),
    "0": (
            952,
            (
                (
                    429, 1000, 286, 952, 190, 810, 143, 571, 143, 429, 190, 190, 286, 48, 429,
                    0, 524, 0, 667, 48, 762, 190, 810, 429, 810, 571, 762, 810, 667, 952, 524,
                    1000, 429, 1000
                ),
            ),
        ),
    "1": (952,((286,810,381,857,524,1000,524,0),)),
    "2": (
            952,
            (
                (
                    190, 762, 190, 810, 238, 905, 286, 952, 381, 1000, 571, 1000, 667, 952, 714,
                    905, 762, 810, 762, 714, 714, 619, 619, 476, 143, 0, 810, 0
                ),
            ),
        ),
    "3": (
            952,
            (
                (
                    238, 1000, 762, 1000, 476, 619, 619, 619, 714, 571, 762, 524, 810, 381, 810,
                    286, 762, 143, 667, 48, 524, 0, 381, 0, 238, 48, 190, 95, 143, 190
                ),
            ),
        ),
    "4": (952,((619,1000,143,333,857,333),(619,1000,619,0))),
    "5": (
            952,
            (
                (
                    714, 1000, 238, 1000, 190, 571, 238, 619, 381, 667, 524, 667, 667, 619, 762,
                    524, 810, 381, 810, 286, 762, 143, 667, 48, 524, 0, 381, 0, 238, 48, 190,
                    95, 143, 190
                ),
            ),
        ),
    "6": (
            952,
            (
                (
                    762, 857, 714, 952, 571, 1000, 476, 1000, 333, 952, 238, 810, 190, 571, 190,
                    333, 238, 143, 333, 48, 476, 0, 524, 0, 667, 48, 762, 143, 810, 286, 810,
                    333, 762, 476, 667, 571, 524, 619, 476, 619, 333, 571, 238, 476, 190, 333
                ),
            ),
        ),
    "7": (952,((810,1000,333,0),(143,1000,810,1000))),
    "8": (
            952,
            (
                (
                    381, 1000, 238, 952, 190, 857, 190, 762, 238, 667, 333, 619, 524, 571, 667,
                    524, 762, 429, 810, 333, 810, 190, 762, 95, 714, 48, 571, 0, 381, 0, 238,
                    48, 190, 95, 143, 190, 143, 333, 190, 429, 286, 524, 429, 571, 619, 619,
                    714, 667, 762, 762, 762, 857, 714, 952, 571, 1000, 381, 1000
                ),
            ),
        ),
    "9": (
            952,
            (
                (
                    762, 667, 714, 524, 619, 429, 476, 381, 429, 381, 286, 429, 190, 524, 143,
                    667, 143, 714, 190, 857, 286, 952, 429, 1000, 476, 1000, 619, 952, 714, 857,
                    762, 667, 762, 429, 714, 190, 619, 48, 476, 0, 381, 0, 238, 48, 190, 143
                ),
            ),
        ),
    ":": (
            381,
            (
                (190,571,143,524,190,476,238,524,190,571),
                (190,238,143,190,190,143,238,190,190,238),
            ),
        ),
    ";": (
            381,
            (
                (190,571,143,524,190,476,238,524,190,571),
                (238,190,190,143,143,190,190,238,238,190,238,95,143,0),
            ),
        ),
    "<": (1143,((952,857,190,429,952,0),)),
    "=": (1238,((190,571,1048,571),(190,286,1048,286))),
    ">": (1143,((190,857,952,429,190,0),)),
    "?": (
            857,
            (
                (
                    143, 762, 143, 810, 190, 905, 238, 952, 333, 1000, 524, 1000, 619, 952, 667,
                    905, 714, 810, 714, 714, 667, 619, 619, 571, 429, 476, 429, 333
                ),
                (429,95,381,48,429,0,476,48,429,95),
            ),
        ),
    "@": (
            1286,
            (
                (
                    857, 619, 810, 714, 714, 762, 571, 762, 476, 714, 429, 667, 381, 524, 381,
                    381, 429, 286, 524, 238, 667, 238, 762, 286, 810, 381
                ),
                (571,762,476,667,429,524,429,381,476,286,524,238),
                (
                    857, 762, 810, 381, 810, 286, 905, 238, 1000, 238, 1095, 333, 1143, 476,
                    1143, 571, 1095, 714, 1048, 810, 952, 905, 857, 952, 714, 1000, 571, 1000,
                    429, 952, 333, 905, 238, 810, 190, 714, 143, 571, 143, 429, 190, 286, 238,
                    190, 333, 95, 429, 48, 571, 0, 714, 0, 857, 48, 952, 95, 1000, 143
                ),
                (905,762,857,381,857,286,905,238),
            ),
        ),
    "A": (857,((429,1000,48,0),(429,1000,810,0),(190,333,667,333))),
    "B": (
            1000,
            (
                (190,1000,190,0),
                (190,1000,619,1000,762,952,810,905,857,810,857,714,810,619,762,571,619,524),
                (190,524,619,524,762,476,810,429,857,333,857,190,810,95,762,48,619,0,190,0),
            ),
        ),
    "C": (
            1000,
            (
                (
                    857, 762, 810, 857, 714, 952, 619, 1000, 429, 1000, 333, 952, 238, 857, 190,
                    762, 143, 619, 143, 381, 190, 238, 238, 143, 333, 48, 429, 0, 619, 0, 714,
                    48, 810, 143, 857, 238
                ),
            ),
        ),
    "D": (
            1000,
            (
                (190,1000,190,0),
                (
                    190, 1000, 524, 1000, 667, 952, 762, 857, 810, 762, 857, 619, 857, 381, 810,
                    238, 762, 143, 667, 48, 524, 0, 190, 0
                ),
            ),
        ),
    "E": (905,((190,1000,190,0),(190,1000,810,1000),(190,524,571,524),(190,0,810,0))),
    "F": (857,((190,1000,190,0),(190,1000,810,1000),(190,524,571,524))),
    "G": (
            1000,
            (
                (
                    857, 762, 810, 857, 714, 952, 619, 1000, 429, 1000, 333, 952, 238, 857, 190,
                    762, 143, 619, 143, 381, 190, 238, 238, 143, 333, 48, 429, 0, 619, 0, 714,
                    48, 810, 143, 857, 238, 857, 381
                ),
                (619,381,857,381),
            ),
        ),
    "H": (1048,((190,1000,190,0),(857,1000,857,0),(190,524,857,524))),
    "I": (381,((190,1000,190,0),)),
    "J": (762,((571,1000,571,238,524,95,476,48,381,0,286,0,190,48,143,95,95,238,95,333),)),
    "K": (1000,((190,1000,190,0),(857,1000,190,333),(429,571,857,0))),
    "L": (810,((190,1000,190,0),(190,0,762,0))),
    "M": (1143,((190,1000,190,0),(190,1000,571,0),(952,1000,571,0),(952,1000,952,0))),
    "N": (1048,((190,1000,190,0),(190,1000,857,0),(857,1000,857,0))),
    "O": (
            1048,
            (
                (
                    429, 1000, 333, 952, 238, 857, 190, 762, 143, 619, 143, 381, 190, 238, 238,
                    143, 333, 48, 429, 0, 619, 0, 714, 48, 810, 143, 857, 238, 905, 381, 905,
                    619, 857, 762, 810, 857, 714, 952, 619, 1000, 429, 1000
                ),
            ),
        ),
    "P": (
            1000,
            (
                (190,1000,190,0),
                (
                    190, 1000, 619, 1000, 762, 952, 810, 905, 857, 810, 857, 667, 810, 571, 762,
                    524, 619, 476, 190, 476
                ),
            ),
        ),
    "Q": (
            1048,
            (
                (
                    429, 1000, 333, 952, 238, 857, 190, 762, 143, 619, 143, 381, 190, 238, 238,
                    143, 333, 48, 429, 0, 619, 0, 714, 48, 810, 143, 857, 238, 905, 381, 905,
                    619, 857, 762, 810, 857, 714, 952, 619, 1000, 429, 1000
                ),
                (571,190,857,-95),
            ),
        ),
    "R": (
            1000,
            (
                (190,1000,190,0),
                (
                    190, 1000, 619, 1000, 762, 952, 810, 905, 857, 810, 857, 714, 810, 619, 762,
                    571, 619, 524, 190, 524
                ),
                (524,524,857,0),
            ),
        ),
    "S": (
            952,
            (
                (
                    810, 857, 714, 952, 571, 1000, 381, 1000, 238, 952, 143, 857, 143, 762, 190,
                    667, 238, 619, 333, 571, 619, 476, 714, 429, 762, 381, 810, 286, 810, 143,
                    714, 48, 571, 0, 381, 0, 238, 48, 143, 143
                ),
            ),
        ),
    "T": (762,((381,1000,381,0),(48,1000,714,1000))),
    "U": (1048,((190,1000,190,286,238,143,333,48,476,0,571,0,714,48,810,143,857,286,857,1000),)),
    "V": (857,((48,1000,429,0),(810,1000,429,0))),
    "W": (1143,((95,1000,333,0),(571,1000,333,0),(571,1000,810,0),(1048,1000,810,0))),
    "X": (952,((143,1000,810,0),(810,1000,143,0))),
    "Y": (857,((48,1000,429,524,429,0),(810,1000,429,524))),
    "Z": (952,((810,1000,143,0),(143,1000,810,1000),(143,0,810,0))),
    "[": (667,((190,1190,190,-333),(238,1190,238,-333),(190,1190,524,1190),(190,-333,524,-333))),
    "\\": (667,((0,1000,667,-143),)),
    "]": (667,((429,1190,429,-333),(476,1190,476,-333),(143,1190,476,1190),(143,-333,476,-333))),
    "^": (762,((381,1095,0,429),(381,1095,762,429))),
    "_": (857,((0,-333,857,-333),)),
    "`": (381,((238,762,143,667,143,571,190,524,238,571,190,619,143,571),)),
    "a": (
            905,
            (
                (714,667,714,0),
                (
                    714, 524, 619, 619, 524, 667, 381, 667, 286, 619, 190, 524, 143, 381, 143,
                    286, 190, 143, 286, 48, 381, 0, 524, 0, 619, 48, 714, 143
                ),
            ),
        ),
    "b": (
            905,
            (
                (190,1000,190,0),
                (
                    190, 524, 286, 619, 381, 667, 524, 667, 619, 619, 714, 524, 762, 381, 762,
                    286, 714, 143, 619, 48, 524, 0, 381, 0, 286, 48, 190, 143
                ),
            ),
        ),
    "c": (
            857,
            (
                (
                    714, 524, 619, 619, 524, 667, 381, 667, 286, 619, 190, 524, 143, 381, 143,
                    286, 190, 143, 286, 48, 381, 0, 524, 0, 619, 48, 714, 143
                ),
            ),
        ),
    "d": (
            905,
            (
                (714,1000,714,0),
                (
                    714, 524, 619, 619, 524, 667, 381, 667, 286, 619, 190, 524, 143, 381, 143,
                    286, 190, 143, 286, 48, 381, 0, 524, 0, 619, 48, 714, 143
                ),
            ),
        ),
    "e": (
            857,
            (
                (
                    143, 381, 714, 381, 714, 476, 667, 571, 619, 619, 524, 667, 381, 667, 286,
                    619, 190, 524, 143, 381, 143, 286, 190, 143, 286, 48, 381, 0, 524, 0, 619,
                    48, 714, 143
                ),
            ),
        ),
    "f": (571,((476,1000,381,1000,286,952,238,810,238,0),(95,667,429,667))),
    "g": (
            905,
            (
                (714,667,714,-95,667,-238,619,-286,524,-333,381,-333,286,-286),
                (
                    714, 524, 619, 619, 524, 667, 381, 667, 286, 619, 190, 524, 143, 381, 143,
                    286, 190, 143, 286, 48, 381, 0, 524, 0, 619, 48, 714, 143
                ),
            ),
        ),
    "h": (905,((190,1000,190,0),(190,476,333,619,429,667,571,667,667,619,714,476,714,0))),
    "i": (381,((143,1000,190,952,238,1000,190,1048,143,1000),(190,667,190,0))),
    "j": (
            476,
            (
                (238,1000,286,952,333,1000,286,1048,238,1000),
                (286,667,286,-143,238,-286,143,-333,48,-333),
            ),
        ),
    "k": (810,((190,1000,190,0),(667,667,190,190),(381,381,714,0))),
    "l": (381,((190,1000,190,0),)),
    "m": (
            1429,
            (
                (190,667,190,0),
                (190,476,333,619,429,667,571,667,667,619,714,476,714,0),
                (714,476,857,619,952,667,1095,667,1190,619,1238,476,1238,0),
            ),
        ),
    "n": (905,((190,667,190,0),(190,476,333,619,429,667,571,667,667,619,714,476,714,0))),
    "o": (
            905,
            (
                (
                    381, 667, 286, 619, 190, 524, 143, 381, 143, 286, 190, 143, 286, 48, 381, 0,
                    524, 0, 619, 48, 714, 143, 762, 286, 762, 381, 714, 524, 619, 619, 524, 667,
                    381, 667
                ),
            ),
        ),
    "p": (
            905,
            (
                (190,667,190,-333),
                (
                    190, 524, 286, 619, 381, 667, 524, 667, 619, 619, 714, 524, 762, 381, 762,
                    286, 714, 143, 619, 48, 524, 0, 381, 0, 286, 48, 190, 143
                ),
            ),
        ),
    "q": (
            905,
            (
                (714,667,714,-333),
                (
                    714, 524, 619, 619, 524, 667, 381, 667, 286, 619, 190, 524, 143, 381, 143,
                    286, 190, 143, 286, 48, 381, 0, 524, 0, 619, 48, 714, 143
                ),
            ),
        ),
    "r": (619,((190,667,190,0),(190,381,238,524,333,619,429,667,571,667))),
    "s": (
            810,
            (
                (
                    667, 524, 619, 619, 476, 667, 333, 667, 190, 619, 143, 524, 190, 429, 286,
                    381, 524, 333, 619, 286, 667, 190, 667, 143, 619, 48, 476, 0, 333, 0, 190,
                    48, 143, 143
                ),
            ),
        ),
    "t": (571,((238,1000,238,190,286,48,381,0,476,0),(95,667,429,667))),
    "u": (905,((190,667,190,190,238,48,333,0,476,0,571,48,714,190),(714,667,714,0))),
    "v": (762,((95,667,381,0),(667,667,381,0))),
    "w": (1048,((143,667,333,0),(524,667,333,0),(524,667,714,0),(905,667,714,0))),
    "x": (810,((143,667,667,0),(667,667,143,0))),
    "y": (762,((95,667,381,0),(667,667,381,0,286,-190,190,-286,95,-333,48,-333))),
    "z": (810,((667,667,143,0),(143,667,667,667),(143,0,667,0))),
    "{": (
            667,
            (
                (
                    429, 1190, 333, 1143, 286, 1095, 238, 1000, 238, 905, 286, 810, 333, 762,
                    381, 667, 381, 571, 286, 476
                ),
                (
                    333, 1143, 286, 1048, 286, 952, 333, 857, 381, 810, 429, 714, 429, 619, 381,
                    524, 190, 429, 381, 333, 429, 238, 429, 143, 381, 48, 333, 0, 286, -95, 286,
                    -190, 333, -286
                ),
                (
                    286, 381, 381, 286, 381, 190, 333, 95, 286, 48, 238, -48, 238, -143, 286,
                    -238, 333, -286, 429, -333
                ),
            ),
        ),
    "|": (381,((190,1190,190,-333),)),
    "}": (
            667,
            (
                (
                    238, 1190, 333, 1143, 381, 1095, 429, 1000, 429, 905, 381, 810, 333, 762,
                    286, 667, 286, 571, 381, 476
                ),
                (
                    333, 1143, 381, 1048, 381, 952, 333, 857, 286, 810, 238, 714, 238, 619, 286,
                    524, 476, 429, 286, 333, 238, 238, 238, 143, 286, 48, 333, 0, 381, -95, 381,
                    -190, 333, -286
                ),
                (
                    381, 381, 286, 286, 286, 190, 333, 95, 381, 48, 429, -48, 429, -143, 381,
                    -238, 333, -286, 238, -333
                ),
            ),
        ),
    "~": (
            1143,
            (
                (
                    143, 286, 143, 381, 190, 524, 286, 571, 381, 571, 476, 524, 667, 381, 762,
                    333, 857, 333, 952, 381, 1000, 476
                ),
                (
                    143, 381, 190, 476, 286, 524, 381, 524, 476, 476, 667, 333, 762, 286, 857,
                    286, 952, 333, 1000, 476, 1000, 571
                ),
            ),
        ),
}

_NORM = float(EM)


def charset() -> str:
    """Every character this font can draw, in code-point order."""
    return "".join(sorted(_GLYPHS))


def _entry(ch) -> tuple:
    """Resolve one character to a ``(advance, strokes)`` table entry.

    Unknown characters fall back to :data:`FALLBACK` (or to a blank advance
    when even that is unavailable) so text rendering never raises on a stray
    symbol; :func:`missing` reports them for the caller to surface.
    """
    if ch in _GLYPHS:
        return _GLYPHS[ch]
    if FALLBACK in _GLYPHS:
        return _GLYPHS[FALLBACK]
    return (int(0.6 * EM), ())


def has_glyph(ch) -> bool:
    """True when ``ch`` is drawn from real data (not the fallback)."""
    return str(ch) in _GLYPHS


def missing(text) -> str:
    """The distinct characters of ``text`` that would be drawn as fallback."""
    seen = []
    for ch in str(text):
        if ch not in _GLYPHS and ch not in seen:
            seen.append(ch)
    return "".join(seen)


def _split(stroke):
    a = np.asarray(stroke, dtype=np.float64)
    return a.reshape(-1, 2)


def glyph_polylines(ch, *, cap_height: float = CAP_HEIGHT):
    """Stroke polylines of one glyph as ``(n, 2)`` float arrays.

    ``cap_height`` scales the unit em box (default keeps cap height at 1.0).
    Each array is one continuous pen-down run; join them with round caps.
    """
    _adv, strokes = _entry(str(ch)[0] if str(ch) else " ")
    s = float(cap_height)
    return [(_split(st) / _NORM) * s for st in strokes]


def glyph_advance(ch, *, cap_height: float = CAP_HEIGHT) -> float:
    """Pen advance of one glyph, in the same unit as ``cap_height``."""
    adv, _ = _entry(str(ch)[0] if str(ch) else " ")
    return (float(adv) / _NORM) * float(cap_height)


def text_width(text, *, cap_height: float = CAP_HEIGHT, tracking: float = 0.0) -> float:
    """Advance width of ``text`` (sum of advances plus ``tracking`` per gap).

    ``tracking`` is the extra space between glyphs, in the same unit as
    ``cap_height``; negative values tighten the string.
    """
    text = str(text)
    if not text:
        return 0.0
    total = sum(glyph_advance(ch, cap_height=cap_height) for ch in text)
    return total + float(tracking) * float(cap_height) * max(0, len(text) - 1)


def text_ink_bounds(text, *, cap_height: float = CAP_HEIGHT,
                    stroke_width: float = 0.0, tracking: float = 0.0):
    """Ink box of ``text`` as ``(x0, y0, x1, y1)``, strokes included.

    The ink box is the *outline* of the swept strokes, so it grows by half a
    stroke width on each side compared with the centre-line bounds.  An empty
    string yields a zero-height box at the origin.
    """
    text = str(text)
    s = float(cap_height)
    half = 0.5 * float(stroke_width)
    if not text:
        return (-half, -half, half, half)
    xs0, ys0, xs1, ys1 = [], [], [], []
    pen = 0.0
    for i, ch in enumerate(text):
        adv = glyph_advance(ch, cap_height=s)
        for st in glyph_polylines(ch, cap_height=s):
            xs0.append(float(st[:, 0].min()) + pen)
            xs1.append(float(st[:, 0].max()) + pen)
            ys0.append(float(st[:, 1].min()))
            ys1.append(float(st[:, 1].max()))
        pen += adv + (float(tracking) * s if i + 1 < len(text) else 0.0)
    if not xs0:                      # spaces only: a box as tall as cap height
        return (-half, -half, pen + half, s + half)
    return (min(xs0) - half, min(ys0) - half, max(xs1) + half, max(ys1) + half)


def layout_strokes(text, *, cap_height: float = CAP_HEIGHT, stroke_width: float = 0.0,
                   tracking: float = 0.0, align: str = "left",
                   baseline: float = 0.0):
    """Place ``text`` on an XY plane; returns ``(strokes, ink_box)``.

    ``align`` is ``left`` / ``center`` / ``right`` and acts on the **ink** box
    (strokes included), so a string with a descender stays optically centred:
    ``left`` puts the ink's left edge at x = 0, ``center`` puts its centre at
    x = 0, ``right`` puts its right edge at x = 0.  ``baseline`` shifts the
    result in y.  Units follow ``cap_height`` (pass metres to get metres).
    """
    a = str(align or "left").strip().lower()
    if a not in ("left", "center", "centre", "right"):
        raise ValueError("align must be 'left', 'center' or 'right', got %r" % (align,))
    text = str(text)
    s = float(cap_height)
    strokes, pen = [], 0.0
    for i, ch in enumerate(text):
        for st in glyph_polylines(ch, cap_height=s):
            strokes.append(st + np.array([pen, float(baseline)], dtype=np.float64))
        pen += glyph_advance(ch, cap_height=s)
        if i + 1 < len(text):
            pen += float(tracking) * s
    box = text_ink_bounds(text, cap_height=s, stroke_width=stroke_width,
                          tracking=tracking)
    shift = {"left": -box[0], "center": centre_shift(box),
             "centre": centre_shift(box), "right": -box[2]}[a]
    if strokes:
        strokes = [st + np.array([shift, 0.0], dtype=np.float64) for st in strokes]
    ink = (box[0] + shift, box[1] + float(baseline),
           box[2] + shift, box[3] + float(baseline))
    return strokes, ink


def centre_shift(box) -> float:
    """x shift that centres an ink box on x = 0."""
    return -0.5 * (float(box[0]) + float(box[2]))


def metrics(text, *, cap_height: float = CAP_HEIGHT, tracking: float = 0.0) -> dict:
    """Text metrics for facts/reports (advance, ink box, glyph census)."""
    text = str(text)
    box = text_ink_bounds(text, cap_height=cap_height, tracking=tracking)
    strokes = sum(len(glyph_polylines(ch, cap_height=cap_height)) for ch in text)
    points = sum(len(st) for ch in text for st in glyph_polylines(ch, cap_height=cap_height))
    return {
        "text": text,
        "n_chars": len(text),
        "n_glyphs": sum(1 for ch in text if has_glyph(ch)),
        "n_strokes": int(strokes),
        "n_points": int(points),
        "advance": round(text_width(text, cap_height=cap_height, tracking=tracking), 6),
        "ink_box": tuple(round(float(x), 6) for x in box),
        "missing": missing(text),
        "font": "hershey-simplex",
        "unit_cap_height": float(cap_height),
    }
