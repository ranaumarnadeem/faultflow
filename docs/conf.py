# Configuration file for the Sphinx documentation builder.
#
# faultflow documentation. Built with Sphinx + Furo + MyST-Markdown.
# Pages are authored in Markdown (.md) under docs/.

# -- Project information -----------------------------------------------------

project = "faultflow"
author = "faultflow contributors"
copyright = "2026, faultflow contributors"

# faultflow does not yet define a release version string in-tree.
release = ""
version = ""

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",          # author docs in Markdown
    "sphinx_copybutton",    # copy button on code blocks
    "sphinx_design",        # cards / grids / tabs
    "sphinx.ext.githubpages",  # emit .nojekyll for GitHub Pages
]

# MyST Markdown extensions.
myst_enable_extensions = [
    "colon_fence",   # ::: fenced admonitions/directives
    "deflist",       # definition lists
    "fieldlist",
    "tasklist",
    "attrs_inline",
    "substitution",
]
myst_heading_anchors = 3

source_suffix = {".md": "markdown"}

# Internal planning notes, integration scratch, and the papers/ directory are not
# part of the published site. They are excluded wholesale so only the authored
# documentation pages are built.
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "plans/**",         # planning baselines + docs/plans/papers/
    "integration/**",   # internal OpenTestability integration notes
    "superpowers/**",   # internal implementation plans
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
    "light_css_variables": {
        "color-brand-primary": "#1565c0",
        "color-brand-content": "#1565c0",
    },
    "dark_css_variables": {
        "color-brand-primary": "#5b9bd5",
        "color-brand-content": "#5b9bd5",
    },
}
