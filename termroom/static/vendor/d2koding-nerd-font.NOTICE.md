# D2KodingLigature Nerd Font Mono notice

Termroom redistributes one WOFF2 conversion of the Regular Mono face from the
official Nerd Fonts 3.5.0 D2Coding release:

- Release: Nerd Fonts 3.5.0
- Upstream: https://github.com/ryanoasis/nerd-fonts
- Release tag commit: `bbb2db23a131139161d66a9b526a6fdc79875c92`
- Source archive: `D2Coding.tar.xz`
- Source archive URL: https://github.com/ryanoasis/nerd-fonts/releases/download/v3.5.0/D2Coding.tar.xz
- Source archive SHA-256: `c1d4e7cbee20b9e55d2481762bbb8413124fda224cee26863b805fe2f863aaec`
- Source TTF: `D2KodingLigatureNerdFontMono-Regular.ttf`
- Source TTF SHA-256: `be8964904705f43a1e5a62339629d9e20eb37316008dda4de5b5681547ea2996`
- Distributed file: `d2koding-ligature-nerd-font-mono-3.5.0.woff2`
- Distributed WOFF2 SHA-256: `6d491d86d652cf6886afe1a37c50877a7bbf91c9369f529049d0d27cd77131be`
- Distributed WOFF2 size: 2,441,744 bytes

The source TTF was converted without glyph subsetting or outline changes using
FontTools 4.63.0 and brotli 1.2.0. `recalcTimestamp=False` removes build-time
variation; two independent conversions produced byte-identical output. The
glyph order, cmap (30,244 codepoints), hmtx metrics, and OpenType table set were
verified equal after decoding the WOFF2.

Reproduction command (run after extracting the release archive):

```sh
uv run --python 3.11 --no-project --with fonttools==4.63.0 --with brotli==1.2.0 -- \
  python -c 'from fontTools.ttLib import TTFont; import sys; source, output = sys.argv[1:]; font = TTFont(source, recalcTimestamp=False); font.flavor = "woff2"; font.save(output, reorderTables=False)' \
  D2KodingLigatureNerdFontMono-Regular.ttf \
  d2koding-ligature-nerd-font-mono-3.5.0.woff2
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

The glyph modifications were made by Nerd Fonts; Termroom only recompresses the
official patched TTF into WOFF2, assigns a CSS-only family alias, disables
ligatures, and restricts browser selection to audited codepoint ranges. Product
names and logos may also be protected by trademark law; bundling an icon does
not grant trademark rights.
