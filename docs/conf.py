# Configuration file for the Sphinx documentation builder.
#
# faultflow documentation. Built with Sphinx + Furo + MyST-Markdown.
# Pages are authored in Markdown (.md) under docs/.

import pathlib

# -- Project information -----------------------------------------------------

project = "faultflow"
author = "faultflow contributors"
copyright = "2026, faultflow contributors"

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
html_title = "faultflow"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_theme_options = {
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
