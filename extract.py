"""Extract questions, options, marked answers, and figures from the PDF."""
import fitz
import json
import re
from pathlib import Path

PDF_PATH = "surat-ti-drayber.pdf"
FIGURES_DIR = Path("figures")
OUTPUT_JSON = "questions.json"

doc = fitz.open(PDF_PATH)
FIGURES_DIR.mkdir(exist_ok=True)

QUESTION_RE = re.compile(r"^(\d+)\.$")
OPTION_RE = re.compile(r"^([a-d])\.$")
FOOTER_Y_THRESHOLD = 725  # exclude "Page N of 21" footer


def collect_spans(page):
    """Return a flat list of (x0, y0, x1, y1, text) spans, excluding the footer."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "")
                if txt.strip() == "":
                    continue
                x0, y0, x1, y1 = span["bbox"]
                if y0 >= FOOTER_Y_THRESHOLD:
                    continue
                out.append((x0, y0, x1, y1, txt))
    return out


def get_circle_rects(page):
    """Return list of fitz.Rect for each circle-like drawing."""
    rects = []
    for d in page.get_drawings():
        r = d.get("rect")
        if not r:
            continue
        if abs(r.width - r.height) < 3 and 5 < r.width < 25:
            rects.append(r)
    return rects


def save_image(page, xref, out_path):
    """Save image referenced by xref to out_path."""
    pix = fitz.Pixmap(doc, xref)
    if pix.n - pix.alpha >= 4:  # CMYK or other → convert to RGB
        pix = fitz.Pixmap(fitz.csRGB, pix)
    pix.save(str(out_path))


def parse_page(page_index):
    """Return list of dicts: one per question on the page."""
    page = doc[page_index]
    spans = collect_spans(page)
    circles = get_circle_rects(page)

    # Identify positions of all question numbers and option markers
    question_anchors = []  # (qnum_int, y0)
    option_anchors = []    # (letter, y0)

    for x0, y0, x1, y1, text in spans:
        t = text.strip()
        m = QUESTION_RE.match(t)
        if m and x0 < 80:
            question_anchors.append((int(m.group(1)), y0))
            continue
        m = OPTION_RE.match(t)
        if m and x0 < 100:
            option_anchors.append((m.group(1), y0))

    question_anchors.sort(key=lambda x: x[1])
    option_anchors.sort(key=lambda x: x[1])

    # Figure out for each option, which question it belongs to
    questions = []
    for i, (qnum, qy) in enumerate(question_anchors):
        next_y = question_anchors[i + 1][1] if i + 1 < len(question_anchors) else 1e9
        opts_for_q = [(letter, oy) for (letter, oy) in option_anchors if qy < oy < next_y]
        # Question text spans: between qy and first option y (or next_y)
        first_opt_y = opts_for_q[0][1] if opts_for_q else next_y
        q_text_spans = [
            (x0, y0, x1, y1, txt)
            for (x0, y0, x1, y1, txt) in spans
            if qy - 2 <= y0 < first_opt_y and x0 >= 85
        ]
        q_text_spans.sort(key=lambda s: (round(s[1], 1), s[0]))
        q_text = " ".join(s[4].strip() for s in q_text_spans).strip()
        # Collapse multiple spaces
        q_text = re.sub(r"\s+", " ", q_text)

        # For each option, gather text on its line(s) until the next option or next question
        options = {}
        for j, (letter, oy) in enumerate(opts_for_q):
            if j + 1 < len(opts_for_q):
                end_y = opts_for_q[j + 1][1]
            else:
                end_y = next_y
            opt_spans = [
                (x0, y0, x1, y1, txt)
                for (x0, y0, x1, y1, txt) in spans
                if oy <= y0 < end_y and x0 >= 100
            ]
            opt_spans.sort(key=lambda s: (round(s[1], 1), s[0]))
            opt_text = " ".join(s[4].strip() for s in opt_spans).strip()
            opt_text = re.sub(r"\s+", " ", opt_text)
            options[letter] = (opt_text, oy)

        # Find the circle (if any) inside this question's Y range
        answer = None
        q_circles = [c for c in circles if qy < c.y0 < next_y]
        if q_circles:
            # use the first circle (typically only one per question)
            cy = q_circles[0].y0 + q_circles[0].height / 2
            # Pick the option whose Y is closest to circle Y
            best = None
            best_dist = 1e9
            for letter, (txt, oy) in options.items():
                # option center y ~ oy + 8
                dist = abs(oy + 8 - cy)
                if dist < best_dist:
                    best_dist = dist
                    best = letter
            answer = best

        # Find figure (image) whose Y falls in this question's range
        figure_rel_path = ""
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects:
                # image center Y must lie within this question's vertical span
                cy_img = (r.y0 + r.y1) / 2
                if qy < cy_img < next_y:
                    out_name = f"q{qnum:03d}.png"
                    out_path = FIGURES_DIR / out_name
                    if not out_path.exists():
                        save_image(page, xref, out_path)
                    figure_rel_path = f"figures/{out_name}"
                    break
            if figure_rel_path:
                break

        questions.append({
            "number": qnum,
            "question": q_text,
            "options": {letter: txt for letter, (txt, _) in options.items()},
            "answer": answer,
            "figure": figure_rel_path,
        })

    return questions


all_questions = []
for p in range(len(doc)):
    all_questions.extend(parse_page(p))

# Build output: list of records with stable shape
output = []
for q in all_questions:
    options_list = []
    for letter in ["a", "b", "c", "d"]:
        if letter in q["options"]:
            options_list.append({"label": letter, "text": q["options"][letter]})
    output.append({
        "number": q["number"],
        "question": q["question"],
        "options": options_list,
        "answer": q["answer"],
        "figure": q["figure"],
    })

with open(OUTPUT_JSON, "w") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"Wrote {len(output)} questions to {OUTPUT_JSON}")
print(f"Figures saved to {FIGURES_DIR}/")

# Summary
with_fig = sum(1 for q in output if q["figure"])
no_answer = [q["number"] for q in output if q["answer"] is None]
print(f"Questions with figures: {with_fig}")
print(f"Questions without an identified answer: {no_answer}")
