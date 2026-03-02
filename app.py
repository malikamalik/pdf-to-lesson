import os
import io
import base64
import uuid
import json
import zipfile
import hashlib
import xml.etree.ElementTree as etree
import concurrent.futures
from pypdf import PdfReader
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB max for batch uploads

# Vercel has read-only filesystem — use /tmp there
IS_VERCEL = os.environ.get("VERCEL") == "1"
if IS_VERCEL:
    app.config["UPLOAD_FOLDER"] = "/tmp"
else:
    app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf", "pptx", "ppt"}


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File(s) too large. Max total upload is 500MB."}), 413


@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({"error": f"Internal server error: {error}"}), 500


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_ext(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


# ==================== PPTX EXTRACTION (zipfile + lxml — no python-pptx/Pillow) ====================

# XML namespaces used in OOXML (PPTX) files
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
EMU_PER_INCH = 914400


def _pptx_slide_order(zf):
    """Return ordered list of slide paths from presentation.xml."""
    try:
        pres = etree.parse(zf.open("ppt/presentation.xml"))
        # Get slide rIds in order
        sld_ids = pres.findall(".//p:sldIdLst/p:sldId", _NS)
        sld_ids.sort(key=lambda e: int(e.get("id", "0")))
        rids = [e.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                for e in sld_ids]
        # Resolve rIds via presentation.xml.rels
        rels_tree = etree.parse(zf.open("ppt/_rels/presentation.xml.rels"))
        rid_map = {}
        for rel in rels_tree.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
            rid_map[rel.get("Id")] = "ppt/" + rel.get("Target").lstrip("/")
        return [rid_map[r] for r in rids if r in rid_map]
    except Exception:
        # Fallback: find slide files and sort numerically
        slides = [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        import re as _re
        slides.sort(key=lambda s: int(_re.search(r'(\d+)', s).group(1)) if _re.search(r'(\d+)', s) else 0)
        return slides


def _get_slide_texts(slide_tree):
    """Extract all text runs from a slide XML tree, returns (title, all_texts)."""
    texts = []
    title = ""
    for sp in slide_tree.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}sp"):
        # Check if this is a title shape
        ph = sp.find(".//p:nvSpPr/p:nvPr/p:ph", _NS)
        is_title = ph is not None and ph.get("type", "") in ("title", "ctrTitle", "")
        shape_text_parts = []
        for t_elem in sp.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
            if t_elem.text:
                shape_text_parts.append(t_elem.text)
        shape_text = "".join(shape_text_parts).strip()
        if shape_text:
            texts.append(shape_text)
            if is_title and not title:
                title = shape_text
    # Also get text from tables
    for tbl in slide_tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}tbl"):
        for tr in tbl.findall("a:tr", _NS):
            cells = []
            for tc in tr.findall("a:tc", _NS):
                cell_parts = []
                for t_elem in tc.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
                    if t_elem.text:
                        cell_parts.append(t_elem.text)
                cell_text = "".join(cell_parts).strip()
                if cell_text:
                    cells.append(cell_text)
            if cells:
                texts.append("[Table row] " + " | ".join(cells))
    return title, texts


def _get_slide_notes(zf, slide_path):
    """Extract speaker notes for a slide."""
    # Notes are in ppt/notesSlides/notesSlideN.xml, linked via slide rels
    slide_name = slide_path.split("/")[-1]
    rels_path = slide_path.replace("slides/", "slides/_rels/") + ".rels"
    try:
        rels_tree = etree.parse(zf.open(rels_path))
        for rel in rels_tree.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
            if "notesSlide" in rel.get("Type", ""):
                notes_path = "ppt/slides/" + rel.get("Target")
                # Normalize path (handles ../notesSlides/notesSlide1.xml)
                import posixpath
                notes_path = posixpath.normpath(notes_path)
                notes_tree = etree.parse(zf.open(notes_path))
                parts = []
                for t_elem in notes_tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
                    if t_elem.text:
                        parts.append(t_elem.text)
                notes = "".join(parts).strip()
                # Filter out slide number placeholders
                if notes and not notes.isdigit():
                    return notes
    except Exception:
        pass
    return ""


def extract_pptx_text(filepath):
    """Extract text from every slide in a PPTX, preserving slide structure."""
    full_text = []
    with zipfile.ZipFile(filepath, "r") as zf:
        slide_paths = _pptx_slide_order(zf)
        for i, sp in enumerate(slide_paths):
            try:
                slide_tree = etree.parse(zf.open(sp))
            except Exception:
                continue
            title, texts = _get_slide_texts(slide_tree)
            slide_texts = []
            if title:
                slide_texts.append(f"Title: {title}")
            for t in texts:
                if t != title:
                    slide_texts.append(t)
            notes = _get_slide_notes(zf, sp)
            if notes:
                slide_texts.append(f"[Speaker Notes] {notes}")
            if slide_texts:
                full_text.append(f"--- Slide {i + 1} ---\n" + "\n".join(slide_texts))
    return "\n\n".join(full_text)


def _get_slide_image_rels(zf, slide_path):
    """Get rId→media-path map for images referenced by a slide."""
    rels_path = slide_path.replace("slides/", "slides/_rels/") + ".rels"
    rel_map = {}
    try:
        rels_tree = etree.parse(zf.open(rels_path))
        for rel in rels_tree.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
            target = rel.get("Target", "")
            if "/media/" in target or target.startswith("../media/"):
                import posixpath
                full = posixpath.normpath("ppt/slides/" + target)
                rel_map[rel.get("Id")] = full
    except Exception:
        pass
    return rel_map


def _mime_from_ext(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
            "tiff": "image/tiff", "bmp": "image/bmp", "emf": "image/x-emf",
            "wmf": "image/x-wmf"}.get(ext, "image/png")


def extract_pptx_images(filepath, max_images=50):
    """Extract images from PPTX with rich contextual metadata using zipfile+lxml."""
    raw_images = []
    seen_hashes = set()
    all_blobs = []

    with zipfile.ZipFile(filepath, "r") as zf:
        slide_paths = _pptx_slide_order(zf)
        media_cache = {}  # cache media file reads

        for i, sp in enumerate(slide_paths):
            try:
                slide_tree = etree.parse(zf.open(sp))
            except Exception:
                continue

            title, texts = _get_slide_texts(slide_tree)
            if not title and texts:
                for t in texts:
                    if len(t) > 3:
                        title = t[:80]
                        break
            slide_context = " | ".join(texts[:10])
            if len(slide_context) > 400:
                slide_context = slide_context[:400] + "..."

            rel_map = _get_slide_image_rels(zf, sp)

            # Find all picture shapes (p:pic) in the slide
            for pic in slide_tree.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}pic"):
                try:
                    # Get image relationship ID — blipFill is under p: namespace
                    blip = pic.find("p:blipFill/a:blip", _NS)
                    if blip is None:
                        # Fallback: search anywhere under pic
                        blip = pic.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
                    if blip is None:
                        continue
                    rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                    if not rid or rid not in rel_map:
                        continue
                    media_path = rel_map[rid]

                    # Read the image blob
                    if media_path not in media_cache:
                        try:
                            media_cache[media_path] = zf.read(media_path)
                        except KeyError:
                            continue
                    blob = media_cache[media_path]

                    # Get dimensions (EMU) — spPr is under p: namespace, xfrm/ext under a:
                    ext_elem = pic.find("p:spPr/a:xfrm/a:ext", _NS)
                    if ext_elem is None:
                        ext_elem = pic.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}ext")
                    w_inches = h_inches = 0.0
                    if ext_elem is not None:
                        w_inches = round(int(ext_elem.get("cx", 0)) / EMU_PER_INCH, 1)
                        h_inches = round(int(ext_elem.get("cy", 0)) / EMU_PER_INCH, 1)

                    # Get alt text / shape name — cNvPr is under p:nvPicPr/p:cNvPr
                    cNvPr = pic.find("p:nvPicPr/p:cNvPr", _NS)
                    alt_text = cNvPr.get("descr", "") if cNvPr is not None else ""
                    shape_name = cNvPr.get("name", "") if cNvPr is not None else ""

                    all_blobs.append({
                        "blob": blob,
                        "hash": hashlib.md5(blob).hexdigest(),
                        "size": len(blob),
                        "content_type": _mime_from_ext(media_path),
                        "slide": i + 1,
                        "slide_title": title,
                        "slide_context": slide_context,
                        "shape_name": shape_name,
                        "alt_text": alt_text,
                        "width_inches": w_inches,
                        "height_inches": h_inches,
                    })
                except Exception:
                    pass

    # Count repeats
    hash_counts = {}
    for b in all_blobs:
        hash_counts[b["hash"]] = hash_counts.get(b["hash"], 0) + 1

    # Filter and keep meaningful images
    MIN_SIZE = 15_000
    MAX_REPEATS = 3

    for b in all_blobs:
        if len(raw_images) >= max_images:
            break
        if b["size"] < MIN_SIZE:
            continue
        if hash_counts[b["hash"]] > MAX_REPEATS:
            continue
        if b["hash"] in seen_hashes:
            continue
        alt_lower = (b.get("alt_text") or "").lower()
        if any(skip in alt_lower for skip in ["rasterized", "gradient", "background", "/tmp/"]):
            continue
        w_in, h_in = b["width_inches"], b["height_inches"]
        if h_in > 0 and w_in / h_in > 5:
            continue
        seen_hashes.add(b["hash"])

        b64 = base64.b64encode(b["blob"]).decode("utf-8")
        data_uri = f"data:{b['content_type']};base64,{b64}"

        # Classify image type
        sn = b["shape_name"].lower()
        w, h = b["width_inches"], b["height_inches"]
        area = w * h
        if "chart" in sn: img_type = "chart/graph"
        elif "diagram" in sn: img_type = "diagram"
        elif "screenshot" in sn: img_type = "screenshot"
        elif "logo" in sn: img_type = "logo"
        elif "photo" in sn or "picture" in sn: img_type = "photo"
        elif area > 20: img_type = "large illustration/diagram"
        elif area > 8: img_type = "illustration"
        elif w > 1.5 * h + 1: img_type = "banner/wide graphic"
        elif h > 1.5 * w + 1: img_type = "tall graphic/infographic"
        else: img_type = "image"

        desc_parts = [f"From slide {b['slide']}"]
        if b["slide_title"]:
            desc_parts.append(f'titled "{b["slide_title"]}"')
        desc_parts.append(f'[{img_type}, {w}"x{h}"]')
        if b["alt_text"]:
            desc_parts.append(f'Alt text: "{b["alt_text"]}"')
        if b["shape_name"] and b["shape_name"] not in ("Picture", "Image"):
            desc_parts.append(f'Shape: "{b["shape_name"]}"')
        if b["slide_context"]:
            desc_parts.append(f"Context: {b['slide_context'][:150]}")

        raw_images.append({
            "page": b["slide"], "data_uri": data_uri, "desc": " — ".join(desc_parts),
            "source": "pptx", "size": b["size"], "slide_title": b["slide_title"],
            "slide_context": b["slide_context"], "img_type": img_type,
        })

    raw_images.sort(key=lambda x: (x["page"], -x["size"]))
    print(f"  PPTX image extraction: {len(all_blobs)} total → {len(raw_images)} kept")
    for idx, img in enumerate(raw_images):
        print(f"    [{idx}] {img['desc'][:120]}")
    return raw_images


# ==================== PDF EXTRACTION (kept for backwards compat) ====================

def extract_pdf_text(filepath):
    """Extract text from PDF using pypdf."""
    full_text = []
    reader = PdfReader(filepath)
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            full_text.append(f"--- Page {i + 1} ---\n{text}")
    return "\n\n".join(full_text)


def extract_pdf_images(filepath, max_images=30):
    """Extract embedded images from PDF pages using pypdf."""
    images = []
    try:
        reader = PdfReader(filepath)
        for i, page in enumerate(reader.pages):
            if len(images) >= max_images:
                break
            if "/XObject" not in (page.get("/Resources") or {}):
                continue
            xobjects = page["/Resources"]["/XObject"].get_object()
            for obj_name in xobjects:
                if len(images) >= max_images:
                    break
                obj = xobjects[obj_name].get_object()
                if obj.get("/Subtype") == "/Image":
                    try:
                        data = obj.get_data()
                        # Determine image format
                        filters = obj.get("/Filter", "")
                        if isinstance(filters, list):
                            filters = filters[0] if filters else ""
                        if "/DCTDecode" in str(filters):
                            mime = "image/jpeg"
                        elif "/FlateDecode" in str(filters):
                            mime = "image/png"
                        else:
                            mime = "image/png"
                        if len(data) < 5000:
                            continue  # skip tiny images
                        b64 = base64.b64encode(data).decode("utf-8")
                        images.append({
                            "page": i + 1,
                            "data_uri": f"data:{mime};base64,{b64}",
                            "desc": f"Image from page {i + 1}"
                        })
                    except Exception:
                        pass
    except Exception as e:
        print(f"PDF image extraction warning: {e}")
    return images


def process_uploaded_images(files):
    """Process manually uploaded images into base64 data URIs."""
    images = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = get_file_ext(f.filename)
        if ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
            continue
        blob = f.read()
        content_type = f.content_type or f"image/{ext}"
        b64 = base64.b64encode(blob).decode("utf-8")
        images.append({
            "page": 0,
            "data_uri": f"data:{content_type};base64,{b64}",
            "desc": f"Uploaded: {f.filename}",
            "source": "upload"
        })
    return images


# Ask Claude ONLY for structured JSON slides — NOT the whole HTML
SLIDES_SYSTEM_PROMPT = r"""You are a world-class instructional designer who creates visually engaging, easy-to-understand lessons. You transform dense source material into clear, visual, memorable learning experiences — like the best TED talks and Duolingo courses combined.

OUTPUT FORMAT: Return ONLY a valid JSON array. No markdown fences, no explanation, no text before or after the JSON.

## YOUR DESIGN PHILOSOPHY

1. **ONE idea per slide.** Never cram multiple concepts together. If a PPT slide has 5 bullet points covering different sub-topics, split them into 5 separate lesson slides — each one focused, clear, and digestible.
2. **Show, don't tell.** Instead of paragraphs of text, use visual blocks: icons with labels, step-by-step flows, comparison tables, tip callouts. Think of how the original PPT used visuals — replicate that energy.
3. **Break complex ideas into flows.** If something is a process, use "steps". If something has categories, use "icons" with emoji. If something has pros/cons or right/wrong ways, use "compare". If there are key terms, use a "table".
4. **Write like you're explaining to a smart friend.** No jargon dumps. No textbook language. Short sentences. Real-world examples and analogies. If the source says "leverage synergies across verticals", you say "use what works in one area to help another".
5. **Every slide should have a clear takeaway.** The student should finish each slide thinking "I get it" — not "what was that about?"

## SLIDE SCHEMA

Each slide object:
- "cat": category (e.g. "Introduction", "Core Concepts", "Deep Dive", "Knowledge Check", "Interactive Activity", "Milestone", "Common Mistakes", "Quick Reference", "Completion")
- "t": short, punchy title (max 8 words). Use action words: "How X Works", "3 Types of Y", "Why Z Matters"
- "s": one-line subtitle that gives context or previews the takeaway
- "narration": spoken explanation (2-5 sentences). Write as a warm, knowledgeable teacher talking to the student. Explain WHY this matters, give context, use an analogy or example. Never just repeat the slide content — ADD insight.
- "type": "content" | "quiz" | "matching" | "prompt_builder" | "ordering" | "milestone" | "completion"
- "body": see schemas below

## CONTENT BLOCK TYPES (use the right block for the right purpose):

```json
{"kind": "text", "html": "Short explanatory text. Use <strong>bold</strong> for max 2-3 key terms."}
```
→ Use for brief intros or context. MAX 2-3 sentences. Never walls of text.

```json
{"kind": "icons", "items": [{"icon": "🎯", "label": "Focus", "desc": "One clear sentence explaining this point"}]}
```
→ **USE THIS HEAVILY.** Best for breaking down 3-5 key features, benefits, types, or categories. Each icon is a visual anchor. Pick specific, meaningful emoji.

```json
{"kind": "steps", "items": [{"text": "First, do this"}, {"text": "Then do this"}]}
```
→ For any sequential process, workflow, or how-to. Shows numbered progression.

```json
{"kind": "bullets", "items": ["Short point one", "Short point two"]}
```
→ For simple lists. Keep each bullet under 15 words. 3-5 bullets max.

```json
{"kind": "compare", "good_label": "Do This ✅", "good": "Clear example", "bad_label": "Not This ❌", "bad": "Bad example"}
```
→ For right vs wrong, before vs after, old way vs new way. Very effective for learning.

```json
{"kind": "tip", "label": "💡 Pro Tip", "text": "Practical, actionable advice"}
```
→ For memorable takeaways, shortcuts, or expert insights.

```json
{"kind": "table", "headers": ["Term", "Meaning"], "rows": [["API", "A way for apps to talk to each other"]]}
```
→ For definitions, comparisons with multiple dimensions, or structured data.

```json
{"kind": "code", "text": "example code or formula"}
```
→ Only for actual code, formulas, or technical syntax.

```json
{"kind": "heading", "text": "Section Header"}
```
→ To visually separate sub-sections within a slide.

```json
{"kind": "image", "image_idx": 0, "alt": "Descriptive label of what image shows"}
```
→ Place images from the source material in the right context.

## INTERACTIVE TYPES:

**"quiz"** — 4 options, 1 correct. Make questions TEST UNDERSTANDING, not just recall:
```json
{"type": "quiz", "body": {"question": "A company wants to increase user retention. Which approach is MOST effective?", "options": ["Send more emails", "Improve onboarding flow", "Lower prices", "Add more features"], "correct": 1, "explanations": {"correct": "Onboarding is the #1 driver of retention — if users don't understand value quickly, they churn.", "wrong": "While other options can help, improving onboarding has the biggest impact on retention because..."}}}
```

**"matching"** — 5 pairs. Match terms to definitions, causes to effects, etc:
```json
{"type": "matching", "body": {"pairs": [{"left": "Term", "right": "Definition"}, ...]}}
```

**"prompt_builder"** — drag chips to build a response:
```json
{"type": "prompt_builder", "body": {"instructions": "Build a complete project plan by arranging these elements:", "chips": ["Define goals", "Set timeline", "Assign team", "Review risks", "Launch", "Get feedback"], "placeholder": "Drag chips here to build your plan..."}}
```

**"ordering"** — arrange steps in correct sequence:
```json
{"type": "ordering", "body": {"instructions": "Put these steps in the right order:", "correct_order": ["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"]}}
```

**"milestone"** — celebration between sections:
```json
{"type": "milestone", "body": {"emoji": "🎯", "message": "You've mastered the fundamentals!", "lessons_done": 4}}
```

**"completion"** — final slide:
```json
{"type": "completion", "body": {"takeaways": ["Key insight 1", "Key insight 2", "Key insight 3", "Key insight 4"], "cta": "Now go apply what you learned!"}}
```

## CRITICAL RULES:

### Content Quality
1. **TRANSFORM, don't transcribe.** Don't just copy PPT bullet points into lesson bullets. Re-think how to present each idea visually. A PPT bullet list of "features" → becomes an "icons" block with emoji. A PPT text paragraph about a process → becomes a "steps" flow. A PPT slide comparing two things → becomes a "compare" block.
2. **Use "icons" blocks as your primary visual tool.** Whenever you have 3-6 related items (types, features, benefits, principles, categories), present them as icons with emoji + label + short description. This is the #1 way to make content scannable and visual.
3. **Use analogies and examples.** After explaining a concept, add a "tip" block with a real-world analogy or concrete example. "Think of an API like a waiter in a restaurant — it takes your order to the kitchen and brings back your food."
4. **Short text blocks only.** A "text" block should be 1-3 sentences max. If you need more, split across blocks or use a more visual format (icons, steps, table).
5. **Slide titles should be specific and engaging.** Not "Introduction" → but "Why This Changes Everything". Not "Features" → but "5 Features That Set It Apart". Not "Process" → but "How It Works: 4 Simple Steps".

### Structure & Flow
6. Cover ALL content from the source. Do NOT skip or summarize. Every detail must appear.
7. ONE idea per slide. 2-4 content blocks max. If a source slide is dense, break it into 2-3 lesson slides.
8. Never have more than 2-3 content slides without an interactive element (quiz/matching/ordering/prompt_builder).
9. Include at minimum: 5 quizzes, 2 matching games, 1 prompt builder, 1 ordering exercise.
10. Quiz questions should test UNDERSTANDING and APPLICATION, not just recall. "Which approach would you use when..." not "What is the definition of..."
11. Add milestones between major sections. Add compare blocks and tips throughout.
12. End with a review/cheat-sheet slide using a table or icons, then a completion slide.

### Images & Videos
13. Place each image/video in the lesson slide covering the SAME TOPIC as the original source slide.
14. Use {"kind": "image", "image_idx": N, "alt": "SPECIFIC DESCRIPTION"} — not "Image from slide 5".
15. Place images AFTER introductory text so the reader has context.
16. Use every provided image/video at least once.
17. **Videos are special:** When a slide contains a video, the video will auto-play at the bottom of the slide. In the narration for that slide, ALWAYS end with a natural transition to the video, such as: "Let's watch how this works in action", "Take a look at this video to see it in practice", "Let's see a demo of this", "Watch the video below to see the full walkthrough", etc. This makes the narration flow naturally into the video playback.

OUTPUT ONLY THE JSON ARRAY. No other text."""


