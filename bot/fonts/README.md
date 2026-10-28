# Report fonts

The `/damage` renderer uses the Mini App's typefaces, bundled so image generation works without network access:

- [Lora](https://github.com/google/fonts/tree/main/ofl/lora) for headings and initials.
- [DM Sans](https://github.com/google/fonts/tree/main/ofl/dmsans) for labels and body text.
- [Manrope](https://github.com/google/fonts/tree/main/ofl/manrope) for amounts and the wordmark.

Each directory includes its font's SIL Open Font License. The variable font files are unmodified copies from Google Fonts; their filenames have been shortened locally. The renderer selects the weight and optical size through Pillow.

The original DejaVu Sans files and their license remain available for other project assets.
