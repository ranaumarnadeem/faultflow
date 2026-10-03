# Configuration file for the Sphinx documentation builder.
#
# faultflow documentation. Built with Sphinx + Furo + MyST-Markdown.
# Pages are authored in Markdown (.md) under docs/.

import pathlib

# -- Project information -----------------------------------------------------

project = "faultflow"
author = "faultflow contributors"
copyright = "2026, faultflow contributors"

# The README's opening sentence: the HTML title and every page's meta
# description.
description = (
    "faultflow is an open-source Automatic Test Pattern Generation (ATPG) and "
    "fault simulation engine for post-synthesis gate-level netlists from Yosys, "
    "with SAT-based test generation, stuck-at and transition fault models, scan "
    "insertion and IEEE 1500 core wrapping."
)

# Single-sourced from the top-level VERSION file (the Nix flake reads the same
# file; see docs/getting_started/installation.md, "Version, overlay, and nix fmt").
release = (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(
    encoding="utf-8"
).strip()
version = release

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",  # author docs in Markdown
    "sphinx_copybutton",  # copy button on code blocks
    "sphinx_design",  # cards / grids / tabs
    "sphinxcontrib.mermaid",  # ```mermaid fences -> rendered diagrams
    "sphinx.ext.githubpages",  # emit .nojekyll for GitHub Pages
    "sphinx_sitemap",  # sitemap.xml from html_baseurl
]

# MyST Markdown extensions.
myst_enable_extensions = [
    "colon_fence",  # ::: fenced admonitions/directives
    "deflist",  # definition lists
    "fieldlist",
    "tasklist",
    "attrs_inline",
    "substitution",
]
myst_heading_anchors = 3

# <meta name="description"> on every page (all pages are MyST Markdown).
myst_html_meta = {"description": description}

# Map plain ```mermaid fences to the mermaid directive, so diagrams also render
# as diagrams (not just a code block) when a page is previewed straight on GitHub.
myst_fence_as_directive = ["mermaid"]

# Follow Furo's light/dark toggle: Furo sets data-theme ("light" / "dark" /
# "auto") on <body>, not <html> -- read that (falling back to the OS preference
# in "auto" mode) and re-render diagrams on toggle, since mermaid.js does not do
# this on its own.
mermaid_init_js = """
document.addEventListener("DOMContentLoaded", () => {
  const getTheme = () => {
    const t = document.body.dataset.theme;
    if (t === "dark") return "dark";
    if (t === "light") return "default";
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "default";
  };
  const blocks = Array.from(document.querySelectorAll(".mermaid"));
  const sources = blocks.map((el) => el.textContent);
  const render = () => {
    const theme = getTheme();
    blocks.forEach((el, i) => {
      el.removeAttribute("data-processed");
      el.innerHTML = sources[i];
    });
    mermaid.initialize({ startOnLoad: false, theme: theme, securityLevel: "loose" });
    mermaid.run({ nodes: blocks });
  };
  render();
  new MutationObserver(render).observe(document.body, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
});
"""

source_suffix = {".md": "markdown"}

# Internal planning notes and the papers/ directory are not part of the
# published site.
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "plans/**",
]

# -- HTML output -------------------------------------------------------------

html_theme = "furo"
html_title = description
html_baseurl = "https://ranaumarnadeem.github.io/faultflow/"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
# Served at the site root: /faultflow/llms.txt.
html_extra_path = ["_static/llms.txt"]

# Page URLs straight under html_baseurl (the default scheme would insert the
# version, e.g. .../faultflow/0.1.0/index.html, which Pages does not serve).
sitemap_url_scheme = "{link}"
sitemap_excludes = ["search.html", "genindex.html"]

html_theme_options = {
    # The logo carries the name; without this the sidebar would also print
    # html_title, the whole description sentence.
    "sidebar_hide_name": True,
    "source_repository": "https://github.com/ranaumarnadeem/faultflow/",
    "source_branch": "main",
    "source_directory": "docs/",
    "light_logo": "logo-light.svg",
    "dark_logo": "logo-dark.svg",
    "light_css_variables": {
        "color-brand-primary": "#1565c0",
        "color-brand-content": "#1565c0",
    },
    "dark_css_variables": {
        "color-brand-primary": "#5b9bd5",
        "color-brand-content": "#5b9bd5",
    },
}