def generate_slides_json(pdf_text, api_key, course_title=None, images_info=None, slide_text_notes=None):
    """Ask Claude to generate ONLY the slides JSON data."""
    # Don't truncate - send as much as possible (Sonnet has 200K context)
    if len(pdf_text) > 150000:
        pdf_text = pdf_text[:150000] + "\n\n[... Content truncated for context window ...]"

    title_instruction = f'Course Title: "{course_title}". Use this exact name for the course. ' if course_title else "Derive a clear, specific course title from the content (e.g. 'Investor Pitch Deck Masterclass', not 'Interactive Lesson'). "

    # Build image info section with rich context for intelligent placement
    images_section = ""
    if images_info:
        images_section = "\n\nAVAILABLE IMAGES — You MUST use ALL of these. Place each in the lesson slide matching its source topic:\n"
        for i, img in enumerate(images_info):
            images_section += f"  - image_idx {i}: {img['desc']}\n"
        images_section += "\nREMINDER: Match each image to the lesson content that covers the same topic. Write specific alt text describing what the image shows.\n"

    # Build per-slide text notes section
    notes_section = ""
    if slide_text_notes:
        notes_section = "\n\nUSER-ADDED NOTES FOR SPECIFIC SLIDES — incorporate these into the relevant lesson slides:\n"
        for slide_idx in sorted(slide_text_notes.keys()):
            notes_section += f"  - Slide {slide_idx + 1}: {slide_text_notes[slide_idx]}\n"
        notes_section += "\nThese are additional details the user wants included in the lesson. Weave them naturally into the content for the corresponding slide topics.\n"

    user_content = f"""{title_instruction}Transform this source material into an engaging, visual lesson.

APPROACH:
- Study how the original material is structured — its flow, its groupings, its visual hierarchy — and MIRROR that in your lesson design.
- Break dense slides into multiple focused lesson slides. ONE idea per slide.
- Use "icons" blocks with emoji as your PRIMARY visual tool for any list of features, types, benefits, or categories.
- Use "steps" for any process or workflow. Use "compare" for any right/wrong or before/after.
- Write text blocks as 1-3 SHORT sentences max. Use analogies and real-world examples.
- Make titles specific and engaging: "3 Ways to X" not just "X Overview".
- Include ALL content — every detail, every example, every concept. Create as many slides as needed.
{images_section}{notes_section}
SOURCE CONTENT:
{pdf_text}

Return ONLY the JSON array. No markdown, no explanation."""

    # Use streaming to avoid timeout on long generations
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 32000,
        "stream": True,
        "system": SLIDES_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    raw_chunks = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        buffer = ""
        for chunk in iter(lambda: resp.read(4096).decode("utf-8", errors="replace"), ""):
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        evt = json.loads(data_str)
                        if evt.get("type") == "content_block_delta":
                            delta = evt.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                raw_chunks.append(text)
                    except json.JSONDecodeError:
                        pass

    raw = "".join(raw_chunks).strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]  # remove opening fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)

    return json.loads(raw)


def build_html(slides_data, course_title, elevenlabs_key="", elevenlabs_voice="EXAVITQu4vr4xnSDxMaL", images=None):
    """Wrap the slides JSON in the complete, guaranteed-working HTML shell."""

    # Derive title if not provided — use first content slide's title
    if not course_title:
        for s in slides_data:
            if s.get("type") == "content" and s.get("t"):
                course_title = s["t"]
                break
        if not course_title:
            course_title = slides_data[0].get("t", "Lesson") if slides_data else "Lesson"

    # Build the welcome subtitle from first content slide
    welcome_sub = "Master the key concepts through interactive lessons, quizzes, and hands-on activities."
    for s in slides_data:
        if s.get("type") == "content" and s.get("s"):
            welcome_sub = s["s"]
            break

    slides_json = json.dumps(slides_data, ensure_ascii=False)

    # Build images lookup: index -> data_uri
    images_dict = {}
    if images:
        for i, img in enumerate(images):
            images_dict[i] = img["data_uri"]
    images_json = json.dumps(images_dict, ensure_ascii=False)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>{course_title}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --b:#4E83FF;--b06:#4E83FF10;--b12:#4E83FF1f;--b25:#4E83FF40;
  --g:#16A34A;--g08:#16A34A14;--r:#DC2626;--r08:#DC262614;
  --y:#CA8A04;--y08:#CA8A0414;--gold:#F59E0B;
  --nv:#1A1F36;--co:#FF6B35;--co08:#FF6B3514;
  --s0:#FAFBFC;--s1:#F4F5F7;--s2:#E5E7EB;--s3:#D1D5DB;
  --c1:#111827;--c2:#4B5563;--c3:#9CA3AF;--c4:#C9CDD3;
  --rd:12px;
}}
body{{font-family:'Inter',system-ui,sans-serif;background:#fff;color:var(--c1);-webkit-font-smoothing:antialiased;font-weight:400;letter-spacing:-.01em;line-height:1.6;font-size:15px;overflow:hidden;height:100vh;overflow-wrap:break-word;word-wrap:break-word}}

@keyframes up{{from{{opacity:0;transform:translateY(24px)}}to{{opacity:1;transform:translateY(0)}}}}
@keyframes fadeIn{{from{{opacity:0}}to{{opacity:1}}}}
@keyframes slideR{{from{{opacity:0;transform:translateX(-20px)}}to{{opacity:1;transform:translateX(0)}}}}
@keyframes pop{{from{{opacity:0;transform:scale(.9)}}to{{opacity:1;transform:scale(1)}}}}
@keyframes slideDown{{from{{opacity:0;transform:translateY(-10px)}}to{{opacity:1;transform:translateY(0)}}}}
@keyframes xpFloat{{0%{{opacity:1;transform:translateY(0) scale(1)}}60%{{opacity:1;transform:translateY(-40px) scale(1.1)}}100%{{opacity:0;transform:translateY(-70px) scale(.9)}}}}
@keyframes xpPulse{{0%{{transform:scale(1)}}50%{{transform:scale(1.25)}}100%{{transform:scale(1)}}}}
@keyframes shake{{0%,100%{{transform:translateX(0)}}20%,60%{{transform:translateX(-4px)}}40%,80%{{transform:translateX(4px)}}}}
@keyframes glow{{0%,100%{{box-shadow:0 0 0 0 rgba(78,131,255,0)}}50%{{box-shadow:0 0 0 8px rgba(78,131,255,.15)}}}}
@keyframes correctBounce{{0%{{transform:scale(1)}}25%{{transform:scale(1.05)}}50%{{transform:scale(.97)}}75%{{transform:scale(1.02)}}100%{{transform:scale(1)}}}}
@keyframes wrongShake{{0%,100%{{transform:translateX(0)}}10%,50%,90%{{transform:translateX(-6px)}}30%,70%{{transform:translateX(6px)}}}}
@keyframes flashScreen{{0%{{opacity:.3}}100%{{opacity:0}}}}
@keyframes particle{{0%{{opacity:1;transform:translate(0,0) scale(1) rotate(0deg)}}100%{{opacity:0;transform:translate(var(--dx),var(--dy)) scale(0) rotate(var(--dr))}}}}
@keyframes starPop{{0%{{opacity:0;transform:scale(0) rotate(-30deg)}}50%{{opacity:1;transform:scale(1.4) rotate(5deg)}}100%{{opacity:0;transform:scale(.3) rotate(20deg) translateY(-20px)}}}}
@keyframes xpBoom{{0%{{transform:scale(1)}}30%{{transform:scale(1.5)}}60%{{transform:scale(.9)}}100%{{transform:scale(1)}}}}
@keyframes checkDraw{{to{{stroke-dashoffset:0}}}}

.an{{opacity:0}}.an.go{{animation:up .55s cubic-bezier(.16,1,.3,1) both}}
.an2{{opacity:0}}.an2.go{{animation:fadeIn .5s ease both}}
.an3{{opacity:0}}.an3.go{{animation:slideR .5s cubic-bezier(.16,1,.3,1) both}}
.an4{{opacity:0}}.an4.go{{animation:pop .45s cubic-bezier(.16,1,.3,1) both}}
.an5{{opacity:0}}.an5.go{{animation:slideDown .4s cubic-bezier(.16,1,.3,1) both}}

.app{{max-width:600px;margin:0 auto;height:100vh;display:flex;flex-direction:column;position:relative;overflow:hidden}}
.hd{{padding:16px 24px;display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,.9);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);z-index:50;border-bottom:1px solid var(--s2);flex-shrink:0}}
.hd-l{{display:flex;align-items:center;gap:12px}}
.ham{{background:none;border:none;cursor:pointer;padding:6px;border-radius:6px;transition:background .2s;display:flex;align-items:center}}
.ham:hover{{background:var(--s1)}}
.hd-cat{{font-size:13px;font-weight:500;color:var(--b);text-transform:uppercase;letter-spacing:1.5px}}
.hd-r{{display:flex;align-items:center;gap:14px}}
.hd-n{{font-size:12.5px;color:var(--c3)}}

