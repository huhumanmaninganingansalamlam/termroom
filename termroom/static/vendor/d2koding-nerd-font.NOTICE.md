# D2KodingLigature Nerd Font Mono notice

Termroom redistributes four range-based WOFF2 subsets of the Regular Mono face
from the official Nerd Fonts 3.5.0 D2Coding release:

- Release: Nerd Fonts 3.5.0
- Upstream: https://github.com/ryanoasis/nerd-fonts
- Release tag commit: `bbb2db23a131139161d66a9b526a6fdc79875c92`
- Source archive: `D2Coding.tar.xz`
- Source archive URL: https://github.com/ryanoasis/nerd-fonts/releases/download/v3.5.0/D2Coding.tar.xz
- Source archive SHA-256: `c1d4e7cbee20b9e55d2481762bbb8413124fda224cee26863b805fe2f863aaec`
- Source TTF: `D2KodingLigatureNerdFontMono-Regular.ttf`
- Source TTF SHA-256: `be8964904705f43a1e5a62339629d9e20eb37316008dda4de5b5681547ea2996`
- Distributed subsets:

| Subset | Unicode ranges | Size | SHA-256 |
| --- | --- | ---: | --- |
| `core-hangul` | `U+0000-4DFF,U+AC00-DFFF,U+FB00-FFFF` | 567,584 | `b9fae6a182cc440dcf69c7a8b3a8b2ad60284fa2ed67e8e41c2f26bead83ede9` |
| `cjk` | `U+4E00-ABFF,U+F900-FAFF` | 801,848 | `0d1b8923dc714312107b8282b1e2f1d79645e48f26e0a12d38dac886028c9783` |
| `nerd-bmp` | audited BMP PUA ranges in `terminal-font.css` | 497,412 | `64c9508468b380ac4e31f65ce5fc548e14939670ae1833870596f276661122dd` |
| `nerd-supp` | `U+F0001-F1AF0` | 397,908 | `25fa39d5040346d76385e04aa5977b15b1bc72b3af27bff429f27e341fc03f0d` |

The source TTF was subset with FontTools 4.63.0 and brotli 1.2.0. Hinting,
metrics, and all layout features are retained. The unsupported `PfEd` FontLab
editor metadata table is intentionally dropped; it has no browser runtime
effect. The four subsets select exactly the 29,694 source cmap codepoints in
Termroom's audited ranges, with no missing, extra, or overlapping ownership.
Their units-per-em, ascent, descent, and average-width metrics match. Two
independent builds produced byte-identical output.

Reproduction commands (run after extracting the release archive):

```sh
font_source=D2KodingLigatureNerdFontMono-Regular.ttf
font_prefix=d2koding-ligature-nerd-font-mono-3.5.0

subset_font() {
  font_output=$1
  font_ranges=$2
  uv run --python 3.11 --no-project \
    --with fonttools==4.63.0 --with brotli==1.2.0 -- \
    pyftsubset "$font_source" \
    --output-file="$font_output" \
    --unicodes="$font_ranges" \
    --flavor=woff2 \
    --layout-features='*' \
    --name-IDs='*' \
    --name-languages='*' \
    --notdef-glyph \
    --notdef-outline
}

subset_font "$font_prefix-core-hangul.woff2" \
  'U+0000-4DFF,U+AC00-DFFF,U+FB00-FFFF'
subset_font "$font_prefix-cjk.woff2" \
  'U+4E00-ABFF,U+F900-FAFF'
subset_font "$font_prefix-nerd-bmp.woff2" \
  'U+E000-E00A,U+E0A0-E0A3,U+E0B0-E0C8,U+E0CA,U+E0CC-E0D2,U+E0D4,U+E0D6-E0D7,U+E200-E2A9,U+E300-E3E3,U+E5FA-E6BB,U+E700-E8EF,U+EA60-EA88,U+EA8A-EA8C,U+EA8F-EAC7,U+EAC9,U+EACC-EB09,U+EB0B-EB4E,U+EB50-EC5E,U+EC60-EC84,U+ED00-EFCF,U+F000-F385,U+F400-F533'
subset_font "$font_prefix-nerd-supp.woff2" \
  'U+F0001-F1AF0'
```

The base D2Coding font is Copyright (c) 2015 NAVER Corporation and is licensed
under the SIL Open Font License 1.1. Its complete copyright notice and license
are distributed as `d2koding-nerd-font.OFL.txt`. Nerd Fonts renames the patched
family to `D2KodingLigature` to comply with D2Coding's Reserved Font Name.

The Nerd Fonts patcher/project license is distributed as
`nerd-fonts.LICENSE`. The patched font also incorporates glyphs from the icon
projects listed by the official 3.5.0 release README:

| Icon set | Upstream | Version | License |
| --- | --- | --- | --- |
| Codicons | https://github.com/microsoft/vscode-codicons | 0.0.45 | CC BY 4.0 |
| Devicons | https://github.com/devicons/devicon | 2.17.0 | MIT |
| extraglyphs | https://github.com/source-foundry/Hack | - | MIT |
| Font Awesome | https://github.com/FortAwesome/Font-Awesome | 6.5.1 | CC BY 4.0 |
| Font Awesome Extension | https://github.com/AndreLZGava/font-awesome-extension | 0.0.3 | MIT |
| Font Logos | https://github.com/lukas-w/font-logos | 1.3.0 | The Unlicense (release README: “unlicensed”) |
| MaterialDesign | https://github.com/Templarian/MaterialDesign-Font | 2022-10-06 | Apache 2.0 |
| Octicons | https://github.com/primer/octicons | 18.3.0 | MIT |
| Seti and original | https://github.com/jesseweed/seti-ui | 0.8.1 | MIT |
| Pomicons | https://github.com/gabrielelana/pomicons | 1.001 | OFL 1.1 RFN |
| Powerline Extra | https://github.com/ryanoasis/powerline-extra-symbols | 1.200 | MIT |
| Powerline Symbols | https://github.com/powerline/powerline | 1.000 (circa 2013) | MIT |
| Power Symbols IEC | https://github.com/jloughry/Unicode | 2015-02 | MIT |
| Weather Icons | https://github.com/erikflowers/weather-icons | 2.0.10 (1.100) | OFL 1.1 |

Canonical license texts:

- CC BY 4.0: https://creativecommons.org/licenses/by/4.0/legalcode
- Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
- SIL Open Font License 1.1: https://openfontlicense.org/open-font-license-official-text/
- The Unlicense: https://unlicense.org/

The glyph modifications were made by Nerd Fonts; Termroom subsets and compresses
the official patched TTF into WOFF2, assigns a CSS-only family alias, disables
ligatures, and restricts browser selection to audited codepoint ranges. Product
names and logos may also be protected by trademark law; bundling an icon does
not grant trademark rights.
