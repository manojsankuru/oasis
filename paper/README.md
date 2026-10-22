# paper/

4-page ACM SIGSPATIAL submission for OASIS 2026, Track A.

## Build

```text
pdflatex -interaction=nonstopmode main.tex
bibtex main
pdflatex -interaction=nonstopmode main.tex
pdflatex -interaction=nonstopmode main.tex
```

Requires the `acmart` class (TeX Live: `tlmgr install acmart`, or Overleaf
template "ACM Conference Proceedings Primary Article").

## Before submitting

1. Run `python -m src.experiments.report` from the repository root. Every
   semantic `\TD{key}` in `main.tex` must map to a traceable metric under
   `outputs/paper/numbers.json` -> `paper_keys`. Source `sha256` and `bytes`
   values there are computed over text with CRLF line endings turned into LF,
   so they hold on any checkout. Binary files are hashed as stored.
2. Review `remaining_paper_todos` in that JSON. Quantitative mappings do not
   resolve prose, acknowledgment, author, conference, or bibliography work.
3. Review the display-value definitions at the top of `main.tex` against the
   exact values in `numbers.json`. The `\TD{key}` calls remain semantic and
   auditable, but render the reviewed values instead of `[VALUE]` markers.
4. Confirm the PDF is **4 pages or fewer, references included**.
5. Verify every bibliography entry against the publisher record. They were
   seeded from memory and the volume, page, and DOI fields are not checked.

## Figures

Generate the two data figures from the repository root with
`python -m src.figures`. `architecture.pdf` was made by hand and has no source
in this repository, so `src.figures` never writes it. The production
`paper/figs/` directory holds exactly these three PDFs, and `main.tex` includes
them directly:

| File | Section | Evidence shown |
| --- | --- | --- |
| `architecture.pdf` | system design | double-column view of both bounded feedback cycles and data/model boundaries |
| `risk_surface.pdf` | spatial risk | single-column 3 m raster-to-tract hazard, vulnerability, and priority view |
| `tradeoff.pdf` | trade-offs | single-column county-separated discrete priority-set view |

`architecture.pdf` is deliberately double-column. The risk and trade-off
figures use standard single-column placement as required by the manuscript
formatting specification; verify that their labels remain legible in the final
compiled paper.

The optional sources under `figure-src/` stay outside `figs/` so the production
gate stays enforceable. The manuscript does not depend on them, and
`figure-src/architecture.tex` did not produce `architecture.pdf`.
