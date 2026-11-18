"""Extract the keyword sections (701 keyword actions, 702 keyword abilities) from the Comprehensive Rules PDF.

    python comprules.py MagicCompRules_<date>.pdf      # -> comprules_keywords.json   (needs pdftotext, from poppler)
"""
import json
import re
import subprocess
import sys


def pdf_text(path):
    """The PDF's text: pdftotext (Poppler) if installed, else pypdf, else a clear message."""
    import shutil
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True, check=True).stdout
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except ImportError:
        raise SystemExit("Can't read the PDF: install pypdf (pip install pypdf) or Poppler (brew install poppler),\n"
                         "or pass the extracted text instead: comprules_20260807.txt (included in the project).")


def main(pdf, out="comprules_keywords.json"):
    text = open(pdf, encoding="utf-8").read() if pdf.endswith(".txt") else pdf_text(pdf)
    body = text[text.find("100. General"):] if "100. General" in text else text
    gl = [m.start() for m in re.finditer(r"\nGlossary\n", body)]
    rules = body[:gl[-1]] if gl else body
    heads = list(re.finditer(r"(?m)^(7\d\d)\.(\d+)\. ([A-Z][^\n]{1,40})$", rules))
    sections = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(rules)
        if m.group(1) in ("701", "702"):
            sections[m.group(3).strip().lower()] = {"rule": f"{m.group(1)}.{m.group(2)}", "name": m.group(3).strip(),
                                                    "text": re.sub(r"\s+\n", "\n", rules[m.start():end]).strip()[:4000]}
    json.dump(sections, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"{len(sections)} keyword sections -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
