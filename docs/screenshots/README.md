# Screenshot provenance

These PNGs are direct, untouched captures of Milo running in an X terminal. The terminal was displayed on a 1400×900, 24-bit Xvfb framebuffer and captured directly to PNG with `scrot --overwrite`. No image-processing command was run on any image: no crop, resize, compositing, conversion, annotation, recoloring, or metadata rewrite.

Commands visibly executed:

- `MILO_HOME=/tmp/milo-screenshot-home uv run milo setup --provider codex --skills recommended --non-interactive`
- `MILO_HOME=/tmp/milo-screenshot-home uv run milo doctor`
- `MILO_HOME=/tmp/milo-interface-home uv run milo` followed by the visibly entered task in `codex-chat.png`; that isolated setup selected Codex with `gpt-5.4-mini` at Milo's low-effort default.

The generic temporary state paths prevent a personal home directory from appearing. All three images were visually inspected for clipping and sensitive data.

SHA-256 at capture:

- `setup.png`: `35952eae8a5f64e34a0e37e18fde91c6a805f2ea644572cfcbf63ebe3d8abdb5`
- `doctor.png`: `89e9512826a4bd894ce695b94efd086e8e38886def09d904ac5ee14866b821f2`
- `codex-chat.png`: `7697fa8977b7b928984296066037c3b90888d6d8a5314e28110d5fa5e0c9ae0f`