.xp-badge{{display:flex;align-items:center;gap:5px;background:linear-gradient(135deg,#FEF3C7,#FDE68A);border:1px solid #FCD34D;border-radius:20px;padding:4px 12px 4px 8px;font-size:12.5px;color:#92400E;font-weight:500;position:relative;transition:all .3s}}
.xp-badge svg{{flex-shrink:0}}
.coin-icon{{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;flex-shrink:0}}
.xp-toast{{position:absolute;top:-8px;right:-4px;font-size:12px;color:var(--gold);font-weight:500;pointer-events:none;animation:xpFloat 1.2s cubic-bezier(.16,1,.3,1) both;white-space:nowrap}}
.xp-pulse{{animation:xpPulse .4s cubic-bezier(.16,1,.3,1)}}

.bar{{height:2px;background:var(--s1);flex-shrink:0}}
.bar-f{{height:2px;background:var(--b);transition:width .6s cubic-bezier(.16,1,.3,1)}}

.ct{{flex:1;padding:28px 20px 24px;overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch}}
.ct.entering{{animation:slideEnter .4s cubic-bezier(.16,1,.3,1) both}}
@keyframes slideEnter{{from{{opacity:0;transform:translateX(30px)}}to{{opacity:1;transform:translateX(0)}}}}
@keyframes slideEnterBack{{from{{opacity:0;transform:translateX(-30px)}}to{{opacity:1;transform:translateX(0)}}}}
.ct.entering-back{{animation:slideEnterBack .4s cubic-bezier(.16,1,.3,1) both}}
.ct h1{{font-size:24px;font-weight:600;text-align:left;color:var(--c1);letter-spacing:-.3px;line-height:1.25;margin-bottom:6px}}
.ct .sub{{font-size:14px;color:var(--c2);line-height:1.6;margin-bottom:24px;text-align:left}}

.ft{{padding:16px 24px;background:rgba(255,255,255,.9);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);z-index:50;border-top:1px solid var(--s2);display:flex;justify-content:space-between;align-items:center;flex-shrink:0}}
.bk{{background:none;border:none;font-size:14px;font-weight:500;color:var(--b);cursor:pointer;font-family:inherit;padding:8px 0;transition:opacity .2s}}
.bk:disabled{{color:var(--s3);cursor:default}}
.nx{{background:var(--nv);color:#fff;border:none;font-size:14px;font-weight:600;border-radius:12px;padding:12px 28px;cursor:pointer;font-family:inherit;transition:all .25s cubic-bezier(.16,1,.3,1)}}
.nx:hover{{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.12)}}
.nx:disabled{{background:var(--s2);color:var(--c4);cursor:default;transform:none;box-shadow:none}}
.dots{{display:flex;gap:3px;align-items:center}}
.dt{{height:4px;border-radius:2px;transition:all .35s cubic-bezier(.16,1,.3,1);cursor:pointer}}
.dt.on{{width:16px;background:var(--b)}}
.dt.dn{{width:4px;background:var(--b25)}}
.dt.of{{width:4px;background:var(--s2)}}

.ov{{position:fixed;inset:0;background:rgba(0,0,0,.15);z-index:100;opacity:0;pointer-events:none;transition:opacity .25s}}
.ov.open{{opacity:1;pointer-events:auto}}
.dw{{position:fixed;top:0;left:0;bottom:0;width:264px;background:#fff;z-index:101;padding:28px 0;overflow-y:auto;transform:translateX(-100%);transition:transform .35s cubic-bezier(.16,1,.3,1)}}
.dw.open{{transform:translateX(0)}}
.dw-h{{padding:0 24px 20px;font-size:14px;font-weight:500;color:var(--c1)}}
.dw-c{{padding:12px 24px 4px;font-size:11px;font-weight:500;color:var(--c3);text-transform:uppercase;letter-spacing:1.5px}}
.dw-i{{display:flex;align-items:center;gap:8px;width:100%;padding:9px 24px 9px 28px;font-size:13.5px;color:var(--c2);background:transparent;border:none;text-align:left;cursor:pointer;font-family:inherit;transition:all .15s}}
.dw-i:hover{{background:var(--s0);color:var(--c1)}}
.dw-i.on{{color:var(--b);background:var(--b06)}}
.dw-i .dw-ico{{font-size:14px;width:20px;text-align:center}}

.crd{{background:#fff;border-radius:16px;border:1px solid var(--s2);padding:28px;max-width:480px;box-shadow:0 1px 3px rgba(0,0,0,.04);text-align:left;margin:0 auto}}
.ib{{border-radius:10px;padding:16px 20px;font-size:13px;line-height:1.65;text-align:left;max-width:440px;margin:14px auto}}
.ib.bl{{background:var(--b06);color:var(--c2)}}
.ib.gn{{background:var(--g08);color:var(--c2)}}
.ib.yw{{background:var(--y08);color:var(--c2)}}
.g2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:100%}}
.pill{{border:none;border-radius:10px;padding:9px 16px;font-size:12.5px;font-weight:500;cursor:pointer;font-family:inherit;transition:all .2s}}
.pill.on{{background:var(--nv);color:#fff}}
.pill.of{{background:var(--s1);color:var(--c2)}}
.pill.of:hover{{background:var(--s2)}}

.qo{{background:var(--s0);border:1.5px solid var(--s2);border-radius:12px;padding:14px 18px;font-size:13.5px;color:var(--c1);text-align:left;cursor:pointer;font-family:inherit;width:100%;transition:all .25s cubic-bezier(.16,1,.3,1)}}
.qo:hover:not(:disabled){{border-color:var(--b);background:var(--b06)}}
.qo.ok{{background:var(--g08);border-color:var(--g);animation:correctBounce .5s cubic-bezier(.16,1,.3,1)}}
.qo.no{{background:var(--r08);border-color:var(--r);animation:wrongShake .5s ease}}
.xp-reward{{display:inline-flex;align-items:center;gap:4px;background:linear-gradient(135deg,#FEF3C7,#FDE68A);border-radius:20px;padding:3px 10px;font-size:12.5px;color:#92400E;font-weight:500;margin-left:6px}}

.cele-flash{{position:fixed;inset:0;pointer-events:none;z-index:200;animation:flashScreen .6s ease both}}
.cele-flash.green{{background:radial-gradient(circle at center,rgba(22,163,74,.25),transparent 70%)}}
.cele-flash.red{{background:radial-gradient(circle at center,rgba(220,38,38,.2),transparent 70%)}}
.particle{{position:fixed;pointer-events:none;z-index:201;border-radius:50%;animation:particle var(--dur) cubic-bezier(.16,1,.3,1) both}}
.particle.square{{border-radius:2px}}
.star-particle{{position:fixed;pointer-events:none;z-index:201;animation:starPop var(--dur) cubic-bezier(.16,1,.3,1) both}}

.check-circle{{display:inline-block;vertical-align:middle;margin-right:6px}}
.check-circle svg .check-path{{stroke-dasharray:20;stroke-dashoffset:20;animation:checkDraw .4s .2s ease both}}
.check-circle svg .circle-path{{stroke-dasharray:100;stroke-dashoffset:100;animation:checkDraw .5s ease both}}

.fas{{min-width:38px;height:38px;border-radius:50%;border:none;font-weight:500;font-size:13px;cursor:pointer;font-family:inherit;transition:all .3s cubic-bezier(.16,1,.3,1)}}
.fas.dn{{background:var(--g);color:#fff}}
.fas.nw{{background:var(--nv);color:#fff;animation:glow 2s ease infinite}}
.fas.wt{{background:var(--s1);color:var(--c3)}}

.tbb{{display:flex;gap:3px;background:var(--s1);border-radius:10px;padding:3px}}
.tbn{{flex:1;background:transparent;border:none;border-radius:8px;padding:9px 0;font-weight:500;font-size:12px;cursor:pointer;font-family:inherit;transition:all .25s cubic-bezier(.16,1,.3,1);color:var(--c3)}}
.tbn.on{{background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.05);color:var(--c1)}}

.po{{background:#fff;color:var(--c2);border:1.5px solid var(--s2);border-radius:10px;padding:8px 14px;font-size:12px;cursor:pointer;font-family:inherit;transition:all .25s cubic-bezier(.16,1,.3,1)}}
.po:hover{{border-color:var(--b)}}
.po.on{{background:var(--nv);color:#fff;border-color:var(--nv)}}


.listen-badge{{display:flex;align-items:center;gap:5px;background:#F0FDFA;border:1px solid #99F6E4;border-radius:16px;padding:3px 10px 3px 6px;font-size:13px;color:#0D9488;font-weight:500;cursor:pointer;transition:all .2s}}
.listen-badge:hover{{background:#CCFBF1}}
.listen-badge .eq{{display:flex;align-items:flex-end;gap:1.5px;height:10px}}
.listen-badge .eq i{{width:2.5px;background:#0D9488;border-radius:1px;animation:eqBar .8s ease infinite alternate}}
.listen-badge .eq i:nth-child(2){{animation-delay:.2s}}
.listen-badge .eq i:nth-child(3){{animation-delay:.4s}}
@keyframes eqBar{{from{{height:3px}}to{{height:10px}}}}
.listen-badge.off .eq i{{animation:none;height:3px}}
.listen-badge.off{{background:var(--s1);border-color:var(--s2);color:var(--c3)}}

.edit-btn{{background:none;border:1px solid var(--s2);border-radius:8px;padding:3px 8px;font-size:12px;color:var(--c3);cursor:pointer;display:none;align-items:center;gap:4px;transition:all .2s}}
body[data-edit] .edit-btn{{display:flex}}
.dl-btn{{background:none;border:1px solid var(--s2);border-radius:8px;padding:3px 8px;font-size:12px;color:var(--c3);cursor:pointer;display:none;align-items:center;gap:4px;transition:all .2s}}
.dl-btn:hover{{border-color:var(--b);color:var(--b)}}
body[data-edit] .dl-btn{{display:flex}}
.undo-btn{{background:none;border:1px solid var(--s2);border-radius:8px;padding:3px 8px;font-size:12px;color:var(--c3);cursor:pointer;display:none;align-items:center;gap:4px;transition:all .2s;opacity:.35}}
.undo-btn:hover{{border-color:var(--b);color:var(--b)}}
body[data-edit] .undo-btn{{display:flex}}
.edit-btn:hover{{border-color:var(--b);color:var(--b)}}
.edit-panel{{position:fixed;inset:0;z-index:400;display:none}}
.edit-panel.open{{display:flex}}
.edit-ov{{position:absolute;inset:0;background:rgba(0,0,0,.3);backdrop-filter:blur(4px)}}
.edit-drawer{{position:absolute;right:0;top:0;bottom:0;width:min(440px,92vw);background:#fff;box-shadow:-4px 0 24px rgba(0,0,0,.12);overflow-y:auto;animation:editIn .3s ease both;display:flex;flex-direction:column}}
@keyframes editIn{{from{{transform:translateX(100%)}}to{{transform:translateX(0)}}}}
.edit-drawer h3{{font-size:15px;font-weight:600;color:var(--c1);padding:20px 20px 0;margin:0}}
.edit-section{{padding:14px 20px;border-bottom:1px solid var(--s1)}}
.edit-section:last-child{{border-bottom:none}}
.edit-label{{font-size:11px;font-weight:600;color:var(--c3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
.edit-input{{width:100%;padding:8px 12px;border:1px solid var(--s2);border-radius:8px;font-size:13px;font-family:inherit;color:var(--c1);resize:vertical;transition:border-color .2s}}
.edit-input:focus{{outline:none;border-color:var(--b)}}
.edit-img-slot{{width:100%;min-height:60px;border:2px dashed var(--s2);border-radius:10px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .2s;overflow:hidden;position:relative;margin-top:6px}}
.edit-img-slot:hover{{border-color:var(--b);background:var(--b06)}}
.edit-img-slot img{{max-width:100%;max-height:200px;object-fit:contain}}
.edit-img-slot .placeholder{{font-size:12px;color:var(--c3);text-align:center;padding:12px}}
.edit-save{{margin:16px 20px;padding:10px;background:var(--b);color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}}
.edit-save:hover{{opacity:.9}}
.edit-insert-media{{text-align:center;padding:4px 0;opacity:.4;transition:opacity .2s}}
.edit-insert-media:hover{{opacity:1}}
.edit-insert-btn{{background:none;border:1px dashed var(--s2);border-radius:6px;padding:4px 12px;font-size:10px;color:var(--c3);cursor:pointer;font-family:inherit;transition:all .15s;display:inline-flex;align-items:center;gap:4px}}
.edit-insert-btn:hover{{border-color:var(--b);color:var(--b);background:var(--b06)}}
.edit-action-btn{{background:none;border:1px solid var(--s1);border-radius:6px;padding:4px 10px;font-size:11px;color:var(--c2);cursor:pointer;font-family:inherit;transition:all .15s;display:flex;align-items:center;gap:3px}}
.edit-action-btn:hover{{background:var(--b06);border-color:var(--b);color:var(--b)}}
.edit-action-btn:disabled{{opacity:.35;cursor:not-allowed}}
.edit-action-btn:disabled:hover{{background:none;border-color:var(--s1);color:var(--c2)}}
.edit-block{{background:var(--s0);border:1px solid var(--s1);border-radius:10px;padding:12px;margin-bottom:10px}}
.edit-block-kind{{font-size:10px;font-weight:600;color:var(--b);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
.edit-img-actions{{display:flex;gap:6px;margin-top:6px}}
.edit-img-del{{background:none;border:1px solid #ef4444;border-radius:6px;padding:4px 10px;font-size:11px;color:#ef4444;cursor:pointer;font-family:inherit;transition:all .2s}}
.edit-img-del:hover{{background:#ef4444;color:#fff}}
.ai-suggest-wrap{{padding:14px 20px;border-bottom:1px solid var(--s1);background:linear-gradient(135deg,rgba(79,70,229,.04),rgba(168,85,247,.04));display:none}}
body[data-edit] .ai-suggest-wrap{{display:block}}
.ai-suggest-row{{display:flex;gap:6px;margin-top:8px}}
.ai-suggest-input{{flex:1;padding:8px 12px;border:1px solid var(--s2);border-radius:8px;font-size:13px;font-family:inherit;color:var(--c1);resize:none;transition:border-color .2s}}
.ai-suggest-input:focus{{outline:none;border-color:#7c3aed}}
.ai-suggest-btn{{padding:8px 14px;background:linear-gradient(135deg,#7c3aed,#6366f1);color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;white-space:nowrap;transition:opacity .2s;display:flex;align-items:center;gap:5px}}
.ai-suggest-btn:hover{{opacity:.9}}
.ai-suggest-btn:disabled{{opacity:.5;cursor:not-allowed}}
.ai-suggest-hint{{font-size:11px;color:var(--c3);margin-top:6px}}
.ai-suggest-error{{font-size:12px;color:#ef4444;margin-top:6px;display:none}}
.narr-header{{display:flex;align-items:center;justify-content:space-between}}
.narr-regen{{background:none;border:1px solid #7c3aed;border-radius:6px;padding:3px 10px;font-size:11px;color:#7c3aed;cursor:pointer;font-family:inherit;transition:all .2s;display:none;align-items:center;gap:4px}}
body[data-edit] .narr-regen{{display:inline-flex}}
.narr-regen:hover{{background:#7c3aed;color:#fff}}
.narr-regen:disabled{{opacity:.5;cursor:not-allowed}}
.edit-add-img{{display:flex;align-items:center;gap:6px;padding:10px 14px;border:1.5px dashed var(--s2);border-radius:10px;background:none;cursor:pointer;font-size:12px;font-weight:500;color:var(--c3);font-family:inherit;width:100%;transition:all .2s;margin-top:8px;justify-content:center}}
.edit-add-img:hover{{border-color:var(--b);color:var(--b);background:var(--b06)}}

@keyframes modalIn{{from{{opacity:0;transform:scale(.92) translateY(12px)}}to{{opacity:1;transform:scale(1) translateY(0)}}}}
@keyframes modalBgIn{{from{{opacity:0}}to{{opacity:1}}}}
.modal-bg{{position:fixed;inset:0;background:rgba(0,0,0,.25);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);z-index:300;display:flex;align-items:center;justify-content:center;animation:modalBgIn .3s ease both}}
.modal{{background:#fff;border-radius:20px;padding:36px 28px;max-width:360px;width:90%;text-align:center;animation:modalIn .45s cubic-bezier(.16,1,.3,1) both;box-shadow:0 24px 60px rgba(0,0,0,.12)}}
.modal h2{{font-size:18px;font-weight:500;color:var(--c1);margin-bottom:6px}}
.modal p{{font-size:13px;color:var(--c2);line-height:1.6;margin-bottom:24px}}
.modal-btn{{width:100%;border:none;border-radius:12px;padding:14px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;transition:all .25s cubic-bezier(.16,1,.3,1);margin-bottom:10px;display:flex;align-items:center;justify-content:center;gap:10px}}
.modal-btn:hover{{transform:translateY(-1px);box-shadow:0 4px 16px rgba(0,0,0,.1)}}
.modal-btn.primary{{background:var(--nv);color:#fff}}
.modal-btn.secondary{{background:var(--s1);color:var(--c2)}}
.modal-btn .btn-icon{{font-size:18px}}

.img-frame{{border:1px solid var(--s2);border-radius:var(--rd);overflow:hidden;margin:14px 0;background:var(--s0)}}
.img-frame img{{width:100%;display:block}}
.img-frame-label{{padding:8px 14px;font-size:12px;color:var(--c3);border-top:1px solid var(--s1)}}

@media(max-width:480px){{.g2{{grid-template-columns:1fr}}.ct{{padding:24px 18px 20px}}.hd,.ft{{padding:12px 18px}}}}
</style>
</head>
<body data-edit="1">
<div class="app" id="app"></div>
<script>
// ── DATA ──
/*SDATA*/const slidesData={slides_json};/*EDATA*/
/*SIMGS*/const IMAGES={images_json};/*EIMGS*/
const COURSE_TITLE=`{course_title}`;

// ── VIDEO BLOB CACHE ──
const _blobCache={{}};
function mediaSrc(idx){{
  if(idx===undefined||!IMAGES[idx])return'';
  const d=IMAGES[idx];
  if(!d.startsWith('data:video/'))return d;
  if(_blobCache[idx])return _blobCache[idx];
  try{{
    const parts=d.split(',');const mime=parts[0].match(/:(.*?);/)[1];
    const raw=atob(parts[1]);const arr=new Uint8Array(raw.length);
    for(let i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i);
    const url=URL.createObjectURL(new Blob([arr],{{type:mime}}));
    _blobCache[idx]=url;return url;
  }}catch(e){{return d}}
}}
function isVideo(idx){{return idx!==undefined&&IMAGES[idx]&&IMAGES[idx].startsWith('data:video/')}}

// ── SVG CONSTANTS ──
const coinSvg=`<svg width="18" height="18" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="256" cy="256" r="256" fill="none"/><path d="M35.27 78.8c2.85-5.62 5.81-11 11.04-14.75 14.26-10.22 35.08-10.9 52.02-9.29 46.82 4.44 94 23.1 135.53 44.37 24.51 12.57 48.09 26.87 70.56 42.8 5.77 4.11 14.44 10.22 19.8 14.72 3.41 2.98 7.41 5.66 10.79 8.74.79.72 2.94 2.63 3.77 3.11 6.32-2.9 13.85-5.04 20.46-7.02 20.64-6.18 42.63-11.06 64.21-11.49 18.58-.37 42.62 2.35 51.44 21.3.42.91 1.03 2.01 1.58 2.84 1.16 1.63 1.72 3.15 2.72 4.7 2.76 4.32 5.09 8.81 7.67 13.22l14.16 24.5c7.32 12.75 12.81 19.38 9.89 35.2-2.42 13.12-8.08 22.62-14.88 33.76-3.01 4.63-8.02 11.83-11.83 15.75-1.05 1.59-1.97 2.57-3.19 4-9.51 11.12-20.16 21.58-30.91 31.49-2.32 2.1-11.93 10.76-14.06 11.69-1.55 1.46-3.23 2.87-4.93 4.16-10.66 8.1-21.8 17.08-33.01 24.4-2.14 1.31-13.78 9.3-15.06 9.68-12.55 7.9-27.52 16.94-40.76 23.58-1.34.97-14.13 7.46-16.12 8.2-11.28 5.28-22.46 10.62-34.09 15.1-3.17 1.23-6.46 2.43-9.57 3.77-2.5 1.29-14.06 5.12-17.05 6.02-17.53 5.31-29.48 8.78-47.93 11.97-3.89.8-16.46 2.39-20.02 2.08-10.47.66-18.56.24-28.86-1.82-21.21-4.24-25.55-15.37-35.34-32.4l-10.85-18.79c-3.36-5.91-7.09-12.75-10.67-18.43-.44-.98-2.43-4.3-3.13-5.4-3.06-4.87-4.53-10.34-4.59-16.11-.25-22.59 20.09-51.4 35.14-65.9-.63-.87-3.48-2.38-4.51-2.95-1.9-1.37-3.6-2.36-5.59-3.55-5.15-3.13-10.2-6.42-15.14-9.87-1.87-1.51-5.08-3.56-7.11-4.95-3.57-2.44-7.08-4.95-10.54-7.55-4.44-3.31-8.84-6.68-13.2-10.09-2.57-2.01-4.69-3.96-7.43-5.77-3.23-2.52-11.18-9.35-13.84-12.15-2.2-1.68-5.42-4.92-7.49-6.88-7.44-7.06-14.93-14.54-21.53-22.4-1.87-2.24-3.27-3.31-5.05-5.91-2.76-3.4-9.63-11.68-11.5-15.47C7.44 177.87-3.22 154.45 1.48 139.25c2.38-7.67 9.62-18.87 13.92-26.23 6.54-11.19 12.99-23.3 19.87-34.22z" fill="#FECD3E"/><path d="M35.27 78.8c2.85-5.62 5.81-11 11.04-14.75 14.26-10.22 35.08-10.9 52.02-9.29 46.82 4.44 94 23.1 135.53 44.37 24.51 12.57 48.09 26.87 70.56 42.8 5.77 4.11 14.44 10.22 19.8 14.72-.89.84-6.55 3.4-8.01 4.1-5.83 2.81-11.63 5.7-17.37 8.68-1.72.88-7.61 3.9-8.97 4.93l-.32-.05c-10.9 6.24-22.33 12.41-32.99 19.03-13.92 8.67-27.53 17.83-40.82 27.46l-.23.05c-8.65 6.57-17.38 12.94-25.67 19.97-1.25 1.06-3.03 2.68-4.37 3.51l.01.09c-11.52 9.16-25.39 22.71-35.5 33.24-2.78 2.95-5.5 5.96-8.14 9.04-1.43 1.65-5.68 6.97-7.15 7.9-1.89-1.36-3.59-2.36-5.59-3.55-5.15-3.13-10.2-6.42-15.14-9.87-1.87-1.51-5.08-3.56-7.11-4.95-3.57-2.44-7.08-4.95-10.54-7.55-4.44-3.31-8.84-6.68-13.2-10.09-2.57-2.01-4.69-3.96-7.43-5.77-3.23-2.52-11.18-9.35-13.84-12.15-2.2-1.68-5.42-4.92-7.49-6.88-7.44-7.06-14.93-14.54-21.53-22.4-1.87-2.24-3.27-3.31-5.05-5.91-2.76-3.4-9.63-11.68-11.5-15.47C7.44 177.87-3.22 154.45 1.48 139.25c2.38-7.67 9.62-18.87 13.92-26.23 6.54-11.19 12.99-23.3 19.87-34.22z" fill="#FECD3E"/><path d="M16.27 190.03C7.44 177.87-3.22 154.45 1.48 139.25c2.38-7.67 9.62-18.87 13.92-26.22 6.54-11.19 12.99-23.3 19.87-34.23.02.09.05.19.07.28.62 2.66-.23 5.47-.32 8.17-.2 6.4 1.41 13.03 3.54 19.02 16.66 46.69 80.87 96.59 122.47 123.15 5.06 3.23 10.16 6.39 15.32 9.46 2.79 1.66 6.5 3.7 9.14 5.46l.01.09c-11.52 9.16-25.39 22.71-35.5 33.24-2.78 2.95-5.5 5.96-8.14 9.04-1.43 1.65-5.68 6.97-7.15 7.9-1.89-1.36-3.59-2.36-5.59-3.55-5.15-3.13-10.2-6.42-15.14-9.87-1.87-1.51-5.08-3.56-7.11-4.95-3.57-2.44-7.08-4.95-10.54-7.54-4.44-3.32-8.84-6.68-13.2-10.09-2.57-2.01-4.69-3.96-7.43-5.77-3.23-2.52-11.18-9.36-13.84-12.15-2.2-1.68-5.42-4.92-7.49-6.88-7.44-7.06-14.93-14.54-21.53-22.4-1.87-2.24-3.27-3.31-5.05-5.9-2.76-3.4-9.63-11.68-11.5-15.48z" fill="#FEA02C"/><path d="M411.19 183.65c6.6-.63 16.5-.65 22.07 3.63 2.39 1.84 3.5 4.26 3.83 7.21.95 8.69-7.95 21.12-13.25 27.44C377.19 277.48 240.24 357.73 169.56 364.44c-6.71.41-16.38.66-21.82-4.11-2.09-1.83-3.36-4.41-3.54-7.18-.63-9.82 8.8-21.96 15.06-29.12C203.63 273.3 298.49 218.85 361.87 196.04c15.11-5.44 33.23-11.3 49.32-12.39z" fill="#FEA02C"/><path d="M365.18 81.86c1.6-.04 3.19-.11 4.79-.18-.12 5.9.19 12.78-.14 18.48 5.89-.2 12.35-.12 18.28-.16-.24 4.44-.06 10.24-.06 14.75-4.49-.02-14.6.24-18.45-.14.6 5.09.21 12.93.45 18.49-3.06-.14-6.62-.07-9.71-.08l-5.34.08c-.01-4.87-.25-14.01.18-18.57-5.18.71-12.45-.16-18.21.5-.07-5-.09-10.01-.05-15 5.66.15 12.94-.22 18.28.18-.43-4.49-.22-13.32-.24-18.17 3.4.03 6.79-.01 10.18-.11h.04zM34.1 298.96c4.86.14 10.09.03 14.98.02-.21 5.85.22 12.65-.19 18.27 5.69-.12 12.7.26 18.24-.26-.19 3.96-.19 11.19.13 15.04-5.31-.23-13.76.19-18.45-.22.2 1.91.45 17.16.13 18.4-4.45-.33-10.27-.13-14.82-.06-.08-1.3-.03-2.6-.01-3.9.1-4.85-.04-9.71.12-14.56-1.87.3-5.27.22-7.28.22-3.65-.02-7.29.03-10.94.15-.09-4.94-.03-10.13-.04-15.09 4.98.64 13.04.12 18.38.3-.35-3.48-.37-14.57-.28-18.3z" fill="#FEA02C"/></svg>`;
const logoSvg=`<svg width="28" height="28" viewBox="0 0 40 40" fill="none"><rect width="40" height="40" rx="10" fill="#4E83FF"/><path d="M10 28L20 12L30 28" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 22L20 14L25 22" stroke="#fff" stroke-width="1.5" stroke-linecap="round" opacity=".5"/><circle cx="20" cy="10" r="2" fill="#fff"/></svg>`;
const animCheck=`<span class="check-circle"><svg width="20" height="20" viewBox="0 0 24 24" fill="none"><circle class="circle-path" cx="12" cy="12" r="10" stroke="#16A34A" stroke-width="2"/><path class="check-path" d="M8 12.5l2.5 3 5.5-6" stroke="#16A34A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`;

// ── XP ──
let xp=0;
const COLORS=['#4E83FF','#003D5B','#FF6B35','#16A34A','#8B5CF6','#06B6D4','#EC4899'];
function addXP(n){{
  xp+=n;
  const badge=document.getElementById('xp-val');
  const wrap=document.getElementById('xp-wrap');
  if(badge){{badge.textContent=xp;wrap.classList.remove('xp-pulse');void wrap.offsetWidth;wrap.classList.add('xp-pulse')}}
  const toast=document.createElement('div');toast.className='xp-toast';toast.textContent=`+${{n}}`;
  if(wrap){{wrap.appendChild(toast);setTimeout(()=>toast.remove(),1300)}}
}}
function celebrate(originEl){{
  const flash=document.createElement('div');flash.className='cele-flash green';document.body.appendChild(flash);setTimeout(()=>flash.remove(),700);
  let cx=window.innerWidth/2,cy=window.innerHeight/2;
  if(originEl){{const r=originEl.getBoundingClientRect();cx=r.left+r.width/2;cy=r.top+r.height/2}}
  for(let i=0;i<24;i++){{const p=document.createElement('div');p.className='particle'+(Math.random()>.5?' square':'');const size=Math.random()*7+3;const angle=Math.random()*Math.PI*2;const dist=Math.random()*140+60;const dx=Math.cos(angle)*dist;const dy=Math.sin(angle)*dist-40;const dur=Math.random()*.4+.5;p.style.cssText=`left:${{cx}}px;top:${{cy}}px;width:${{size}}px;height:${{size}}px;background:${{COLORS[i%COLORS.length]}};--dx:${{dx}}px;--dy:${{dy}}px;--dr:${{Math.random()*400-200}}deg;--dur:${{dur}}s;`;document.body.appendChild(p);setTimeout(()=>p.remove(),dur*1000+50)}}
  for(let i=0;i<5;i++){{const s=document.createElement('div');s.className='star-particle';const ox=(Math.random()-.5)*120;const oy=(Math.random()-.5)*80-20;const dur=Math.random()*.3+.4;s.style.cssText=`left:${{cx+ox}}px;top:${{cy+oy}}px;--dur:${{dur}}s;`;s.innerHTML=`<svg width="16" height="16" viewBox="0 0 32 32" fill="${{COLORS[(i+3)%COLORS.length]}}"><ellipse cx="16" cy="16" rx="12" ry="10"/></svg>`;document.body.appendChild(s);setTimeout(()=>s.remove(),dur*1000+50)}}
}}
function wrongFlash(){{const flash=document.createElement('div');flash.className='cele-flash red';document.body.appendChild(flash);setTimeout(()=>flash.remove(),600)}}

// ── BUILD SLIDES ARRAY ──
function renderBlock(b){{
  if(!b) return '';
  if(typeof b==='string') return `<div class="an" style="font-size:13.5px;color:var(--c2);line-height:1.7;margin-bottom:12px">${{b}}</div>`;
  const k=b.kind||b.type||'';
  if(k==='text') return `<div class="an" style="font-size:13.5px;color:var(--c2);line-height:1.7;margin-bottom:12px">${{b.html||b.text||b.content||''}}</div>`;
  if(k==='bullets'){{
    const items=(b.items||[]).map(x=>`<li style="margin-bottom:6px">${{x}}</li>`).join('');
    return `<ul class="an" style="font-size:13.5px;color:var(--c2);line-height:1.7;padding-left:20px;margin-bottom:14px">${{items}}</ul>`;
  }}
  if(k==='icons'){{
    const items=(b.items||[]).map(x=>{{
      const label=x.label||x.text||x;
      const desc=x.desc||x.description||'';
      const icon=x.icon||x.emoji||'•';
      return `<div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:10px"><span style="font-size:18px;flex-shrink:0">${{icon}}</span><div><div style="font-size:13.5px;color:var(--c2);line-height:1.6;font-weight:500">${{label}}</div>${{desc?`<div style="font-size:12.5px;color:var(--c3);line-height:1.5;margin-top:2px">${{desc}}</div>`:''}}</div></div>`;
    }}).join('');
    return `<div class="an" style="margin-bottom:14px">${{items}}</div>`;
  }}
  if(k==='steps'){{
    const items=(b.items||[]).map((x,i)=>{{
      const label=x.label||x.text||x;
      return `<div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:12px"><div style="min-width:28px;height:28px;border-radius:50%;background:var(--b);color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0">${{i+1}}</div><div style="font-size:13.5px;color:var(--c2);line-height:1.6;padding-top:4px">${{label}}</div></div>`;
    }}).join('');
    return `<div class="an" style="margin-bottom:14px">${{items}}</div>`;
  }}
  if(k==='tip'||k==='info'){{
    const cls=b.style==='green'?'gn':b.style==='yellow'?'yw':'bl';
    const icon=b.icon||b.emoji||(cls==='gn'?'\\u2705':cls==='yw'?'\\uD83D\\uDCA1':'\\u2139\\uFE0F');
    return `<div class="ib ${{cls}} an">${{icon}} &nbsp;${{b.text||b.content||''}}</div>`;
  }}
  if(k==='table'){{
    const hdr=(b.headers||[]).map(h=>`<th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:500;color:var(--c3);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--s2)">${{h}}</th>`).join('');
    const rows=(b.rows||[]).map(row=>`<tr>${{row.map(cell=>`<td style="padding:10px 14px;font-size:13px;color:var(--c2);border-bottom:1px solid var(--s1)">${{cell}}</td>`).join('')}}</tr>`).join('');
    return `<div class="an" style="overflow-x:auto;margin-bottom:14px"><table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--s2);border-radius:10px;overflow:hidden"><thead><tr>${{hdr}}</tr></thead><tbody>${{rows}}</tbody></table></div>`;
  }}
  if(k==='code'){{
    return `<div class="an" style="margin-bottom:14px"><pre style="background:var(--nv);color:#e2e8f0;border-radius:10px;padding:18px;font-size:12.5px;line-height:1.6;overflow-x:auto;font-family:'Fira Code',monospace">${{b.code||b.text||b.content||''}}</pre></div>`;
  }}
  if(k==='compare'){{
    if(b.good!==undefined||b.bad!==undefined){{
      const gLabel=b.good_label||'Do This';const bLabel=b.bad_label||'Not This';
      return `<div class="g2 an" style="margin-bottom:14px"><div style="background:var(--g08);border-radius:10px;padding:14px 16px;font-size:13px;color:var(--c2);line-height:1.6"><strong>\\u2705 ${{gLabel}}</strong><br>${{b.good||''}}</div><div style="background:var(--r08);border-radius:10px;padding:14px 16px;font-size:13px;color:var(--c2);line-height:1.6"><strong>\\u274C ${{bLabel}}</strong><br>${{b.bad||''}}</div></div>`;
    }}
    const items=(b.items||[]).map(x=>{{
      const bg=x.color==='green'?'var(--g08)':x.color==='red'?'var(--r08)':'var(--b06)';
      const icon=x.icon||x.emoji||(x.color==='green'?'\\u2705':'\\u274C');
      return `<div style="background:${{bg}};border-radius:10px;padding:14px 16px;font-size:13px;color:var(--c2);line-height:1.6"><strong>${{icon}} ${{x.label||''}}</strong><br>${{x.text||x.content||''}}</div>`;
    }}).join('');
    return `<div class="g2 an" style="margin-bottom:14px">${{items}}</div>`;
  }}
  if(k==='image'){{
    const idx=b.image_idx;
    if(idx!==undefined && IMAGES[idx]){{
      const alt=b.alt||b.caption||'';
      const src=mediaSrc(idx);
      if(isVideo(idx)){{
        return `<div class="img-frame an slide-video-wrap"><video src="${{src}}" controls playsinline class="slide-video" style="width:100%;display:block"></video>${{alt?`<div class="img-frame-label">${{alt}}</div>`:''}}</div>`;
      }}
      return `<div class="img-frame an"><img src="${{src}}" alt="${{alt}}" loading="lazy">${{alt?`<div class="img-frame-label">${{alt}}</div>`:''}}</div>`;
    }}
    return '';
  }}
  if(k==='heading') return `<div class="an" style="font-size:16px;font-weight:600;color:var(--c1);margin:18px 0 8px">${{b.text||b.content||''}}</div>`;
  if(k==='divider') return `<hr class="an" style="border:none;border-top:1px solid var(--s2);margin:16px 0">`;
  // fallback: render any text-like content
  if(b.text||b.content) return `<div class="an" style="font-size:13.5px;color:var(--c2);line-height:1.7;margin-bottom:12px">${{b.text||b.content}}</div>`;
  return '';
}}

function buildContentSlide(d){{
  let html='<div style="max-width:100%">';
  const blocks=(d.body&&d.body.blocks)||d.body||[];
  if(Array.isArray(blocks)){{
    // Render non-video blocks first, then video blocks at the end
    const nonVideo=[];const videoBlocks=[];
    blocks.forEach(b=>{{
      const k=b&&(b.kind||b.type||'');
      if(k==='image'&&b.image_idx!==undefined&&isVideo(b.image_idx))videoBlocks.push(b);
      else nonVideo.push(b);
    }});
    nonVideo.forEach(b=>{{ html+=renderBlock(b); }});
    videoBlocks.forEach(b=>{{ html+=renderBlock(b); }});
  }} else if(typeof blocks==='object'){{
    Object.values(blocks).forEach(b=>{{ if(Array.isArray(b))b.forEach(x=>{{html+=renderBlock(x)}}); }});
  }}
  html+='</div>';
  return html;
}}

const S=slidesData.map((d,idx)=>{{
  const obj={{cat:d.cat||'Lesson',t:d.t||'',s:d.s||'',narr:d.narration||''}};
  const tp=d.type||'content';

  if(tp==='content'){{
    obj.r=function(){{return buildContentSlide(d)}};
  }}
  else if(tp==='quiz'){{
    const qid='q'+idx;
    const opts=d.options||(d.body&&d.body.options)||[];
    const ci=d.correct!==undefined?d.correct:(d.body&&d.body.correct!==undefined?d.body.correct:0);
    const q=d.question||(d.body&&d.body.question)||d.t;
    const exObj=d.explanations||(d.body&&d.body.explanations)||null;
    const ex=exObj?(typeof exObj==='string'?exObj:((exObj.correct||'')+(exObj.wrong?' '+exObj.wrong:''))):(d.explanation||(d.body&&d.body.explanation)||'');
    obj.r=function(){{return `<div id="${{qid}}" class="an"></div>`}};
    obj.i=function(){{QZ(qid,q,opts,ci,ex,true)}};
  }}
  else if(tp==='matching'){{
    const mid='m'+idx;
    const pairs=d.pairs||(d.body&&d.body.pairs)||[];
    obj.r=function(){{return `<div id="${{mid}}" class="an"></div>`}};
    obj.i=function(){{MATCH(mid,pairs)}};
  }}
  else if(tp==='ordering'){{
    const oid='o'+idx;
    const items=d.items||(d.body&&d.body.correct_order)||(d.body&&d.body.items)||[];
    obj.r=function(){{return `<div id="${{oid}}" class="an"></div>`}};
    obj.i=function(){{ORDER(oid,items)}};
  }}
  else if(tp==='prompt_builder'){{
    const pbid='pb'+idx;
    const rawParts=d.parts||(d.body&&d.body.parts)||[];
    const parts=(d.body&&d.body.chips)?[{{l:d.body.instructions||'Build your response',o:d.body.chips}}]:rawParts;
    obj.r=function(){{return `<div id="${{pbid}}" class="an"></div>`}};
    obj.i=function(){{PBUILD(pbid,parts)}};
  }}
  else if(tp==='milestone'){{
    const mEmoji=d.emoji||(d.body&&d.body.emoji)||'\\uD83C\\uDF89';
    const mMsg=d.s||(d.body&&d.body.message)||'Great progress! Keep going.';
    obj.r=function(){{
      return `<div style="text-align:center;padding:20px 0">
        <div class="an4" style="font-size:48px;margin-bottom:16px">${{mEmoji}}</div>
        <div class="an" style="font-size:20px;font-weight:600;color:var(--c1);margin-bottom:8px">${{d.t}}</div>
        <div class="an" style="font-size:14px;color:var(--c2);line-height:1.6;max-width:320px;margin:0 auto 20px">${{mMsg}}</div>
        <div class="an" style="display:inline-flex;align-items:center;gap:6px;background:linear-gradient(135deg,#FEF3C7,#FDE68A);border-radius:20px;padding:8px 20px;font-size:14px;color:#92400E;font-weight:500"><span class="coin-icon">${{coinSvg}}</span> ${{xp}} XP earned</div>
      </div>`;
    }};
  }}
  else if(tp==='completion'){{
    const cEmoji=d.emoji||(d.body&&d.body.emoji)||'\\uD83C\\uDF93';
    const cMsg=d.s||(d.body&&d.body.message)||'You have completed the lesson. Well done!';
    obj.r=function(){{
      return `<div style="text-align:center;padding:20px 0">
        <div class="an4" style="font-size:56px;margin-bottom:16px">${{cEmoji}}</div>
        <div class="an" style="font-size:22px;font-weight:600;color:var(--c1);margin-bottom:8px">${{d.t||'Lesson Complete!'}}</div>
        <div class="an" style="font-size:14px;color:var(--c2);line-height:1.6;max-width:340px;margin:0 auto 24px">${{cMsg}}</div>
        <div class="an" style="display:inline-flex;align-items:center;gap:8px;background:linear-gradient(135deg,#FEF3C7,#FDE68A);border:2px solid #FCD34D;border-radius:24px;padding:12px 28px;font-size:18px;color:#92400E;font-weight:600"><span class="coin-icon">${{coinSvg}}</span> ${{xp}} XP</div>
        <div class="an" style="margin-top:20px;font-size:12.5px;color:var(--c3)">Share your achievement or revisit any slide from the menu</div>
      </div>`;
    }};
  }}
  else{{
    obj.r=function(){{return buildContentSlide(d)}};
  }}
  return obj;
}});

// ── STATE ──
let cur=0,prevCur=0;
let listenMode=false,speaking=false,autoTimer=null;

// ── NAVIGATION ──
function go(i){{prevCur=cur;document.querySelectorAll('.slide-video').forEach(v=>{{v.pause()}});cur=Math.max(0,Math.min(S.length-1,i));stopAudio();R();if(listenMode)speakSlide()}}

// ── FOLLOW-ALONG STEPS ──
function FA(id,steps){{const el=document.getElementById(id);if(!el)return;let st=0;
  function r(){{const s=steps[st];el.innerHTML=`<div style="max-width:100%"><div style="display:flex;gap:8px;margin-bottom:20px;overflow-x:auto;padding-bottom:4px">${{steps.map((_,i)=>`<button class="fas ${{i<st?'dn':i===st?'nw':'wt'}}" onclick="window._f${{id}}(${{i}})">${{i<st?'\\u2713':i+1}}</button>`).join('')}}</div><div class="an4" style="background:var(--s0);border:1px solid var(--s2);border-radius:14px;padding:26px 22px"><div style="font-size:13px;font-weight:500;color:var(--b);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px">Step ${{st+1}}</div><div style="font-size:15px;font-weight:500;color:var(--c1);margin-bottom:10px">${{s.t}}</div><div style="font-size:13px;color:var(--c2);line-height:1.65">${{s.d}}</div>${{s.p?`<div class="ib yw" style="margin-top:14px">\\uD83D\\uDCA1 &nbsp;${{s.p}}</div>`:''}}</div><div style="display:flex;justify-content:space-between;margin-top:16px"><button class="bk" onclick="window._f${{id}}(${{st-1}})" ${{st===0?'disabled':''}}>\\u2190 Previous</button><button class="nx" onclick="window._f${{id}}(${{st+1}})" ${{st===steps.length-1?'disabled':''}}>Next Step \\u2192</button></div></div>`;setTimeout(()=>{{const a=el.querySelector('.an4');if(a)a.classList.add('go')}},20)}}
  window['_f'+id]=function(i){{st=Math.max(0,Math.min(steps.length-1,i));r()}};r()}}

// ── PROMPT BUILDER ──
function PBUILD(id,parts){{const el=document.getElementById(id);if(!el)return;
  const pa=parts.map(p=>({{l:p.label||p.l||'Option',o:p.options||p.o||[]}}));
  let se=pa.map(()=>null);
  function r(){{const dn=se.every(s=>s!==null);el.innerHTML=`<div style="max-width:100%">${{pa.map((p,pi)=>`<div style="margin-bottom:18px"><div style="font-size:13px;font-weight:500;color:var(--b);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px">${{p.l}}</div><div style="display:flex;gap:8px;flex-wrap:wrap">${{p.o.map((o,oi)=>`<button class="po${{se[pi]===oi?' on':''}}" onclick="window._pb${{id}}(${{pi}},${{oi}})">${{o}}</button>`).join('')}}</div></div>`).join('')}}${{dn?`<div class="an4 go" style="background:var(--s0);border:1px solid var(--s2);border-radius:10px;padding:18px"><div style="font-size:13px;font-weight:500;color:var(--c3);margin-bottom:6px;text-transform:uppercase;letter-spacing:1.5px">Your prompt</div><div style="font-size:13.5px;color:var(--c1);line-height:1.6;font-style:italic">"${{pa.map((p,i)=>p.o[se[i]]).join(', ')}}"</div></div>`:''}}</div>`}}
  window['_pb'+id]=function(p,o){{se[p]=o;r()}};r()}}

// ── MATCHING ──
function MATCH(id,pairs){{const el=document.getElementById(id);if(!el)return;
  const left=pairs.map((p,i)=>({{idx:i,text:p.left||p[0]||p.term||''}}));
  const right=pairs.map((p,i)=>({{idx:i,text:p.right||p[1]||p.def||''}}));
  // shuffle right side
  const shuffled=[...right].sort(()=>Math.random()-.5);
  let selL=null,matched={{}},wrongPair=null;
  function r(){{
    el.innerHTML=`<div class="crd"><div style="font-size:14px;font-weight:500;color:var(--c1);margin-bottom:16px">Match each item on the left with its pair on the right</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">${{left.map((l,li)=>{{
      const isMatched=matched[l.idx]!==undefined;
      const isSelL=selL===li;
      const isWrongL=wrongPair&&wrongPair[0]===li;
      let cls='qo';if(isMatched)cls+=' ok';else if(isSelL)cls=' qo';else if(isWrongL)cls+=' no';
      let st=isSelL&&!isMatched?'border-color:var(--b);background:var(--b06)':'';
      return `<button class="${{cls}}" style="${{st}}" onclick="window._ml${{id}}(${{li}})" ${{isMatched?'disabled':''}}>${{isMatched?animCheck:''}}${{l.text}}</button>`;
    }}).join('')}}${{shuffled.map((r,ri)=>{{
      const isMatched=Object.values(matched).includes(ri);
      const isWrongR=wrongPair&&wrongPair[1]===ri;
      let cls='qo';if(isMatched)cls+=' ok';else if(isWrongR)cls+=' no';
      return `<button class="${{cls}}" onclick="window._mr${{id}}(${{ri}})" ${{isMatched?'disabled':''}}>${{r.text}}</button>`;
    }}).join('')}}</div>
    ${{Object.keys(matched).length===pairs.length?`<div class="an go" style="margin-top:14px;padding:14px;background:var(--g08);border-radius:10px;font-size:13px;color:var(--c1);text-align:center">${{animCheck}} All matched! <span class="xp-reward"><span class="coin-icon">${{coinSvg}}</span> +20</span></div>`:''}}
    </div>`;
  }}
  window['_ml'+id]=function(li){{if(matched[left[li].idx]!==undefined)return;selL=li;r()}};
  window['_mr'+id]=function(ri){{
    if(selL===null)return;
    if(Object.values(matched).includes(ri))return;
    if(left[selL].idx===shuffled[ri].idx){{
      matched[left[selL].idx]=ri;selL=null;wrongPair=null;r();
      if(Object.keys(matched).length===pairs.length){{addXP(20);setTimeout(()=>celebrate(el),100)}}
    }}else{{
      wrongPair=[selL,ri];wrongFlash();r();setTimeout(()=>{{wrongPair=null;r()}},600);
    }}
    selL=null;
  }};
  r()}}

// ── ORDERING ──
function ORDER(id,items){{const el=document.getElementById(id);if(!el)return;
  const correct=items.map((x,i)=>i);
  let current=[...correct].sort(()=>Math.random()-.5);
  let selIdx=null,done=false;
  function r(){{
    el.innerHTML=`<div class="crd"><div style="font-size:14px;font-weight:500;color:var(--c1);margin-bottom:16px">Put these in the correct order</div>
    <div style="display:flex;flex-direction:column;gap:8px">${{current.map((ci,pos)=>{{
      const isSel=selIdx===pos;
      const item=typeof items[ci]==='string'?items[ci]:(items[ci].text||items[ci].label||items[ci]);
      let cls='qo';if(isSel)cls+=' ';
      let st=isSel?'border-color:var(--b);background:var(--b06)':'';
      if(done){{cls='qo ok';st='';}}
      return `<button class="${{cls}}" style="${{st}}" onclick="window._ord${{id}}(${{pos}})">${{done?animCheck:''}}${{pos+1}}. ${{item}}</button>`;
    }}).join('')}}</div>
    ${{done?`<div class="an go" style="margin-top:14px;padding:14px;background:var(--g08);border-radius:10px;font-size:13px;color:var(--c1);text-align:center">${{animCheck}} Correct order! <span class="xp-reward"><span class="coin-icon">${{coinSvg}}</span> +20</span></div>`:
    `<button class="nx" style="margin-top:14px;width:100%" onclick="window._ordChk${{id}}()">Check Order</button>`}}
    </div>`;
  }}
  window['_ord'+id]=function(pos){{
    if(done)return;
    if(selIdx===null){{selIdx=pos;r()}}
    else{{const tmp=current[selIdx];current[selIdx]=current[pos];current[pos]=tmp;selIdx=null;r()}}
  }};
  window['_ordChk'+id]=function(){{
    const isCorrect=current.every((c,i)=>c===i);
    if(isCorrect){{done=true;addXP(20);r();setTimeout(()=>celebrate(el),100)}}
    else{{wrongFlash();const el2=document.getElementById(id);if(el2)el2.style.animation='wrongShake .5s ease';setTimeout(()=>{{if(el2)el2.style.animation=''}},600)}}
  }};
  r()}}

// ── QUIZ ──
function QZ(id,q,o,ci,ex,withXP){{const el=document.getElementById(id);if(!el)return;let sl=null;
  function r(){{const d=sl!==null;el.innerHTML=`<div class="crd" id="crd-${{id}}"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px"><div style="font-size:14px;color:var(--c1);line-height:1.55;flex:1">${{q}}</div>${{withXP?`<div style="font-size:12px;color:var(--gold);font-weight:500;margin-left:12px;white-space:nowrap"><span class="coin-icon">${{coinSvg}}</span> 20</div>`:''}} </div><div style="display:flex;flex-direction:column;gap:8px">${{o.map((x,i)=>{{let c='qo';if(d&&i===ci)c+=' ok';if(d&&i===sl&&i!==ci)c+=' no';return`<button class="${{c}}" id="qo-${{id}}-${{i}}" onclick="window._q${{id}}(${{i}})" ${{d?'disabled':''}}>${{x}}</button>`}}).join('')}}</div>${{d?`<div class="an go" style="margin-top:14px;padding:14px;background:${{sl===ci?'var(--g08)':'var(--y08)'}};border-radius:10px;font-size:12.5px;color:var(--c1);line-height:1.6">${{sl===ci?`${{animCheck}} Correct! <span class="xp-reward"><span class="coin-icon">${{coinSvg}}</span> +20</span><br>`:'\\u2717 Not quite. '}}${{ex}}</div>`:''}} </div>`}}
  window['_q'+id]=function(i){{if(sl===null){{sl=i;
    if(sl===ci){{if(withXP)addXP(20);r();setTimeout(()=>{{const btn=document.getElementById('qo-'+id+'-'+i);celebrate(btn)}},80)}}
    else{{wrongFlash();r()}}}}}};r()}}

// ── RENDER ──
function R(){{
  const s=S[cur],cats=[...new Set(S.map(x=>x.cat))],pct=((cur+1)/S.length)*100;
  let dots='';for(let i=0;i<S.length;i++)dots+=`<div class="dt ${{i===cur?'on':i<cur?'dn':'of'}}" onclick="go(${{i}})"></div>`;
  let nav='';cats.forEach(c=>{{nav+=`<div class="dw-c">${{c}}</div>`;S.filter(x=>x.cat===c).forEach(x=>{{const idx=S.indexOf(x);const ico=x.t.startsWith('Quick')?'\\u2726':'\\u2022';nav+=`<button class="dw-i${{idx===cur?' on':''}}" onclick="go(${{idx}});cN()"><span class="dw-ico">${{ico}}</span>${{x.t}}</button>`}})}});

  document.getElementById('app').innerHTML=`
    <div class="hd"><div class="hd-l"><button class="ham" onclick="oN()"><svg width="15" height="12" viewBox="0 0 15 12" fill="none"><path d="M1 1h13M1 6h9M1 11h13" stroke="var(--c1)" stroke-width="1.3" stroke-linecap="round"/></svg></button><span class="hd-cat">${{s.cat}}</span></div><div class="hd-r"><button class="undo-btn" id="undo-btn" onclick="doUndo()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 10h10a5 5 0 015 5v2M3 10l4-4M3 10l4 4"/></svg>Undo</button><button class="edit-btn" onclick="openEdit()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>Edit</button><button class="dl-btn" onclick="downloadWithEdits()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>Download</button><div id="listen-toggle" class="${{listenMode?'listen-badge':'listen-badge off'}}" onclick="toggleListen()"><div class="eq"><i></i><i></i><i></i></div><span class="listen-text">${{listenMode?'Listening':'Listen'}}</span></div><div class="xp-badge" id="xp-wrap"><span class="coin-icon">${{coinSvg}}</span><span id="xp-val">${{xp}}</span></div><span class="hd-n">${{cur+1}}/${{S.length}}</span></div></div>
    <div class="bar"><div class="bar-f" style="width:${{pct}}%"></div></div>
    <div class="ov" id="ov" onclick="cN()"></div><div class="dw" id="dw"><div class="dw-h">Lessons</div>${{nav}}</div>
    <div class="ct ${{cur>=prevCur?'entering':'entering-back'}}" id="cn"><h1 class="an">${{s.t}}</h1>${{s.s?`<p class="sub an">${{s.s}}</p>`:'<div style="height:20px"></div>'}}\n${{s.r()}}</div>
    <div class="ft"><button class="bk" onclick="go(${{cur-1}})" ${{cur===0?'disabled':''}}>\\u2190 Back</button><div class="dots">${{dots}}</div><button class="nx" onclick="go(${{cur+1}})" ${{cur===S.length-1?'disabled':''}}>Next \\u2192</button></div>`;

  setTimeout(()=>{{document.querySelectorAll('.an,.an2,.an3,.an4,.an5').forEach((el,i)=>{{setTimeout(()=>el.classList.add('go'),i*70)}})}},30);
  if(s.i)s.i();
  const cn=document.getElementById('cn');if(cn)cn.scrollTop=0;
  // Auto-play videos: if listen mode is off, play video after slide animation
  if(!listenMode){{
    setTimeout(()=>{{
      const vids=document.querySelectorAll('.slide-video');
      vids.forEach(v=>{{
        v.scrollIntoView({{behavior:'smooth',block:'center'}});
        v.muted=true;v.currentTime=0;
        v.play().then(()=>{{v.muted=false}}).catch(()=>{{}});
      }});
    }},800);
  }}
}}
function oN(){{document.getElementById('ov').classList.add('open');document.getElementById('dw').classList.add('open')}}
function cN(){{document.getElementById('ov').classList.remove('open');document.getElementById('dw').classList.remove('open')}}


// ── TTS (ElevenLabs) ──
const EL_KEY='{elevenlabs_key}';
const EL_VOICE='{elevenlabs_voice}';
const EL_MODEL='eleven_turbo_v2_5';
let currentAudio=null,audioCache={{}},audioUnlocked=false;

async function unlockAudio(){{
  if(audioUnlocked)return;
  try{{const s=new Audio('data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA');await s.play();audioUnlocked=true}}catch(e){{}}
}}

async function elFetch(text,idx){{
  if(audioCache[idx])return audioCache[idx];
  if(!EL_KEY)return null;
  try{{
    const r=await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${{EL_VOICE}}/stream`,{{
      method:'POST',
      headers:{{'Content-Type':'application/json','xi-api-key':EL_KEY,'Accept':'audio/mpeg'}},
      body:JSON.stringify({{text,model_id:EL_MODEL,voice_settings:{{stability:0.5,similarity_boost:0.75,use_speaker_boost:true}}}})
    }});
    if(!r.ok)throw new Error(r.status);
    const url=URL.createObjectURL(await r.blob());
    audioCache[idx]=url;return url;
  }}catch(e){{console.warn('ElevenLabs:',e.message);return null}}
}}

function stopAudio(){{
  if(currentAudio){{currentAudio.pause();currentAudio.currentTime=0;currentAudio=null}}
  speaking=false;
  if(autoTimer){{clearTimeout(autoTimer);autoTimer=null}}
}}

function slideHasVideo(idx){{
  const d=slidesData[idx];if(!d)return false;
  const blocks=(d.body&&d.body.blocks)||[];
  if(!Array.isArray(blocks))return false;
  return blocks.some(b=>b&&(b.kind||b.type)==='image'&&b.image_idx!==undefined&&isVideo(b.image_idx));
}}
function slideText(s){{
  let text=s.narr||s.t+'. '+(s.s||'');
  // Auto-append video transition if slide has a video and narration doesn't already mention it
  const idx=S.indexOf(s);
  if(idx>=0&&slideHasVideo(idx)&&!/video|watch|demo|action|look at/i.test(text)){{
    text+=' Now, let\\'s watch the video to see this in action.';
  }}
  return text;
}}

function preCache(from){{
  for(let i=1;i<=3;i++){{const idx=from+i;if(idx<S.length&&!audioCache[idx])elFetch(slideText(S[idx]),idx).catch(()=>{{}})}}
}}

async function speakSlide(){{
  stopAudio();
  if(!listenMode||!EL_KEY)return;
  const myCur=cur,s=S[myCur],text=slideText(s);
  speaking=true;
  const badge=document.getElementById('listen-toggle');
  const setTxt=(t)=>{{if(badge){{const lt=badge.querySelector('.listen-text');if(lt)lt.textContent=t}}}};
  const stale=()=>!listenMode||cur!==myCur;

  let url=audioCache[myCur]||null;
  if(!url){{setTxt('Loading...');url=await elFetch(text,myCur)}}
  if(stale()){{speaking=false;return}}
  if(!url){{setTxt('Error');speaking=false;setTimeout(()=>setTxt(listenMode?'Listening':'Listen'),2000);return}}

  setTxt('Listening');
  const audio=new Audio(url);
  currentAudio=audio;
  audio.onended=()=>{{
    speaking=false;currentAudio=null;
    if(stale())return;
    const interactive=s.t.startsWith('Quick Check')||s.t==='Build a Prompt';
    // If slide has a video, don't auto-advance — play video first
    const hasVideo=document.querySelector('.slide-video');
    if(hasVideo){{
      hasVideo.scrollIntoView({{behavior:'smooth',block:'center'}});
      hasVideo.currentTime=0;
      // Start muted to satisfy autoplay policy, then unmute
      hasVideo.muted=true;
      hasVideo.play().then(()=>{{hasVideo.muted=false}}).catch(()=>{{}});
      hasVideo.onended=()=>{{if(cur===myCur&&listenMode&&cur<S.length-1)autoTimer=setTimeout(()=>go(cur+1),800)}};
      return;
    }}
    if(!interactive&&cur<S.length-1)autoTimer=setTimeout(()=>go(cur+1),800);
  }};
  audio.onerror=()=>{{speaking=false;currentAudio=null;setTxt('Error')}};
  try{{await audio.play();preCache(myCur)}}catch(e){{speaking=false;console.warn('Play blocked:',e)}}
}}

function toggleListen(){{
  listenMode=!listenMode;
  if(listenMode){{unlockAudio();speakSlide()}}else{{stopAudio()}}
  const badge=document.getElementById('listen-toggle');
  if(badge){{badge.className=listenMode?'listen-badge':'listen-badge off';badge.querySelector('.listen-text').textContent=listenMode?'Listening':'Listen'}}
}}

// Pre-cache first slides on load
if(EL_KEY){{for(let i=0;i<Math.min(3,S.length);i++)elFetch(slideText(S[i]),i).catch(()=>{{}})}}

// ── WELCOME MODAL ──
function showWelcome(){{
  const hasVoice=!!EL_KEY;
  const m=document.createElement('div');m.className='modal-bg';m.id='welcome-modal';
  m.innerHTML=`<div class="modal">
    <div style="margin-bottom:20px;font-size:40px">\\uD83D\\uDCDA</div>
    <h2>${{COURSE_TITLE}}</h2>
    <p>${{S[0]&&S[0].s?S[0].s:'Master key concepts through interactive lessons, quizzes, and activities.'}}</p>
    ${{hasVoice?`<button class="modal-btn primary" onclick="startListenMode()"><span class="btn-icon">\\uD83C\\uDFA7</span> Listen Along<span style="font-size:12.5px;color:rgba(255,255,255,.6);margin-left:4px">\\u00B7 auto-play</span></button>`:''}}
    <button class="modal-btn ${{hasVoice?'secondary':'primary'}}" onclick="closeWelcome()"><span class="btn-icon">\\uD83D\\uDCD6</span> Read at My Pace</button>
    <div style="font-size:12px;color:var(--c3);margin-top:6px">${{S.length}} slides \\u00B7 Earn XP along the way</div>
  </div>`;
  document.body.appendChild(m);
}}
function startListenMode(){{listenMode=true;unlockAudio();closeWelcome();speakSlide()}}
function closeWelcome(){{const m=document.getElementById('welcome-modal');if(m){{m.style.opacity='0';m.style.transition='opacity .25s';setTimeout(()=>m.remove(),260)}}}}

// ── UNDO HISTORY ──
const undoStack=[];
const UNDO_MAX=30;
function pushUndo(){{
  undoStack.push({{
    slidesData:JSON.parse(JSON.stringify(slidesData)),
    images:JSON.parse(JSON.stringify(IMAGES)),
    sArr:S.map(s=>({{t:s.t,s:s.s,narr:s.narr,cat:s.cat}}))
  }});
  if(undoStack.length>UNDO_MAX)undoStack.shift();
  updateUndoBtn();
}}
function doUndo(){{
  if(!undoStack.length)return;
  const snap=undoStack.pop();
  // Restore slidesData
  for(let i=0;i<slidesData.length;i++){{
    if(snap.slidesData[i])Object.assign(slidesData[i],snap.slidesData[i]);
  }}
  // Restore IMAGES
  Object.keys(IMAGES).forEach(k=>delete IMAGES[k]);
  Object.assign(IMAGES,snap.images);
  // Restore S array display fields
  snap.sArr.forEach((s,i)=>{{if(S[i]){{S[i].t=s.t;S[i].s=s.s;S[i].narr=s.narr;S[i].cat=s.cat}}}});
  // Rebuild renderers for content slides
  slidesData.forEach((d,i)=>{{if((d.type||'content')==='content')S[i].r=function(){{return buildContentSlide(d)}}}});
  // Clear audio cache
  if(audioCache)Object.keys(audioCache).forEach(k=>delete audioCache[k]);
  R();
  updateUndoBtn();
}}
function updateUndoBtn(){{
  const btn=document.getElementById('undo-btn');
  if(btn)btn.style.opacity=undoStack.length?'1':'0.35';
}}

// ── DOWNLOAD WITH EDITS ──
function downloadWithEdits(){{
  let html='<!DOCTYPE html>\\n'+document.documentElement.outerHTML;
  // Replace slidesData with current edited version
  const sd1=html.indexOf('/*SDATA*/');
  const sd2=html.indexOf('/*EDATA*/');
  if(sd1!==-1&&sd2!==-1){{
    html=html.substring(0,sd1)+'/*SDATA*/const slidesData='+JSON.stringify(slidesData)+';/*EDATA*/'+html.substring(sd2+9);
  }}
  // Replace IMAGES with current version (includes newly added images)
  const im1=html.indexOf('/*SIMGS*/');
  const im2=html.indexOf('/*EIMGS*/');
  if(im1!==-1&&im2!==-1){{
    html=html.substring(0,im1)+'/*SIMGS*/const IMAGES='+JSON.stringify(IMAGES)+';/*EIMGS*/'+html.substring(im2+9);
  }}
  // Strip edit mode so downloaded file is clean
  html=html.replace(' data-edit="1"','');
  // Download
  const blob=new Blob([html],{{type:'text/html'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;
  a.download=(COURSE_TITLE||'lesson').replace(/[^a-zA-Z0-9 ]/g,'').trim().replace(/\\s+/g,'_')+'.html';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

// ── EDIT MODE ──
function openEdit(){{
  const d=slidesData[cur];
  const tp=d.type||'content';
  let blocksHtml='';

  if(tp==='content'){{
    const blocks=(d.body&&d.body.blocks)||d.body||[];
    if(Array.isArray(blocks)){{
      blocks.forEach((b,bi)=>{{
        const k=b.kind||b.type||'text';
        if(k==='text'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Text</div><textarea class="edit-input" rows="3" data-bi="${{bi}}" data-field="html">${{(b.html||b.text||b.content||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}}</textarea></div>`;
        }}else if(k==='bullets'){{
          const items=(b.items||[]).join('\\n');
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Bullet Points</div><textarea class="edit-input" rows="${{Math.max(3,(b.items||[]).length+1)}}" data-bi="${{bi}}" data-field="items" data-type="list">${{items}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One bullet per line</div></div>`;
        }}else if(k==='tip'||k==='info'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">${{k==='tip'?'Tip':'Info'}}</div><textarea class="edit-input" rows="2" data-bi="${{bi}}" data-field="text">${{b.text||b.content||''}}</textarea></div>`;
        }}else if(k==='steps'){{
          const items=(b.items||[]).map(x=>x.label||x.text||x).join('\\n');
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Steps</div><textarea class="edit-input" rows="${{Math.max(3,(b.items||[]).length+1)}}" data-bi="${{bi}}" data-field="items" data-type="steps">${{items}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One step per line</div></div>`;
        }}else if(k==='icons'){{
          const items=(b.items||[]).map(x=>{{const l=x.label||x.text||'';const dd=x.desc||'';const ic=x.icon||'';return ic+'|'+l+'|'+dd}}).join('\\n');
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Icons</div><textarea class="edit-input" rows="${{Math.max(3,(b.items||[]).length+1)}}" data-bi="${{bi}}" data-field="items" data-type="icons">${{items}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">Format: emoji|label|description (one per line)</div></div>`;
        }}else if(k==='compare'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Compare</div><div class="edit-label" style="margin-top:6px">Do This</div><textarea class="edit-input" rows="2" data-bi="${{bi}}" data-field="good">${{b.good||''}}</textarea><div class="edit-label" style="margin-top:8px">Not This</div><textarea class="edit-input" rows="2" data-bi="${{bi}}" data-field="bad">${{b.bad||''}}</textarea></div>`;
        }}else if(k==='code'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Code</div><textarea class="edit-input" rows="3" data-bi="${{bi}}" data-field="text" style="font-family:monospace">${{b.code||b.text||''}}</textarea></div>`;
        }}else if(k==='heading'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Heading</div><input class="edit-input" data-bi="${{bi}}" data-field="text" value="${{(b.text||b.content||'').replace(/"/g,'&quot;')}}"></div>`;
        }}else if(k==='image'){{
          const imgIdx=b.image_idx;
          const hasMedia=imgIdx!==undefined&&IMAGES[imgIdx];
          const vid=isVideo(imgIdx);
          const preview=hasMedia?(vid?`<video src="${{mediaSrc(imgIdx)}}" controls playsinline style="max-width:100%;max-height:200px"></video>`:`<img src="${{mediaSrc(imgIdx)}}">`):
          `<div class="placeholder">Click to upload image or video</div>`;
          blocksHtml+=`<div class="edit-block" id="edit-img-block-${{bi}}"><div class="edit-block-kind">${{vid?'Video':'Image'}}</div><div class="edit-img-slot" id="edit-img-slot-${{bi}}" onclick="this.querySelector('input').click()"><input type="file" accept="image/*,video/mp4,video/webm" style="display:none" onchange="editImgChange(this,${{imgIdx!==undefined?imgIdx:'null'}},${{bi}})">${{preview}}</div><div class="edit-img-actions">${{hasMedia?`<button class="edit-img-del" onclick="editImgDelete(${{bi}},${{imgIdx!==undefined?imgIdx:'null'}})">Delete</button>`:''}}</div><input class="edit-input" style="margin-top:6px" data-bi="${{bi}}" data-field="alt" placeholder="Description" value="${{(b.alt||b.caption||'').replace(/"/g,'&quot;')}}"></div>`;
        }}else if(k==='table'){{
          blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Table (headers)</div><input class="edit-input" data-bi="${{bi}}" data-field="headers" data-type="csv" value="${{(b.headers||[]).join(', ')}}"><div class="edit-label" style="margin-top:8px">Rows (comma-separated, one row per line)</div><textarea class="edit-input" rows="${{Math.max(2,(b.rows||[]).length+1)}}" data-bi="${{bi}}" data-field="rows" data-type="table">${{(b.rows||[]).map(r=>r.join(', ')).join('\\n')}}</textarea></div>`;
        }}
        // Insert media button after each block
        blocksHtml+=`<div class="edit-insert-media"><button class="edit-insert-btn" onclick="insertMediaAt(${{bi+1}})"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" d="M12 5v14m-7-7h14"/></svg> Insert image / video here</button></div>`;
      }});
    }}
  }}else if(tp==='quiz'){{
    const body=d.body||{{}};
    const q=body.question||d.question||'';
    const opts=body.options||d.options||[];
    const ci=body.correct!==undefined?body.correct:(d.correct||0);
    const ex=body.explanations||d.explanations||{{}};
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Question</div><textarea class="edit-input" rows="2" id="eq-q">${{q}}</textarea></div>`;
    opts.forEach((o,i)=>{{
      blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Option ${{i+1}} ${{i===ci?'(correct)':''}}</div><input class="edit-input" id="eq-o${{i}}" value="${{o.replace(/"/g,'&quot;')}}"></div>`;
    }});
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Correct answer (0-${{opts.length-1}})</div><input class="edit-input" type="number" id="eq-ci" value="${{ci}}" min="0" max="${{opts.length-1}}"></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Explanation (correct)</div><textarea class="edit-input" rows="2" id="eq-exc">${{typeof ex==='string'?ex:(ex.correct||'')}}</textarea></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Explanation (wrong)</div><textarea class="edit-input" rows="2" id="eq-exw">${{typeof ex==='object'?(ex.wrong||''):''}}</textarea></div>`;
  }}else if(tp==='matching'){{
    const body=d.body||{{}};
    const pairs=body.pairs||[];
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Matching Pairs</div><textarea class="edit-input" rows="${{Math.max(4,pairs.length+1)}}" id="eq-pairs">${{pairs.map(p=>(p.left||'')+' | '+(p.right||'')).join('\\n')}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One pair per line: left | right</div></div>`;
  }}else if(tp==='prompt_builder'){{
    const body=d.body||{{}};
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Instructions</div><textarea class="edit-input" rows="2" id="eq-pb-instr">${{body.instructions||''}}</textarea></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Chips</div><textarea class="edit-input" rows="${{Math.max(3,(body.chips||[]).length)}}" id="eq-pb-chips">${{(body.chips||[]).join('\\n')}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One chip per line</div></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Placeholder</div><input class="edit-input" id="eq-pb-ph" value="${{(body.placeholder||'').replace(/"/g,'&quot;')}}"></div>`;
  }}else if(tp==='ordering'){{
    const body=d.body||{{}};
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Instructions</div><textarea class="edit-input" rows="2" id="eq-ord-instr">${{body.instructions||''}}</textarea></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Correct Order</div><textarea class="edit-input" rows="${{Math.max(3,(body.correct_order||[]).length)}}" id="eq-ord-items">${{(body.correct_order||[]).join('\\n')}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One step per line (in correct order)</div></div>`;
  }}else if(tp==='milestone'){{
    const body=d.body||{{}};
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Emoji</div><input class="edit-input" id="eq-ms-emoji" value="${{(body.emoji||'').replace(/"/g,'&quot;')}}"></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Message</div><textarea class="edit-input" rows="2" id="eq-ms-msg">${{body.message||''}}</textarea></div>`;
  }}else if(tp==='completion'){{
    const body=d.body||{{}};
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Takeaways</div><textarea class="edit-input" rows="${{Math.max(3,(body.takeaways||[]).length+1)}}" id="eq-comp-ta">${{(body.takeaways||[]).join('\\n')}}</textarea><div style="font-size:10px;color:var(--c3);margin-top:4px">One takeaway per line</div></div>`;
    blocksHtml+=`<div class="edit-block"><div class="edit-block-kind">Call to Action</div><input class="edit-input" id="eq-comp-cta" value="${{(body.cta||'').replace(/"/g,'&quot;')}}"></div>`;
  }}

  const panel=document.createElement('div');
  panel.className='edit-panel open';
  panel.id='edit-panel';
  panel.innerHTML=`<div class="edit-ov" onclick="closeEdit()"></div><div class="edit-drawer">
    <h3>Edit Slide ${{cur+1}} <span style="font-size:11px;font-weight:400;background:var(--c5,#f3f4f6);padding:2px 8px;border-radius:4px;margin-left:6px">${{tp}}</span></h3>
    <div class="edit-slide-actions" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
      <button class="edit-action-btn" onclick="moveSlide(-1)" title="Move up" ${{cur===0?'disabled':''}}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7"/></svg> Up</button>
      <button class="edit-action-btn" onclick="moveSlide(1)" title="Move down" ${{cur>=slidesData.length-1?'disabled':''}}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg> Down</button>
      <button class="edit-action-btn" onclick="duplicateSlide()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Duplicate</button>
      <button class="edit-action-btn" onclick="addSlideAfter()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" d="M12 5v14m-7-7h14"/></svg> Add slide</button>
      <button class="edit-action-btn" onclick="deleteSlide()" style="color:#ef4444;border-color:rgba(239,68,68,.3)" ${{slidesData.length<=1?'disabled':''}}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg> Delete</button>
    </div>
    <div class="ai-suggest-wrap">
      <div class="edit-label" style="display:flex;align-items:center;gap:6px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> AI Suggest</div>
      <div class="ai-suggest-row">
        <textarea class="ai-suggest-input" id="ai-prompt" rows="2" placeholder="e.g. Make this more engaging, simplify the language, add an example..."></textarea>
        <button class="ai-suggest-btn" id="ai-suggest-btn" onclick="aiSuggest()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Apply</button>
      </div>
      <div class="ai-suggest-hint">Describe changes for this slide. AI will update the fields below.</div>
      <div class="ai-suggest-error" id="ai-error"></div>
    </div>
    <div class="edit-section"><div class="edit-label">Title</div><input class="edit-input" id="edit-title" value="${{(d.t||'').replace(/"/g,'&quot;')}}"></div>
    <div class="edit-section"><div class="edit-label">Subtitle</div><input class="edit-input" id="edit-sub" value="${{(d.s||'').replace(/"/g,'&quot;')}}"></div>
    <div class="edit-section"><div class="narr-header"><div class="edit-label">Narration (voice-over text)</div><button class="narr-regen" id="narr-regen-btn" onclick="regenNarration()"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Rewrite with AI</button></div><textarea class="edit-input" id="edit-narr" rows="4">${{d.narration||''}}</textarea></div>
    ${{tp==='content'?`<div class="edit-section"><div class="edit-label">Content Blocks</div>${{blocksHtml}}<button class="edit-add-img" onclick="editAddImage()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg> Add Image / Video</button><input type="file" accept="image/*,video/mp4,video/webm" id="edit-add-img-input" style="display:none" onchange="editAddImageDone(this)"></div>`:(blocksHtml?`<div class="edit-section"><div class="edit-label">Content Blocks</div>${{blocksHtml}}</div>`:'')}}
    <button class="edit-save" onclick="saveEdit()">Save Changes</button>
    <div style="height:20px"></div>
  </div>`;
  document.body.appendChild(panel);
}}

function closeEdit(){{
  const p=document.getElementById('edit-panel');
  if(p){{p.querySelector('.edit-drawer').style.animation='editIn .2s ease reverse both';setTimeout(()=>p.remove(),200)}}
}}

function rebuildAllSlides(){{
  S=slidesData.map((d,i)=>{{
    const tp=d.type||'content';
    const entry={{t:d.t||'',s:d.s||'',narr:d.narration||'',cat:d.cat||''}};
    if(tp==='content')entry.r=function(){{return buildContentSlide(d)}};
    return entry;
  }});
  if(cur>=S.length)cur=S.length-1;
  if(cur<0)cur=0;
  R();
}}

function moveSlide(dir){{
  const target=cur+dir;
  if(target<0||target>=slidesData.length)return;
  pushUndo();
  const tmp=slidesData[cur];
  slidesData[cur]=slidesData[target];
  slidesData[target]=tmp;
  cur=target;
  closeEdit();
  rebuildAllSlides();
  setTimeout(()=>openEdit(),250);
}}

function duplicateSlide(){{
  pushUndo();
  const copy=JSON.parse(JSON.stringify(slidesData[cur]));
  copy.t=(copy.t||'')+' (copy)';
  slidesData.splice(cur+1,0,copy);
  cur=cur+1;
  closeEdit();
  rebuildAllSlides();
  setTimeout(()=>openEdit(),250);
}}

function addSlideAfter(){{
  pushUndo();
  const newSlide={{cat:'Content',t:'New Slide',s:'',narration:'',type:'content',body:{{blocks:[{{kind:'text',html:'Edit this slide content.'}}]}}}};
  slidesData.splice(cur+1,0,newSlide);
  cur=cur+1;
  closeEdit();
  rebuildAllSlides();
  setTimeout(()=>openEdit(),250);
}}

function deleteSlide(){{
  if(slidesData.length<=1)return;
  if(!confirm('Delete slide '+(cur+1)+'?'))return;
  pushUndo();
  slidesData.splice(cur,1);
  if(cur>=slidesData.length)cur=slidesData.length-1;
  closeEdit();
  rebuildAllSlides();
}}

async function callClaude(apiKey,slide,instruction){{
  const sysPrompt='You are an expert instructional designer editing a single lesson slide. You will receive the current slide data as JSON and a user instruction describing what to change. Return ONLY valid JSON with the updated slide. Keep the same structure/schema. Keep the same type and cat unless the user explicitly asks to change them. Write narration as a friendly teacher explaining the content (2-5 sentences). Keep content concise for a mobile screen. Return ONLY the JSON object, no markdown fences, no extra text.';
  const userMsg='Current slide JSON:\\n'+JSON.stringify(slide)+'\\n\\nUser instruction: '+instruction+'\\n\\nReturn the updated slide JSON only.';
  const resp=await fetch('https://api.anthropic.com/v1/messages',{{
    method:'POST',
    headers:{{'Content-Type':'application/json','x-api-key':apiKey,'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'}},
    body:JSON.stringify({{model:'claude-sonnet-4-20250514',max_tokens:4000,system:sysPrompt,messages:[{{role:'user',content:userMsg}}]}})
  }});
  if(!resp.ok){{const e=await resp.text();throw new Error('Claude API error ('+resp.status+'): '+e.slice(0,200))}}
  const result=await resp.json();
  let text='';
  for(const block of (result.content||[])){{if(block.type==='text')text+=block.text||''}}
  text=text.trim();
  if(text.startsWith('```')){{const lines=text.split('\\n');lines.shift();if(lines.length&&lines[lines.length-1].trim().startsWith('```'))lines.pop();text=lines.join('\\n')}}
  return JSON.parse(text);
}}

async function aiSuggest(){{
  const prompt=document.getElementById('ai-prompt');
  const btn=document.getElementById('ai-suggest-btn');
  const errEl=document.getElementById('ai-error');
  const instruction=prompt.value.trim();
  if(!instruction)return;

  // Get API key from localStorage (same key used by main app)
  const apiKey=localStorage.getItem('lf_anthropic_key')||'';
  if(!apiKey){{
    errEl.textContent='No API key found. Set your Anthropic API key in the main app Configure section first.';
    errEl.style.display='block';
    return;
  }}

  // Build current slide snapshot from the edit form
  const d=slidesData[cur];
  const snapshot=JSON.parse(JSON.stringify(d));
  // Override with current form values so AI sees latest edits
  snapshot.t=document.getElementById('edit-title').value;
  snapshot.s=document.getElementById('edit-sub').value;
  snapshot.narration=document.getElementById('edit-narr').value;

  btn.disabled=true;
  btn.innerHTML='<div style="width:12px;height:12px;border:1.5px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite"></div> Thinking...';
  errEl.style.display='none';

  try{{
    const updated=await callClaude(apiKey,snapshot,instruction);

    // Populate form fields with AI suggestions
    if(updated.t!==undefined)document.getElementById('edit-title').value=updated.t;
    if(updated.s!==undefined)document.getElementById('edit-sub').value=updated.s;
    if(updated.narration!==undefined)document.getElementById('edit-narr').value=updated.narration;

    // Update content blocks in the form
    const tp=d.type||'content';
    if(tp==='content'&&updated.body){{
      const newBlocks=(updated.body.blocks)||[];
      const oldBlocks=(d.body&&d.body.blocks)||[];
      // Update existing block fields
      document.querySelectorAll('[data-bi]').forEach(el=>{{
        const bi=parseInt(el.dataset.bi);
        const field=el.dataset.field;
        const dtype=el.dataset.type;
        if(!newBlocks[bi])return;
        const nb=newBlocks[bi];
        if(dtype==='list'&&nb.items){{el.value=nb.items.join('\\n')}}
        else if(dtype==='steps'&&nb.items){{el.value=nb.items.map(x=>x.text||x.label||x).join('\\n')}}
        else if(dtype==='icons'&&nb.items){{el.value=nb.items.map(x=>(x.icon||'')+'|'+(x.label||'')+'|'+(x.desc||'')).join('\\n')}}
        else if(dtype==='csv'&&nb[field]){{el.value=nb[field].join(', ')}}
        else if(dtype==='table'&&nb.rows){{el.value=nb.rows.map(r=>r.join(', ')).join('\\n')}}
        else if(field==='html'){{el.value=nb.html||nb.text||nb.content||''}}
        else if(field==='good'||field==='bad'){{el.value=nb[field]||''}}
        else if(field==='alt'){{el.value=nb.alt||nb.caption||''}}
        else if(field==='text'){{el.value=nb.text||nb.content||nb.code||''}}
        else if(nb[field]!==undefined){{el.value=nb[field]}}
      }});
      // Store new blocks so saveEdit picks them up
      pushUndo();
      if(d.body)d.body.blocks=newBlocks;
    }}else if(tp==='quiz'&&updated.body){{
      const b=updated.body;
      const qEl=document.getElementById('eq-q');if(qEl&&b.question)qEl.value=b.question;
      (b.options||[]).forEach((o,i)=>{{const el=document.getElementById('eq-o'+i);if(el)el.value=o}});
      const ciEl=document.getElementById('eq-ci');if(ciEl&&b.correct!==undefined)ciEl.value=b.correct;
      if(b.explanations){{
        const excEl=document.getElementById('eq-exc');if(excEl)excEl.value=b.explanations.correct||'';
        const exwEl=document.getElementById('eq-exw');if(exwEl)exwEl.value=b.explanations.wrong||'';
      }}
    }}

    // Flash success
    btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" d="M5 13l4 4L19 7"/></svg> Applied!';
    btn.style.background='#16a34a';
    setTimeout(()=>{{btn.disabled=false;btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Apply';btn.style.background=''}},2000);
  }}catch(e){{
    errEl.textContent=e.message;
    errEl.style.display='block';
    btn.disabled=false;
    btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Apply';
  }}
}}

async function regenNarration(){{
  const btn=document.getElementById('narr-regen-btn');
  const narrEl=document.getElementById('edit-narr');
  const apiKey=localStorage.getItem('lf_anthropic_key')||'';
  if(!apiKey){{
    alert('No API key found. Set your Anthropic API key in the main app Configure section first.');
    return;
  }}

  // Build a snapshot with current form values
  const d=slidesData[cur];
  const snapshot=JSON.parse(JSON.stringify(d));
  snapshot.t=document.getElementById('edit-title').value;
  snapshot.s=document.getElementById('edit-sub').value;
  // Read current block edits from the form
  const tp=d.type||'content';
  if(tp==='content'){{
    const blocks=(snapshot.body&&snapshot.body.blocks)||[];
    document.querySelectorAll('[data-bi]').forEach(el=>{{
      const bi=parseInt(el.dataset.bi);
      const field=el.dataset.field;
      const dtype=el.dataset.type;
      if(!blocks[bi])return;
      if(dtype==='list')blocks[bi].items=el.value.split('\\n').filter(x=>x.trim());
      else if(field==='html'){{blocks[bi].html=el.value;blocks[bi].text=el.value}}
      else if(field==='text')blocks[bi].text=el.value;
      else if(field!=='alt')blocks[bi][field]=el.value;
    }});
  }}else if(tp==='quiz'){{
    const body=snapshot.body||{{}};
    const qEl=document.getElementById('eq-q');if(qEl)body.question=qEl.value;
    const opts=[];for(let i=0;i<4;i++){{const el=document.getElementById('eq-o'+i);if(el)opts.push(el.value)}}
    if(opts.length)body.options=opts;
    snapshot.body=body;
  }}
  delete snapshot.narration;

  btn.disabled=true;
  btn.innerHTML='<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin .7s linear infinite"><path d="M12 2L2 7l10 5 10-5-10-5z"/></svg> Rewriting...';

  try{{
    const updated=await callClaude(apiKey,snapshot,'Rewrite ONLY the narration field for this slide. Write 2-5 sentences as a friendly teacher explaining the current content of this slide. Keep the narration natural and conversational. Return the full slide JSON with the updated narration.');
    if(updated&&updated.narration){{
      narrEl.value=updated.narration;
      btn.innerHTML='<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" d="M5 13l4 4L19 7"/></svg> Done!';
      btn.style.borderColor='#16a34a';btn.style.color='#16a34a';
      setTimeout(()=>{{btn.disabled=false;btn.innerHTML='<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Rewrite with AI';btn.style.borderColor='';btn.style.color=''}},2000);
    }}else{{throw new Error('No narration returned')}}
  }}catch(e){{
    alert('Failed: '+e.message);
    btn.disabled=false;
    btn.innerHTML='<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> Rewrite with AI';
  }}
}}

function editImgChange(input,imgIdx,bi){{
  if(!input.files||!input.files[0])return;
  const file=input.files[0];
  const reader=new FileReader();
  reader.onload=function(e){{
    const dataUri=e.target.result;
    // Store new media
    if(imgIdx===null||imgIdx===undefined){{imgIdx=Object.keys(IMAGES).length;while(IMAGES[imgIdx])imgIdx++}}
    IMAGES[imgIdx]=dataUri;
    // Update block
    const d=slidesData[cur];
    const blocks=(d.body&&d.body.blocks)||[];
    if(blocks[bi]){{blocks[bi].image_idx=imgIdx}}
    // Invalidate blob cache for this index
    if(_blobCache[imgIdx]){{URL.revokeObjectURL(_blobCache[imgIdx]);delete _blobCache[imgIdx]}}
    // Update preview
    const slot=input.parentElement;
    const src=mediaSrc(imgIdx);
    const vid=dataUri.startsWith('data:video/');
    const mediaEl=vid?`<video src="${{src}}" controls playsinline style="max-width:100%;max-height:200px"></video>`:`<img src="${{src}}">`;
    slot.innerHTML=`<input type="file" accept="image/*,video/mp4,video/webm" style="display:none" onchange="editImgChange(this,${{imgIdx}},${{bi}})">${{mediaEl}}`;
    // If video was added/replaced, update narration and clear audio cache
    if(vid){{
      const d2=slidesData[cur];
      const narr=d2.narration||'';
      if(!/video|watch|demo|action|look at/i.test(narr)){{
        d2.narration=(narr.trim()?narr.trim()+' ':'')+'Now, let\\'s watch the video to see this in action.';
        S[cur].narr=d2.narration;
      }}
      if(audioCache)delete audioCache[cur];
    }}
  }};
  reader.readAsDataURL(file);
}}

function editImgDelete(bi,imgIdx){{
  pushUndo();
  const d=slidesData[cur];
  const blocks=(d.body&&d.body.blocks)||[];
  if(blocks[bi]){{delete blocks[bi].image_idx}}
  if(imgIdx!==null&&imgIdx!==undefined){{
    if(_blobCache[imgIdx]){{URL.revokeObjectURL(_blobCache[imgIdx]);delete _blobCache[imgIdx]}}
    delete IMAGES[imgIdx];
  }}
  // Update UI
  const slot=document.getElementById('edit-img-slot-'+bi);
  if(slot){{slot.innerHTML=`<input type="file" accept="image/*,video/mp4,video/webm" style="display:none" onchange="editImgChange(this,null,${{bi}})"><div class="placeholder">Click to upload image or video</div>`}}
  const actions=slot&&slot.nextElementSibling;
  if(actions)actions.innerHTML='';
}}

function editAddImage(){{
  const inp=document.getElementById('edit-add-img-input');
  inp.dataset.insertAt='';
  inp.click();
}}

function insertMediaAt(pos){{
  const inp=document.getElementById('edit-add-img-input');
  inp.dataset.insertAt=pos;
  inp.click();
}}

function editAddImageDone(input){{
  if(!input.files||!input.files[0])return;
  const insertPos=input.dataset.insertAt;
  const file=input.files[0];
  const reader=new FileReader();
  reader.onload=function(e){{
    pushUndo();
    const dataUri=e.target.result;
    let imgIdx=0;
    while(IMAGES[imgIdx])imgIdx++;
    IMAGES[imgIdx]=dataUri;
    const d=slidesData[cur];
    if(!d.body)d.body={{}};
    if(!d.body.blocks)d.body.blocks=[];
    const newBlock={{kind:'image',image_idx:imgIdx,alt:''}};
    if(insertPos!==undefined&&insertPos!==''){{
      d.body.blocks.splice(parseInt(insertPos),0,newBlock);
    }}else{{
      d.body.blocks.push(newBlock);
    }}
    // If a video was added, auto-update narration and clear audio cache
    if(dataUri.startsWith('data:video/')){{
      const narr=d.narration||'';
      if(!/video|watch|demo|action|look at/i.test(narr)){{
        d.narration=(narr.trim()?narr.trim()+' ':'')+'Now, let\\'s watch the video to see this in action.';
        S[cur].narr=d.narration;
      }}
      if(audioCache)delete audioCache[cur];
    }}
    input.value='';
    closeEdit();
    setTimeout(()=>openEdit(),250);
  }};
  reader.readAsDataURL(file);
}}

function saveEdit(){{
  pushUndo();
  const d=slidesData[cur];
  const tp=d.type||'content';

  d.t=document.getElementById('edit-title').value;
  d.s=document.getElementById('edit-sub').value;
  d.narration=document.getElementById('edit-narr').value;

  // Update the S array
  S[cur].t=d.t;
  S[cur].s=d.s;
  S[cur].narr=d.narration;

  if(tp==='content'){{
    const blocks=(d.body&&d.body.blocks)||[];
    document.querySelectorAll('[data-bi]').forEach(el=>{{
      const bi=parseInt(el.dataset.bi);
      const field=el.dataset.field;
      const dtype=el.dataset.type;
      if(!blocks[bi])return;
      if(dtype==='list'){{
        blocks[bi].items=el.value.split('\\n').filter(x=>x.trim());
      }}else if(dtype==='steps'){{
        blocks[bi].items=el.value.split('\\n').filter(x=>x.trim()).map(x=>({{text:x}}));
      }}else if(dtype==='icons'){{
        blocks[bi].items=el.value.split('\\n').filter(x=>x.trim()).map(x=>{{const p=x.split('|');return{{icon:p[0]||'',label:p[1]||'',desc:p[2]||''}}}});
      }}else if(dtype==='csv'){{
        blocks[bi][field]=el.value.split(',').map(x=>x.trim());
      }}else if(dtype==='table'){{
        blocks[bi].rows=el.value.split('\\n').filter(x=>x.trim()).map(r=>r.split(',').map(c=>c.trim()));
      }}else if(field==='html'){{
        blocks[bi].html=el.value;blocks[bi].text=el.value;blocks[bi].content=el.value;
      }}else if(field==='good'||field==='bad'){{
        blocks[bi][field]=el.value;
      }}else if(field==='alt'){{
        blocks[bi].alt=el.value;blocks[bi].caption=el.value;
      }}else{{
        blocks[bi][field]=el.value;if(field==='text'){{blocks[bi].content=el.value;blocks[bi].code=el.value}}
      }}
    }});
  }}else if(tp==='quiz'){{
    const body=d.body||{{}};
    body.question=document.getElementById('eq-q').value;
    const opts=[];
    for(let i=0;i<4;i++){{const el=document.getElementById('eq-o'+i);if(el)opts.push(el.value)}}
    body.options=opts;
    body.correct=parseInt(document.getElementById('eq-ci').value)||0;
    body.explanations={{correct:document.getElementById('eq-exc').value,wrong:document.getElementById('eq-exw').value}};
    d.body=body;
  }}else if(tp==='matching'){{
    const body=d.body||{{}};
    const pairsEl=document.getElementById('eq-pairs');
    if(pairsEl){{
      body.pairs=pairsEl.value.split('\\n').filter(x=>x.trim()).map(line=>{{
        const parts=line.split('|').map(p=>p.trim());
        return{{left:parts[0]||'',right:parts[1]||''}};
      }});
    }}
    d.body=body;
  }}else if(tp==='prompt_builder'){{
    const body=d.body||{{}};
    const instrEl=document.getElementById('eq-pb-instr');if(instrEl)body.instructions=instrEl.value;
    const chipsEl=document.getElementById('eq-pb-chips');if(chipsEl)body.chips=chipsEl.value.split('\\n').filter(x=>x.trim());
    const phEl=document.getElementById('eq-pb-ph');if(phEl)body.placeholder=phEl.value;
    d.body=body;
  }}else if(tp==='ordering'){{
    const body=d.body||{{}};
    const instrEl=document.getElementById('eq-ord-instr');if(instrEl)body.instructions=instrEl.value;
    const itemsEl=document.getElementById('eq-ord-items');if(itemsEl)body.correct_order=itemsEl.value.split('\\n').filter(x=>x.trim());
    d.body=body;
  }}else if(tp==='milestone'){{
    const body=d.body||{{}};
    const emojiEl=document.getElementById('eq-ms-emoji');if(emojiEl)body.emoji=emojiEl.value;
    const msgEl=document.getElementById('eq-ms-msg');if(msgEl)body.message=msgEl.value;
    d.body=body;
  }}else if(tp==='completion'){{
    const body=d.body||{{}};
    const taEl=document.getElementById('eq-comp-ta');if(taEl)body.takeaways=taEl.value.split('\\n').filter(x=>x.trim());
    const ctaEl=document.getElementById('eq-comp-cta');if(ctaEl)body.cta=ctaEl.value;
    d.body=body;
  }}

  // Clear audio cache for this slide (narration changed)
  if(audioCache)delete audioCache[cur];

  closeEdit();
  rebuildAllSlides();
}}

// ── KEYS ──
document.addEventListener('keydown',e=>{{if(e.key==='ArrowRight')go(cur+1);if(e.key==='ArrowLeft')go(cur-1)}});

R();
showWelcome();
</script>
</body>
</html>'''


def extract_pptx_slide_titles(filepath):
    """Extract slide titles from a PPTX file for the slide-by-slide image assignment UI."""
    slides = []
    with zipfile.ZipFile(filepath, "r") as zf:
        slide_paths = _pptx_slide_order(zf)
        for i, sp in enumerate(slide_paths):
            try:
                slide_tree = etree.parse(zf.open(sp))
                title, texts = _get_slide_texts(slide_tree)
                if not title and texts:
                    for t in texts:
                        if len(t) > 3:
                            title = t[:80]
                            break
            except Exception:
                title = ""
            slides.append({
                "index": i,
                "title": title or f"Slide {i + 1}",
                "slide_number": i + 1
            })
    return slides


def extract_pdf_page_titles(filepath):
    """Extract page titles/first-lines from a PDF for the slide-by-slide image assignment UI."""
    pages = []
    try:
        reader = PdfReader(filepath)
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            title = ""
            if text:
                for line in text.split("\n"):
                    line = line.strip()
                    if line:
                        title = line[:80]
                        break
            pages.append({
                "index": i,
                "title": title or f"Page {i + 1}",
                "slide_number": i + 1
            })
    except Exception as e:
        print(f"PDF title extraction warning: {e}")
    return pages


def generate_lesson(pdf_text, api_key, course_title=None, elevenlabs_key="", elevenlabs_voice="EXAVITQu4vr4xnSDxMaL", images=None, slide_text_notes=None):
    """Generate interactive HTML lesson from PDF text."""
    images_info = [{"page": img["page"], "desc": img["desc"]} for img in (images or [])]
    slides_data = generate_slides_json(pdf_text, api_key, course_title, images_info=images_info or None, slide_text_notes=slide_text_notes)
    title = course_title or "Interactive Lesson"
    return build_html(slides_data, title, elevenlabs_key=elevenlabs_key, elevenlabs_voice=elevenlabs_voice, images=images)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def extract():
    """Extract slide titles from an uploaded PPTX/PDF without generating a lesson."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only PPTX and PDF files are allowed"}), 400

    filename = secure_filename(file.filename)
    ext = get_file_ext(filename)
    temp_path = os.path.join(app.config["UPLOAD_FOLDER"], f"temp_{uuid.uuid4().hex}_{filename}")
    file.save(temp_path)

    try:
        if ext in ("pptx", "ppt"):
            slides = extract_pptx_slide_titles(temp_path)
            file_type = "pptx"
        else:
            slides = extract_pdf_page_titles(temp_path)
            file_type = "pdf"

        return jsonify({
            "success": True,
            "file_type": file_type,
            "slides": slides,
            "total_slides": len(slides)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/convert", methods=["POST"])
def convert():
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "Please provide your Anthropic API key"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only PPTX and PDF files are allowed"}), 400

    filename = secure_filename(file.filename)
    ext = get_file_ext(filename)
    temp_path = os.path.join(app.config["UPLOAD_FOLDER"], f"temp_{uuid.uuid4().hex}_{filename}")
    file.save(temp_path)

    try:
        # Extract text + images based on file type
        if ext in ("pptx", "ppt"):
            content_text = extract_pptx_text(temp_path)
            auto_images = extract_pptx_images(temp_path)
            slide_label = "Slide"
        else:
            content_text = extract_pdf_text(temp_path)
            auto_images = extract_pdf_images(temp_path)
            slide_label = "Page"

        if not content_text.strip():
            return jsonify({"error": "Could not extract text from this file. It may be empty or corrupted."}), 400

        # Process manually uploaded images (legacy bulk upload)
        manual_images = process_uploaded_images(request.files.getlist("images"))

        # Process slide-specific image assignments
        assigned_images = []
        image_assignments_json = request.form.get("image_assignments", "").strip()
        if image_assignments_json:
            try:
                assignments = json.loads(image_assignments_json)
                # assignments is a dict: {"0": "slide_image_0", "3": "slide_image_3", ...}
                # The keys are slide indices, values are the form field names
                for slide_idx_str, field_name in assignments.items():
                    slide_idx = int(slide_idx_str)
                    img_file = request.files.get(field_name)
                    if img_file and img_file.filename:
                        img_ext = get_file_ext(img_file.filename)
                        if img_ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
                            blob = img_file.read()
                            content_type = img_file.content_type or f"image/{img_ext}"
                            b64 = base64.b64encode(blob).decode("utf-8")
                            assigned_images.append({
                                "page": slide_idx + 1,
                                "data_uri": f"data:{content_type};base64,{b64}",
                                "desc": f"User-assigned image for slide {slide_idx + 1}",
                                "source": "assigned"
                            })
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Warning: Could not parse image_assignments: {e}")

        # Combine: assigned images first (highest priority), then manual uploads, then auto-extracted
        all_images = assigned_images + manual_images + auto_images

        # Process per-slide text notes
        slide_text_notes = {}
        text_notes_json = request.form.get("slide_text_notes", "").strip()
        if text_notes_json:
            try:
                raw_notes = json.loads(text_notes_json)
                # raw_notes is { "0": "text for slide 0", "3": "text for slide 3", ... }
                for slide_idx_str, text in raw_notes.items():
                    text = text.strip()
                    if text:
                        slide_text_notes[int(slide_idx_str)] = text
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Warning: Could not parse slide_text_notes: {e}")

        course_title = request.form.get("title", "").strip()
        if not course_title:
            # Derive from filename: "Investor-Pitch-Deck-Lesson.pptx" -> "Investor Pitch Deck Lesson"
            raw_name = os.path.splitext(filename)[0]
            course_title = raw_name.replace("-", " ").replace("_", " ").strip()
            if not course_title:
                course_title = None
        elevenlabs_key = request.form.get("elevenlabs_key", "").strip()
        elevenlabs_voice = request.form.get("elevenlabs_voice", "").strip() or "EXAVITQu4vr4xnSDxMaL"
        html_content = generate_lesson(
            content_text, api_key, course_title,
            elevenlabs_key=elevenlabs_key, elevenlabs_voice=elevenlabs_voice,
            images=all_images, slide_text_notes=slide_text_notes
        )

        output_name = f"lesson_{uuid.uuid4().hex[:8]}.html"
        output_path = os.path.join(app.config["UPLOAD_FOLDER"], output_name)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        slides_count = content_text.count(f"--- {slide_label}")

        return jsonify({
            "success": True,
            "filename": output_name,
            "preview_url": f"/preview/{output_name}",
            "download_url": f"/download/{output_name}",
            "pages_extracted": slides_count,
            "chars_extracted": len(content_text),
            "images_extracted": len(auto_images),
            "images_uploaded": len(manual_images),
        })

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse AI response as JSON. Try again. Detail: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/ai-suggest", methods=["POST"])
def ai_suggest():
    """Use Claude to suggest changes to a slide based on user instruction."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    api_key = data.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "API key required. Set it in the Configure section."}), 400

    slide = data.get("slide", {})
    instruction = data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "Please describe what changes you want."}), 400

    # Build a focused prompt for Claude
    system_prompt = """You are an expert instructional designer editing a single lesson slide.
You will receive the current slide data as JSON and a user instruction describing what to change.
Return ONLY valid JSON with the updated slide. Keep the same structure/schema.

Rules:
- Keep the same "type" and "cat" unless the user explicitly asks to change them
- For content slides: preserve the blocks array structure, update text/items as needed
- For quiz slides: preserve the options/correct/explanations structure
- Write "narration" as a friendly teacher explaining the content (2-5 sentences)
- Keep content concise — suitable for a single mobile screen
- Return ONLY the JSON object, no markdown fences, no extra text"""

    user_msg = f"""Current slide JSON:
{json.dumps(slide, ensure_ascii=False)}

User instruction: {instruction}

Return the updated slide JSON only."""

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "stream": False,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)

        updated_slide = json.loads(text)
        return jsonify({"success": True, "slide": updated_slide})

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return jsonify({"error": f"Claude API error ({e.code}): {body[:200]}"}), 500
    except json.JSONDecodeError:
        return jsonify({"error": "Claude returned invalid JSON. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload-html", methods=["POST"])
def upload_html():
    """Accept an HTML lesson file upload for editing."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not file.filename.lower().endswith((".html", ".htm")):
        return jsonify({"error": "Only HTML files are allowed"}), 400

    filename = secure_filename(file.filename)
    # Add unique prefix to avoid collisions
    unique_name = f"edit_{uuid.uuid4().hex[:8]}_{filename}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    file.save(filepath)

    # Ensure the file has the data-edit attribute for editing
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            html = f.read()
        if 'data-edit="1"' not in html:
            html = html.replace("<body", '<body data-edit="1"', 1)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "filename": unique_name,
        "preview_url": f"/preview/{unique_name}",
        "download_url": f"/download/{unique_name}",
    })


@app.route("/preview/<filename>")
def preview(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/batch-convert", methods=["POST"])
def batch_convert():
    """Convert multiple PPTX/PDF files in parallel."""
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "Please provide your Anthropic API key"}), 400

    files = request.files.getlist("files")
    if not files or len(files) == 0:
        return jsonify({"error": "No files uploaded"}), 400

    # Validate all files first
    valid_files = []
    for f in files:
        if f.filename and allowed_file(f.filename):
            valid_files.append(f)

    if not valid_files:
        return jsonify({"error": "No valid PPTX or PDF files found"}), 400

    elevenlabs_key = request.form.get("elevenlabs_key", "").strip()
    elevenlabs_voice = request.form.get("elevenlabs_voice", "").strip() or "EXAVITQu4vr4xnSDxMaL"

    # Save all files to temp paths first (request.files can only be read once)
    file_infos = []
    for f in valid_files:
        filename = secure_filename(f.filename)
        ext = get_file_ext(filename)
        temp_path = os.path.join(app.config["UPLOAD_FOLDER"], f"temp_{uuid.uuid4().hex}_{filename}")
        f.save(temp_path)
        file_infos.append({
            "original_name": f.filename,
            "filename": filename,
            "ext": ext,
            "temp_path": temp_path,
        })

    import time as _time

    def convert_single(info, attempt=0):
        """Convert a single file — runs in a thread."""
        temp_path = info["temp_path"]
        filename = info["filename"]
        ext = info["ext"]
        try:
            if ext in ("pptx", "ppt"):
                content_text = extract_pptx_text(temp_path)
                auto_images = extract_pptx_images(temp_path)
            else:
                content_text = extract_pdf_text(temp_path)
                auto_images = extract_pdf_images(temp_path)

            if not content_text.strip():
                return {"filename": info["original_name"], "error": "Could not extract text from this file."}

            course_title = os.path.splitext(filename)[0].replace("-", " ").replace("_", " ").strip() or None

            html_content = generate_lesson(
                content_text, api_key, course_title,
                elevenlabs_key=elevenlabs_key, elevenlabs_voice=elevenlabs_voice,
                images=auto_images
            )

            output_name = f"lesson_{uuid.uuid4().hex[:8]}.html"
            output_path = os.path.join(app.config["UPLOAD_FOLDER"], output_name)
            with open(output_path, "w", encoding="utf-8") as out_f:
                out_f.write(html_content)

            return {
                "filename": info["original_name"],
                "output_name": output_name,
                "preview_url": f"/preview/{output_name}",
                "download_url": f"/download/{output_name}",
                "success": True,
            }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            # Retry on rate limit (429) or overloaded (529) up to 2 times
            if e.code in (429, 529) and attempt < 2:
                _time.sleep(15 * (attempt + 1))
                return convert_single(info, attempt + 1)
            return {"filename": info["original_name"], "error": f"API error {e.code}: {body}"}
        except Exception as e:
            return {"filename": info["original_name"], "error": str(e)}
        finally:
            # Only clean up on the original call, not retries
            if attempt == 0 and os.path.exists(temp_path):
                os.remove(temp_path)

    # Run conversions in parallel (max 2 to reduce API rate limit risk)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(convert_single, info): info for info in file_infos}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    # Sort results back to original file order
    order = {info["original_name"]: i for i, info in enumerate(file_infos)}
    results.sort(key=lambda r: order.get(r["filename"], 999))

    successful = [r for r in results if r.get("success")]
    return jsonify({
        "success": True,
        "results": results,
        "total": len(file_infos),
        "completed": len(successful),
        "failed": len(file_infos) - len(successful),
    })


@app.route("/batch-download-zip", methods=["POST"])
def batch_download_zip():
    """Download multiple lesson HTMLs as a single ZIP file."""
    data = request.get_json()
    if not data or "files" not in data:
        return jsonify({"error": "No files specified"}), 400

    filenames = data["files"]  # list of output_name strings
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in filenames:
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], fname)
            if os.path.isfile(filepath):
                # Use a cleaner name in the zip
                zf.write(filepath, fname)

    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": 'attachment; filename="lessons.zip"'}
    )


@app.route("/download/<filename>")
def download(filename):
    import re as _re
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.isfile(filepath):
        return "File not found", 404
    with open(filepath, "r", encoding="utf-8") as f:
        html = f.read()
    # Strip edit mode: remove data-edit attribute so edit button is hidden via CSS
    html = html.replace(' data-edit="1"', '')
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def scrape_url(url, timeout=30):
    """Scrape text content from a URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        import re as _re
        # Remove script/style tags and their content
        raw = _re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=_re.DOTALL | _re.IGNORECASE)
        raw = _re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=_re.DOTALL | _re.IGNORECASE)
        # Remove HTML tags
        raw = _re.sub(r'<[^>]+>', ' ', raw)
        # Decode HTML entities
        import html as _html_mod
        raw = _html_mod.unescape(raw)
        # Collapse whitespace
        raw = _re.sub(r'\s+', ' ', raw).strip()
        # Trim if very long
        if len(raw) > 80000:
            raw = raw[:80000] + "\n\n[... Content truncated ...]"
        return raw
    except Exception as e:
        return f"[Error scraping {url}: {str(e)}]"


@app.route("/topic-convert", methods=["POST"])
def topic_convert():
    """Generate a tutorial lesson from a topic description and scraped URLs."""
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "Please provide your Anthropic API key"}), 400

    topic = request.form.get("topic", "").strip()
    description = request.form.get("description", "").strip()
    urls_raw = request.form.get("urls", "").strip()

    if not topic:
        return jsonify({"error": "Please provide a topic"}), 400

    # Parse URLs (one per line)
    urls = [u.strip() for u in urls_raw.split("\n") if u.strip() and u.strip().startswith("http")]

    # Scrape all URLs in parallel
    scraped_content = ""
    if urls:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(scrape_url, url): url for url in urls}
            for future in _cf.as_completed(futures):
                url = futures[future]
                text = future.result()
                scraped_content += f"\n\n--- SOURCE: {url} ---\n{text}\n"

    # Build combined content
    combined = f"TOPIC: {topic}\n\n"
    if description:
        combined += f"DESCRIPTION & CONTEXT:\n{description}\n\n"
    if scraped_content:
        combined += f"REFERENCE DOCUMENTATION & CONTENT SCRAPED FROM URLS:\n{scraped_content}\n"

    if not description and not scraped_content:
        # No content at all — just a topic, let Claude generate from its knowledge
        combined += "Generate a comprehensive tutorial on this topic using your knowledge.\n"

    elevenlabs_key = request.form.get("elevenlabs_key", "").strip()
    elevenlabs_voice = request.form.get("elevenlabs_voice", "").strip() or "EXAVITQu4vr4xnSDxMaL"

    try:
        slides_data = generate_slides_json(combined, api_key, course_title=topic)
        html_content = build_html(slides_data, topic, elevenlabs_key=elevenlabs_key, elevenlabs_voice=elevenlabs_voice)

        output_name = f"lesson_{uuid.uuid4().hex[:8]}.html"
        output_path = os.path.join(app.config["UPLOAD_FOLDER"], output_name)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        return jsonify({
            "success": True,
            "filename": output_name,
            "preview_url": f"/preview/{output_name}",
            "download_url": f"/download/{output_name}",
            "urls_scraped": len(urls),
            "content_length": len(combined),
        })

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return jsonify({"error": f"API error {e.code}: {body}"}), 500
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse AI response as JSON. Try again. Detail: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("VERCEL"))
    app.run(debug=debug, host="0.0.0.0", port=port)
